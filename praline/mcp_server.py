"""PRaline as an MCP server, so Claude can drive it directly.

Run it with `praline-mcp`; register it with Claude Code as:

    claude mcp add praline -- praline-mcp

Every tool works on one repository. By default that is the directory the server
was started in, which is the repo you are working in when Claude Code launches
it; `PRALINE_REPO_DIR` overrides that, and every tool takes an explicit
`repo_dir` for the cross-repo case.

Two rules shape the design:

  - **Nothing reaches GitHub without being asked twice.** `review_pr_draft` only ever
    drafts, and returns the draft. Posting is a separate `post_review` call that
    names the comments to post. A model that misreads "review this PR" as
    "review and post" therefore cannot post anything.
  - **stdout belongs to the protocol.** The rest of PRaline prints progress as
    it works, and on a stdio transport a stray print corrupts the JSON-RPC
    stream. Every tool body runs with stdout captured, and the captured text
    comes back as the tool's `log` so the progress is not simply lost.
"""

import json
import os
import sys
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path

from . import config, hardness, slack, watch
from .github import check_auth, get_pr, infer_repo_slug, list_open_prs
from .memory import last_updated_at, merged_since_last_update, update_knowledge
from .reviewer import (
    get_pr_status,
    has_suggestion,
    log_review,
    post_accepted_comments,
    rerequest_review,
    review_pr,
)
from .verdict import count_items, flatten_review, status_label

# mcp 2.x renamed FastMCP to MCPServer and moved it up a level. The two expose
# the same three things this module uses (constructor, `.tool()`, `.run()`), so
# supporting both is one import instead of a version pin.
try:
    try:
        from mcp.server import MCPServer as _Server
    except ImportError:
        from mcp.server.fastmcp import FastMCP as _Server
except ModuleNotFoundError:  # pragma: no cover - import-time guidance only
    raise SystemExit(
        "The MCP server needs the `mcp` package, which is an optional extra.\n"
        "Install it with:  uv pip install 'praline[mcp]'   (or: pip install 'mcp>=1.2')"
    )

server = _Server("praline")

# Drafted reviews, keyed by (repo, pr number), so post_review can post exactly
# what review_pr showed instead of re-running a review that would come back
# subtly different. Process-local and deliberately not persisted: a draft is
# only meaningful for as long as the conversation that produced it.
_DRAFTS: dict[tuple[str, int], dict] = {}


@contextmanager
def _captured():
    """Run a tool body with stdout diverted into a buffer."""
    buffer = StringIO()
    with redirect_stdout(buffer):
        yield buffer


def _resolve(repo_dir: str | None) -> tuple[Path, str]:
    """The repo a tool call is about: (checkout path, owner/repo)."""
    raw = repo_dir or os.environ.get("PRALINE_REPO_DIR") or "."
    path = Path(raw).expanduser().resolve()
    if not (path / ".git").exists():
        raise ValueError(f"Not a git repository: {path}")
    return path, infer_repo_slug(path)


def _model() -> str:
    return os.environ.get("PRALINE_MODEL") or config.DEFAULT_MODEL


def _reply(log: StringIO, **payload) -> str:
    """A tool result: the payload as JSON, with whatever PRaline printed."""
    text = log.getvalue().strip()
    if text:
        payload["log"] = text
    return json.dumps(payload, indent=2, default=str)


def _slack_config(repo_dir: Path, login: str):
    """The Slack config, or None with the reason, so a notification that cannot
    be sent is reported rather than silently skipped."""
    try:
        return slack.load_config(repo_dir, reviewer_login=login), None
    except Exception as e:
        return None, str(e)


@server.tool()
def list_prs(repo_dir: str | None = None) -> str:
    """List the repo's open pull requests, each tagged new / updated / seen
    since PRaline last looked, with the PR it is stacked on if it is part of a
    stack. Read-only."""
    with _captured() as log:
        path, repo = _resolve(repo_dir)
        prs = list_open_prs(repo)
        changes = watch.classify(path, prs)
        rows = [
            {
                "number": c.pr.number,
                "title": c.pr.title,
                "author": c.pr.author,
                "draft": c.pr.draft,
                "url": c.pr.url,
                "state": c.status,
                "updated_at": c.pr.updated_at,
                "stacked_on": c.parent,
            }
            for c in changes
        ]
    return _reply(log, repo=repo, open_prs=len(rows), prs=rows)


