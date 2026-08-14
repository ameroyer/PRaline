"""
GitHub operations via the REST API directly (no `gh` CLI dependency).

Auth: a personal access token read from the GITHUB_TOKEN or GH_TOKEN
environment variable. Prefer a fine-grained PAT scoped to:
  - Contents: Read-only
  - Pull requests: Read and write (review comments, review requests)
  - Issues: Read and write (only because a PR's top-level comment thread is
    the issue-comment endpoint; PRaline never opens, closes or edits an issue)
If your org gates fine-grained PATs behind admin approval, a classic PAT
(Settings -> Developer settings -> Personal access tokens -> Tokens classic)
with the `repo` scope works identically here - the token is broader than
strictly needed, but this module never calls a write-to-code endpoint
regardless of what the token itself is allowed to do.

ALLOWED:
  - Reading repo content, diffs, PR info, issue lists
  - Posting PR review comments (line-level)
  - Posting PR issue comments (top-level)
  - Re-requesting a review from a user (request_review): PR metadata only,
    the API equivalent of the re-request arrow in the reviewers sidebar

NEVER:
  - Push, merge, approve, create branches, commit anything.
  - Open, close or edit issues.
  Every function below maps to exactly one read-only, comment, or
  review-request REST endpoint; there is no code path that can call a
  write-to-code endpoint, and none that can approve or merge a PR.

The token itself never leaves this module: it is read from the environment
per request, sent only to api.github.com as an Authorization header, and
never logged, echoed into an error, written to disk, or handed to a
subprocess (see claude_client.ask, which strips it from the child env).
"""

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .claude_client import FETCH_TOKEN_ENV

API_ROOT = "https://api.github.com"


@dataclass
class PRInfo:
    number: int
    title: str
    author: str
    body: str
    base_branch: str
    head_branch: str
    head_sha: str
    url: str
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    comment_count: int = 0
    review_comment_count: int = 0
    draft: bool = False
    updated_at: str = ""
    created_at: str = ""


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError(
            "No GitHub token found. Create a personal access token and export it "
            "as GITHUB_TOKEN. Easiest: a classic token (https://github.com/settings/tokens) "
            "with the `repo` scope. Tighter alternative: a fine-grained token "
            "(https://github.com/settings/personal-access-tokens) scoped to "
            "Contents: Read-only, Pull requests: Read/write, Issues: Read/write "
            "(the last one only because top-level PR comments go through the "
            "issue-comment endpoint)."
        )
    return token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request(method: str, path: str, **kwargs) -> requests.Response:
    resp = requests.request(method, f"{API_ROOT}{path}", headers=_headers(), **kwargs)
    resp.raise_for_status()
    return resp


def _git(repo_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
        cwd=repo_dir,
    )
    return result.stdout


class FetchError(RuntimeError):
    """A fetch failed over both the remote's own credentials and the token."""


# Fetching through `origin` uses whatever credentials the remote is configured
# for, usually an ssh key. That is the one thing PRaline cannot assume: a tmux
# session outliving the ssh agent that started it, or a machine with no key
# loaded, leaves every fetch dead — while the GitHub token PRaline already
# requires would have worked. So a failed fetch is retried over HTTPS with that
# token, and only a failure of both is reported.
#
# The token reaches git through the environment and is named, not inlined, in
# the credential helper: the value never appears in argv (where `ps` would show
# it to every user on the machine) and is never written to .git/config. The
# empty `credential.helper=` first resets the helper list, so a system helper
# configured elsewhere cannot answer ahead of this one with the wrong account.
_CREDENTIAL_HELPER = (
    f'!f() {{ echo username=x-access-token; echo "password=${FETCH_TOKEN_ENV}"; }}; f'
)


def _try_git(repo_dir: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=repo_dir, env=env
    )