@server.tool()
def pr_status(pr: int, repo_dir: str | None = None) -> str:
    """Size and conversation of one open PR: files changed, lines added and
    removed, how many comments and from whom. Read-only."""
    with _captured() as log:
        path, repo = _resolve(repo_dir)
        info = get_pr(repo, pr)
        status = get_pr_status(repo, pr, info)
    return _reply(
        log,
        repo=repo,
        number=info.number,
        title=info.title,
        author=info.author,
        url=info.url,
        draft=info.draft,
        base=info.base_branch,
        head=info.head_branch,
        **status,
    )


@server.tool()
def check_new(mark_seen: bool = False, repo_dir: str | None = None) -> str:
    """What has changed on the repo's open PRs since the last PRaline check.
    Set mark_seen to acknowledge them, so the next check starts from here."""
    with _captured() as log:
        path, repo = _resolve(repo_dir)
        count = watch.run_check(path, repo, mark=mark_seen)
    return _reply(log, repo=repo, new_or_updated=count, marked_seen=mark_seen)


@server.tool()
def review_pr_draft(
    pr: int, hardness_level: int = hardness.DEFAULT, repo_dir: str | None = None
) -> str:
    """Review a pull request and return the draft comments WITHOUT posting
    anything to GitHub. Show the draft to the user; use post_review to publish
    the comments they approve.

    hardness_level is how hard to look: 0 = light (default, high-level comments
    only), 1 = standard (every changed file), 2 = thorough (edge cases, error
    paths, security, tests), 3 = exhaustive (adversarial, also reads the repo
    around the diff). Higher levels are slower and cost more.
    """
    with _captured() as log:
        path, repo = _resolve(repo_dir)
        level = hardness.clamp(hardness_level)
        info = get_pr(repo, pr)
        review = review_pr(
            path, repo, info, model=_model(), custom_prompt=None, interactive=False, level=level
        )
        items = flatten_review(review)
        _DRAFTS[(repo, pr)] = {"review": review, "items": items, "pr": info, "level": level}
        drafted = [
            {
                "index": i,
                "kind": item.get("severity", "nit"),
                "file": item.get("file"),
                "line": item.get("line"),
                "start_line": item.get("start_line"),
                "reply_to_id": item.get("reply_to_id"),
                "resolves_thread": bool(item.get("resolved")),
                "has_suggestion": has_suggestion(item),
                "body": item.get("body", ""),
            }
            for i, item in enumerate(items)
        ]
    return _reply(
        log,
        repo=repo,
        number=pr,
        title=info.title,
        url=info.url,
        depth=hardness.label(level),
        verdict=status_label(review),
        posted=False,
        next_step="Show these to the user, then call post_review with the indices they approve.",
        comments=drafted,
        **count_items(items),
    )


@server.tool()
def post_review(
    pr: int,
    indices: list[int] | None = None,
    notify_slack: bool = False,
    request_review: bool = True,
    repo_dir: str | None = None,
) -> str:
    """Post comments from the draft that review_pr_draft produced. THIS WRITES
    TO GITHUB, publicly and under the user's account: only call it once the
    user has seen the draft and said which comments to post.

    indices selects comments by their index in the draft; omit it to post all
    of them. notify_slack additionally pings the PR author (needs Slack set up).
    """
    with _captured() as log:
        path, repo = _resolve(repo_dir)
        draft = _DRAFTS.get((repo, pr))
        if draft is None:
            raise ValueError(
                f"No draft review for {repo}#{pr} in this session. "
                "Call review_pr_draft first, and show the user the result."
            )
        items = draft["items"]
        if indices is None:
            chosen = list(items)
        else:
            out_of_range = [i for i in indices if not 0 <= i < len(items)]
            if out_of_range:
                raise ValueError(
                    f"No comment at index {out_of_range}: the draft has {len(items)} "
                    "comment(s), indexed from 0."
                )
            chosen = [items[i] for i in indices]
        if not chosen:
            raise ValueError("Nothing selected, so nothing was posted.")

        info = draft["pr"]
        post_accepted_comments(repo, info, chosen)
        log_review(path, info, draft["review"], chosen)

        login = check_auth()
        if request_review:
            rerequest_review(repo, info, login)
        slack_note = "not requested"
        if notify_slack:
            cfg, problem = _slack_config(path, login)
            if cfg is None:
                slack_note = f"skipped: {problem}"
            else:
                slack.notify_review(cfg, repo, info, chosen, draft["review"])
                slack_note = "sent"
    return _reply(
        log,
        repo=repo,
        number=pr,
        url=info.url,
        posted=len(chosen),
        of_drafted=len(items),
        slack=slack_note,
        **count_items(chosen),
    )


@server.tool()
def update_knowledge_base(
    since_days: int = config.DEFAULT_SINCE_DAYS,
    redraw_module_map: bool = True,
    repo_dir: str | None = None,
) -> str:
    """Rebuild the repo's knowledge base: what the code does, the conventions,
    and the lessons from PRs merged in the last since_days days. Also redraws
    the module map shown in the HTML version. Slow (minutes), and it costs a
    few Claude calls, so do not call it speculatively.

    Writes only inside the repo's .praline/ directory."""
    with _captured() as log:
        path, repo = _resolve(repo_dir)
        md_path, html_path = update_knowledge(
            path, repo, model=_model(), since_days=since_days, with_graph=redraw_module_map
        )
        graph = config.load_graph(path)
    return _reply(
        log,
        repo=repo,
        markdown=str(md_path),
        html=str(html_path),
        modules_mapped=len(graph["nodes"]),
        dependencies=len(graph["edges"]),
        updated_at=str(last_updated_at(path)),
    )


@server.tool()
def knowledge_base(include_content: bool = False, repo_dir: str | None = None) -> str:
    """Where the repo's knowledge base lives and how current it is: the
    markdown and HTML paths, the published artifact link if one was recorded,
    when it was last built, and which PRs have been merged since. Pass
    include_content to get the full markdown document back as well."""
    with _captured() as log:
        path, repo = _resolve(repo_dir)
        exists = config.knowledge_exists(path)
        stale = merged_since_last_update(repo, path) if exists else []
        graph = config.load_graph(path)
        payload = {
            "repo": repo,
            "exists": exists,
            "markdown": str(config.knowledge_md_file(path)),
            "html": str(config.knowledge_html_file(path)),
            "artifact_url": config.load_artifact_url(path),
            "updated_at": str(last_updated_at(path)) if exists else None,
            "modules_mapped": len(graph["nodes"]),
            "merged_since_update": [
                {"number": p["number"], "title": p["title"]} for p in stale
            ],
        }
        if include_content:
            payload["content"] = config.knowledge_md_file(path).read_text() if exists else ""
    return _reply(log, **payload)


@server.tool()
def set_artifact_url(url: str, repo_dir: str | None = None) -> str:
    """Record where the knowledge base was published, so knowledge_base can
    hand back the link later. Call this after publishing the HTML page as an
    artifact."""
    with _captured() as log:
        path, repo = _resolve(repo_dir)
        config.save_artifact_url(path, url)
    return _reply(log, repo=repo, artifact_url=config.load_artifact_url(path))


@server.tool()
def review_depths() -> str:
    """The review depth levels review_pr_draft accepts, and what each one does."""
    return json.dumps(
        {
            "default": hardness.DEFAULT,
            "levels": [
                {"level": lvl.value, "name": lvl.name, "does": lvl.blurb}
                for lvl in hardness.LEVELS.values()
            ],
        },
        indent=2,
    )


def main() -> None:
    try:
        check_auth()
    except Exception as e:
        # Fail at startup rather than on the first tool call: a server that
        # cannot reach GitHub has nothing useful to offer.
        print(f"PRaline MCP server: GitHub auth failed: {e}", file=sys.stderr)
        raise SystemExit(1)
    server.run()


if __name__ == "__main__":
    main()