def _first_line(stderr: str) -> str:
    """The one line of a git failure worth showing, with any credentials in a
    remote URL stripped out of it."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return redact_url(lines[0]) if lines else "git fetch failed"


def fetch_refs(repo_dir: Path, repo: str, *refspecs: str) -> None:
    """Fetch `refspecs` into the local repo, by whatever credentials work.

    Both attempts use the same explicit refspecs, so what ends up in
    `refs/remotes/` does not depend on which one succeeded. Like every fetch
    here, this only writes remote-tracking refs and objects: the working tree,
    the index and local branches are untouched."""
    attempt = _try_git(repo_dir, "fetch", "--quiet", "origin", *refspecs)
    if attempt.returncode == 0:
        return

    try:
        token = _token()
    except RuntimeError:
        # Nothing to retry with. Report what the remote actually said, since
        # that is the failure the user has to act on.
        raise FetchError(_first_line(attempt.stderr))

    with_token = _try_git(
        repo_dir,
        "-c", "credential.helper=",
        "-c", f"credential.helper={_CREDENTIAL_HELPER}",
        "fetch", "--quiet", f"https://github.com/{repo}", *refspecs,
        env={**os.environ, FETCH_TOKEN_ENV: token, "GIT_TERMINAL_PROMPT": "0"},
    )
    if with_token.returncode == 0:
        return
    raise FetchError(_first_line(with_token.stderr) or _first_line(attempt.stderr))


def redact_url(url: str) -> str:
    """Strip any credentials embedded in a URL's userinfo part.

    A remote of the form https://ghp_xxx@github.com/owner/repo is a real thing
    people configure, and this string ends up in error messages, so the token
    is removed before anyone can print it."""
    return re.sub(r"://[^/@\s]*@", "://***@", url)


def infer_repo_slug(repo_dir: Path) -> str:
    """Derive owner/repo from the local git remote 'origin'."""
    url = _git(repo_dir, "remote", "get-url", "origin").strip()
    match = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(\.git)?$", url)
    if not match:
        raise RuntimeError(f"Could not parse a GitHub owner/repo from remote: {redact_url(url)}")
    return match.group("slug")


def is_git_ignored(repo_dir: Path, relative_path: str) -> bool:
    """Whether `relative_path` is gitignored in this repo.

    Used to check that `.praline/` cannot be committed: it holds the knowledge
    base, the review log, and possibly a Slack config, none of which belong in
    a repo. A git that cannot answer (not a repo, no git) counts as ignored,
    so the check warns only when it is genuinely sure."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", relative_path],
            capture_output=True,
            cwd=repo_dir,
        )
    except OSError:
        return True
    return result.returncode == 0


def check_auth() -> str:
    """Return the authenticated GitHub username."""
    return _request("GET", "/user").json()["login"]


def _pr_from_json(p: dict) -> PRInfo:
    return PRInfo(
        number=p["number"],
        title=p["title"],
        author=p["user"]["login"],
        body=p["body"] or "",
        base_branch=p["base"]["ref"],
        head_branch=p["head"]["ref"],
        head_sha=p["head"]["sha"],
        url=p["html_url"],
        additions=p.get("additions", 0),
        deletions=p.get("deletions", 0),
        changed_files=p.get("changed_files", 0),
        comment_count=p.get("comments", 0),
        review_comment_count=p.get("review_comments", 0),
        draft=p.get("draft", False),
        updated_at=p.get("updated_at", ""),
        created_at=p.get("created_at", ""),
    )


def list_open_prs(repo: str) -> list[PRInfo]:
    resp = _request("GET", f"/repos/{repo}/pulls", params={"state": "open", "per_page": 100})
    return [_pr_from_json(p) for p in resp.json()]


def get_pr(repo: str, number: int) -> PRInfo:
    resp = _request("GET", f"/repos/{repo}/pulls/{number}")
    return _pr_from_json(resp.json())


class DiffTooLargeError(RuntimeError):
    """GitHub refuses to render this PR as a single diff (406: the diff media
    type is unsupported past ~300 changed files). Callers fall back to
    producing the same diff with local git; see get_pr_diff_local."""


def get_pr_diff(repo: str, number: int) -> str:
    resp = requests.get(
        f"{API_ROOT}/repos/{repo}/pulls/{number}",
        headers={**_headers(), "Accept": "application/vnd.github.v3.diff"},
    )
    if resp.status_code == 406:
        raise DiffTooLargeError(
            f"GitHub cannot render PR #{number} as a diff (406 Not Acceptable): "
            "the PR exceeds the API's diff size limits."
        )
    resp.raise_for_status()
    return resp.text


def fetch_pr_refs(repo_dir: Path, repo: str, pr: PRInfo) -> None:
    """Fetch the PR's base branch and its hidden head ref (`pull/N/head`) so
    both sides of the diff exist locally."""
    try:
        fetch_refs(
            repo_dir,
            repo,
            f"+refs/heads/{pr.base_branch}:refs/remotes/origin/{pr.base_branch}",
            f"+refs/pull/{pr.number}/head:refs/remotes/origin/pull/{pr.number}",
        )
    except FetchError as e:
        raise FetchError(f"Could not fetch PR #{pr.number}: {e}")


def get_pr_diff_local(repo_dir: Path, pr: PRInfo) -> str:
    """The PR's full unified diff from local git (merge-base of the base branch
    to the PR head), equivalent to GitHub's rendered diff but with no size
    limit. Call fetch_pr_refs first."""
    return _git(repo_dir, "diff", f"origin/{pr.base_branch}...{pr.head_sha}")


def get_pr_diffstat_local(repo_dir: Path, pr: PRInfo) -> str:
    """`git diff --stat` over the same range as get_pr_diff_local."""
    return _git(repo_dir, "diff", "--stat", f"origin/{pr.base_branch}...{pr.head_sha}").strip()


def add_pr_worktree(repo_dir: Path, sha: str) -> Path:
    """Check out `sha` detached in a temporary git worktree and return its
    path. The user's working tree, index, and branches are untouched; pair
    with remove_pr_worktree when done."""
    path = Path(tempfile.mkdtemp(prefix="praline-pr-"))
    _git(repo_dir, "worktree", "add", "--detach", str(path), sha)
    return path


def remove_pr_worktree(repo_dir: Path, path: Path) -> None:
    try:
        _git(repo_dir, "worktree", "remove", "--force", str(path))
    except subprocess.CalledProcessError:
        pass  # worst case a stale temp dir; never fail a finished review on cleanup


def get_merged_prs(repo: str, since_days: int = 30) -> list[dict]:
    """Return merged PRs up to `since_days` ago with title, body, number."""
    return get_merged_prs_since(repo, datetime.now(timezone.utc) - timedelta(days=since_days))


def get_merged_prs_since(repo: str, cutoff: datetime) -> list[dict]:
    """Return PRs merged at or after `cutoff`, newest activity first."""
    result = []
    page = 1
    while True:
        resp = _request(
            "GET",
            f"/repos/{repo}/pulls",
            params={
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
                "page": page,
            },
        )
        items = resp.json()
        if not items:
            break
        stop = False
        for p in items:
            if not p.get("merged_at"):
                continue
            merged_at = datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00"))
            if merged_at < cutoff:
                stop = True
                continue
            result.append(p)
        # Since we sort by "updated", we can't strictly early-exit on merged_at,
        # but once every item on a page is older than cutoff it's safe to stop.
        if stop and all(
            datetime.fromisoformat(p["updated_at"].replace("Z", "+00:00")) < cutoff
            for p in items
        ):
            break
        page += 1
        if page > 10:  # hard cap: 1000 closed PRs is plenty to scan
            break
    return result


def get_default_branch(repo: str) -> str:
    """The repo's default branch on GitHub (e.g. 'main')."""
    return _request("GET", f"/repos/{repo}").json()["default_branch"]


def fetch_remote_branch(repo_dir: Path, repo: str, branch: str) -> str:
    """Fetch `branch`'s remote-tracking ref and return it (e.g. 'origin/main').

    Raises FetchError if neither the remote's own credentials nor the GitHub
    token can reach it; callers fall back to reading the local checkout."""
    fetch_refs(repo_dir, repo, f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    return f"origin/{branch}"


def local_commits_behind(repo_dir: Path, ref: str) -> int:
    """How many commits `ref` has that the local HEAD doesn't."""
    return int(_git(repo_dir, "rev-list", "--count", f"HEAD..{ref}").strip())


def get_repo_structure(repo_dir: Path, ref: str, max_files: int = 300) -> str:
    """Return a tree-like listing of the repo (git-tracked files) as of `ref`."""
    raw = _git(repo_dir, "ls-tree", "-r", "--name-only", ref)
    lines = raw.strip().splitlines()
    files = lines[:max_files]
    out = "\n".join(files)
    if len(lines) > max_files:
        out += f"\n... (truncated to {max_files} files)"
    return out


def get_recent_commits(repo_dir: Path, ref: str, n: int = 50) -> str:
    return _git(repo_dir, "log", ref, f"--max-count={n}", "--oneline", "--no-merges").strip()


# One review reads a PR's comments from several places (the status line, the
# prompt's conversation section, the reply lookup). They are the same bytes
# every time, so fetch them once per PR and drop the entry the moment we post,
# which is the only thing that can make them stale.
_COMMENT_CACHE: dict[tuple[str, str, int], list[dict]] = {}


def _cached_comments(kind: str, repo: str, number: int, path: str) -> list[dict]:
    key = (kind, repo, number)
    if key not in _COMMENT_CACHE:
        _COMMENT_CACHE[key] = _request("GET", path, params={"per_page": 100}).json()
    return _COMMENT_CACHE[key]


def forget_pr_comments(repo: str, number: int) -> None:
    """Drop the cached comment lists for a PR. Called after every post, so a
    second look at the same PR sees what we just added."""
    for kind in ("review", "issue"):
        _COMMENT_CACHE.pop((kind, repo, number), None)


def forget_all_comments() -> None:
    """Empty the cache completely.

    The cache is scoped to one look at a PR: posting invalidates the entries it
    could have staled, and a one-shot command exits before anything else can.
    A monitor does neither — it runs for hours — so without this a comment left
    by someone between two passes would never be seen, and a re-review would be
    written against a conversation that has moved on. Called at the top of
    every pass."""
    _COMMENT_CACHE.clear()


def get_pr_review_comments(repo: str, number: int) -> list[dict]:
    """Line-level review comments on the PR diff, including replies (has 'in_reply_to_id')."""
    return _cached_comments("review", repo, number, f"/repos/{repo}/pulls/{number}/comments")


def get_pr_issue_comments(repo: str, number: int) -> list[dict]:
    """Top-level comments on the PR (the issue-comment thread)."""
    return _cached_comments("issue", repo, number, f"/repos/{repo}/issues/{number}/comments")


def get_pr_comment_authors(repo: str, number: int) -> dict[str, int]:
    """Count comments per author across both top-level and line-level threads."""
    counts: dict[str, int] = {}
    for c in get_pr_issue_comments(repo, number) + get_pr_review_comments(repo, number):
        login = c["user"]["login"]
        counts[login] = counts.get(login, 0) + 1
    return counts


def post_pr_comment(repo: str, number: int, body: str) -> None:
    """Post a top-level comment on the PR (via the issues endpoint, as GitHub does)."""
    _request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})
    forget_pr_comments(repo, number)


def post_pr_review_comment(
    repo: str,
    number: int,
    body: str,
    path: str,
    line: int,
    commit_id: str,
    start_line: int | None = None,
) -> None:
    """Post a line-level review comment on the PR diff.

    `commit_id` is the head sha the review was written against; the caller
    already holds it, and pinning the comment to the exact commit that was
    reviewed is both cheaper and more correct than re-reading the PR here.

    `start_line` anchors the comment to a range ending at `line`, which is what
    a multi-line ```suggestion replaces. GitHub rejects a range that isn't
    strictly before the end line, so an equal or larger `start_line` is dropped
    back to a single-line comment rather than sent and refused."""
    payload = {
        "body": body,
        "path": path,
        "commit_id": commit_id,
        "line": line,
        "side": "RIGHT",
    }
    if start_line and start_line < line:
        payload["start_line"] = start_line
        payload["start_side"] = "RIGHT"
    _request("POST", f"/repos/{repo}/pulls/{number}/comments", json=payload)
    forget_pr_comments(repo, number)


def post_pr_review_reply(repo: str, number: int, in_reply_to_id: int, body: str) -> None:
    """Reply to an existing line-level review comment thread, keeping it a single thread."""
    _request(
        "POST",
        f"/repos/{repo}/pulls/{number}/comments",
        json={"body": body, "in_reply_to": in_reply_to_id},
    )
    forget_pr_comments(repo, number)


# Resolving a review thread ("mark conversation as resolved") has no REST
# endpoint - it's GraphQL-only. These two calls are the minimum needed: find
# the thread's GraphQL node id from a REST comment id, then resolve it.

_GRAPHQL_URL = f"{API_ROOT}/graphql"

_FIND_THREAD_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id isResolved comments(first: 100) { nodes { databaseId } } }
      }
    }
  }
}
"""

_RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


def _graphql(query: str, variables: dict) -> dict:
    resp = requests.post(
        _GRAPHQL_URL, headers=_headers(), json={"query": query, "variables": variables}
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")
    return payload["data"]


def find_review_thread_id(repo: str, number: int, comment_id: int) -> str | None:
    """Find the GraphQL review-thread node id that contains the given REST comment id."""
    owner, name = repo.split("/", 1)
    after = None
    while True:
        data = _graphql(
            _FIND_THREAD_QUERY, {"owner": owner, "name": name, "number": number, "after": after}
        )
        threads = data["repository"]["pullRequest"]["reviewThreads"]
        for node in threads["nodes"]:
            comment_ids = {c["databaseId"] for c in node["comments"]["nodes"]}
            if comment_id in comment_ids:
                return None if node["isResolved"] else node["id"]
        if not threads["pageInfo"]["hasNextPage"]:
            return None
        after = threads["pageInfo"]["endCursor"]


def resolve_review_thread(thread_id: str) -> None:
    _graphql(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id})


def request_review(repo: str, number: int, reviewer: str) -> None:
    """Put `reviewer` back on the PR's requested-reviewer list.

    This is the one call here that writes PR metadata rather than a comment.
    It does not approve, merge, or change a single line of code: it is the API
    equivalent of clicking the re-request arrow next to a reviewer's name, so
    the PR shows up in that person's review queue again. GitHub refuses (422)
    when the reviewer is the PR's own author, so callers skip that case."""
    _request(
        "POST",
        f"/repos/{repo}/pulls/{number}/requested_reviewers",
        json={"reviewers": [reviewer]},
    )


