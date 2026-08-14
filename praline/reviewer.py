"""Review a PR with Claude and run the interactive per-comment approval loop."""

import textwrap
from pathlib import Path

from . import claude_client, config, hardness, prompts
from .github import (
    DiffTooLargeError,
    PRInfo,
    add_pr_worktree,
    fetch_pr_refs,
    find_review_thread_id,
    get_pr,
    get_pr_comment_authors,
    get_pr_diff,
    get_pr_diff_local,
    get_pr_diffstat_local,
    get_pr_issue_comments,
    get_pr_review_comments,
    post_pr_comment,
    post_pr_review_comment,
    post_pr_review_reply,
    remove_pr_worktree,
    request_review,
    resolve_review_thread,
)
from .term import DIM, YELLOW, _c, confirm
from .verdict import (
    count_items,
    flatten_review,
    overview_of,
    status_key,
    status_label,
)

SEVERITY_LABEL = {
    "overall": "📋 OVERALL",
    "reply": "↩️  REPLY",
    "bug": "🐛 BUG",
    "warning": "⚠️  WARNING",
    "nit": "💬 nit",
}


RECENT_REVIEWS_IN_PROMPT = 8


def _format_review_log(repo_dir: Path, pr: PRInfo | None) -> str:
    """PRaline's own past reviews, as context for this one.

    Two things matter and both come from the log: what was already said about
    *this* PR on an earlier pass (so a re-review builds on it instead of
    repeating it), and what was said about the PRs around it (so a stack gets
    reviewed as one story rather than N unrelated diffs). Earlier passes over
    the same PR come first and are never crowded out by the recency window."""
    entries = config.load_review_log(repo_dir)
    if not entries:
        return ""

    same_pr = [e for e in entries if pr and e.get("number") == pr.number]
    others = [e for e in entries if not (pr and e.get("number") == pr.number)]
    chosen = same_pr[-RECENT_REVIEWS_IN_PROMPT:] + others[-RECENT_REVIEWS_IN_PROMPT:]
    if not chosen:
        return ""

    lines = []
    for e in chosen:
        mine = " (THIS PR, earlier pass)" if pr and e.get("number") == pr.number else ""
        head = (
            f"- **#{e.get('number')} {e.get('title', '')}**{mine} "
            f"by {e.get('author', '?')}, reviewed {e.get('reviewed_at', '?')}, "
            f"verdict `{e.get('status', '?')}`, "
            f"{e.get('comments_added', 0)} comment(s), {e.get('comments_left', 0)} reply(ies)"
        )
        lines.append(head)
        if e.get("summary"):
            lines.append(f"  - what I said: {e['summary']}")
    return "\n".join(lines)


def _build_review_prompt(
    repo_dir: Path,
    custom_prompt: str | None,
    pr: PRInfo | None = None,
    level: int = hardness.DEFAULT,
) -> str:
    # The depth setting is orthogonal to the prompt itself: it says how hard to
    # look, not what to look for, so it applies to a custom prompt too.
    base = custom_prompt or prompts.DEFAULT_REVIEW_PROMPT
    base += "\n\n---\n" + hardness.get(level).addendum
    repo_knowledge = config.load_repo_knowledge(repo_dir)
    pr_history = config.load_pr_history(repo_dir)
    review_log = _format_review_log(repo_dir, pr)

    if repo_knowledge:
        base += f"\n\n---\n## REPO KNOWLEDGE\n\n{repo_knowledge}"
    if pr_history:
        base += f"\n\n---\n## PR HISTORY LESSONS\n\n{pr_history}"
    if review_log:
        base += (
            "\n\n---\n## YOUR OWN RECENT REVIEWS IN THIS REPO\n\n"
            "These are reviews you (PRaline) already did here, newest last. Use them: do not "
            "repeat a point you already made on this PR, do carry a concern forward if the new "
            "diff still has it, and read a stacked PR in light of the one below it. If an earlier "
            "verdict no longer holds, say so plainly rather than pretending it never happened.\n\n"
            f"{review_log}"
        )
    return base


def log_review(repo_dir: Path, pr: PRInfo, review: dict, items: list[dict]) -> None:
    """Record this review in `.praline/review_log.json`, the memory that
    _format_review_log feeds back into later prompts."""
    config.append_review_log(
        repo_dir,
        {
            "number": pr.number,
            "title": pr.title,
            "author": pr.author,
            "url": pr.url,
            "head_sha": pr.head_sha,
            "base_branch": pr.base_branch,
            "head_branch": pr.head_branch,
            "reviewed_at": config.now_iso(),
            "status": status_key(review) or "unknown",
            "status_label": status_label(review),
            "summary": overview_of(review, items),
            **count_items(items),
        },
    )


def rerequest_review(repo: str, pr: PRInfo, reviewer_login: str) -> None:
    """Put yourself back in the PR's reviewer list after PRaline had its say,
    so an approval is two clicks away instead of a hunt through the PR list.

    Skipped for your own PRs (GitHub rejects that with a 422) and reported but
    never fatal: the review itself is already posted by the time we get here."""
    if not reviewer_login or reviewer_login.lower() == pr.author.lower():
        return
    try:
        request_review(repo, pr.number, reviewer_login)
        print(_c(f"  ✓ review re-requested from @{reviewer_login}", DIM))
    except Exception as e:
        print(_c(f"  ✗ could not re-request your review on #{pr.number}: {e}", YELLOW))


def build_comment_lookup(repo: str, number: int) -> dict[int, dict]:
    """Map review-comment id -> {author, body, path, line}, for showing reply
    context in the CLI (which thread/comment a `reply` item is answering)."""
    lookup = {}
    for c in get_pr_review_comments(repo, number):
        lookup[c["id"]] = {
            "author": c["user"]["login"],
            "body": c["body"],
            "path": c.get("path"),
            "line": c.get("line") or c.get("original_line"),
        }
    return lookup


def get_pr_status(repo: str, number: int) -> dict:
    """Small pre-review snapshot: size of the diff and who's been commenting."""
    pr = get_pr(repo, number)
    authors = get_pr_comment_authors(repo, number)
    return {
        "changed_files": pr.changed_files,
        "additions": pr.additions,
        "deletions": pr.deletions,
        "comment_count": pr.comment_count + pr.review_comment_count,
        "comment_authors": authors,
    }


def _format_existing_conversation(repo: str, number: int) -> str:
    """Render the PR's existing comment thread (top-level + line comments,
    including replies), tagged with comment IDs, so the model can see what's
    already been discussed and — for line comments — reply into the same
    thread instead of re-raising it or bolting on a disconnected new comment."""
    issue_comments = get_pr_issue_comments(repo, number)
    review_comments = get_pr_review_comments(repo, number)
    if not issue_comments and not review_comments:
        return ""

    entries = []
    for c in issue_comments:
        entries.append((c["created_at"], f"**{c['user']['login']}** (general): {c['body']}"))
    for c in review_comments:
        reply_tag = f" (reply to #{c['in_reply_to_id']})" if c.get("in_reply_to_id") else ""
        loc = f"{c.get('path')}:{c.get('line') or c.get('original_line')}"
        header = f"**{c['user']['login']}** on {loc}, comment id `{c['id']}`{reply_tag}"
        entries.append((c["created_at"], f"{header}: {c['body']}"))
    entries.sort(key=lambda e: e[0])
    return "\n".join(f"- {body}" for _, body in entries)


def _build_user_msg(repo: str, pr: PRInfo, diff_section: str) -> str:
    conversation = _format_existing_conversation(repo, pr.number)
    user_msg = (
        f"## PR #{pr.number}: {pr.title}\n\n"
        f"**Author:** {pr.author}\n"
        f"**Branch:** `{pr.head_branch}` → `{pr.base_branch}`\n\n"
        f"### Description\n{pr.body or '(none)'}\n\n"
    )
    if conversation:
        user_msg += (
            "### Existing conversation on this PR (chronological, with comment ids)\n"
            "Read this thread carefully before looking at the diff for new issues. "
            "Do not re-raise anything already covered here — replied-to or "
            "discussed points are handled, even if you'd word it differently.\n\n"
            f"{conversation}\n\n"
        )
    return user_msg + diff_section


def review_pr(
    repo_dir: Path,
    repo: str,
    pr: PRInfo,
    model: str,
    custom_prompt: str | None,
    interactive: bool = True,
    level: int = hardness.DEFAULT,
) -> dict:
    """Call Claude to review the PR. Returns parsed JSON response.

    PRs too large for GitHub's diff endpoint (406) fall back to a local git
    diff; `interactive` gates the explore-mode question there (auto mode has
    no prompts, so it enables explore mode directly).

    `level` is the hardness setting: at the top level the review additionally
    runs against a read-only checkout, so Claude can read around the diff."""
    try:
        diff = get_pr_diff(repo, pr.number)
    except DiffTooLargeError:
        return _review_huge_pr(repo_dir, repo, pr, model, custom_prompt, interactive, level)

    system_prompt = _build_review_prompt(repo_dir, custom_prompt, pr, level)
    user_msg = _build_user_msg(repo, pr, f"### Diff\n```diff\n{diff}\n```")
    if hardness.explores(level):
        raw = _ask_in_checkout(repo_dir, repo, pr, system_prompt, user_msg, model)
    else:
        raw = claude_client.ask(system_prompt, user_msg, model=model)
    return claude_client.extract_json(raw)


def _ask_in_checkout(
    repo_dir: Path, repo: str, pr: PRInfo, system_prompt: str, user_msg: str, model: str
) -> str:
    """Run the review from a temporary read-only checkout of the PR head, so
    Claude can Read/Glob/Grep the code the diff sits in.

    The checkout is a convenience, not the review: if the PR head cannot be
    fetched (no network, no access to the fork), fall back to reviewing the
    diff on its own rather than failing."""
    try:
        fetch_pr_refs(repo_dir, repo, pr)
        worktree = add_pr_worktree(repo_dir, pr.head_sha)
    except Exception as e:
        print(_c(f"  · no checkout to explore ({e}); reviewing the diff alone.", DIM))
        return claude_client.ask(system_prompt, user_msg, model=model)

    print(_c("  Reading the repo around the diff...", DIM))
    try:
        return claude_client.ask(
            system_prompt + prompts.HARDNESS_EXPLORE_ADDENDUM,
            user_msg,
            model=model,
            timeout=claude_client.EXPLORE_TIMEOUT_S,
            tools=claude_client.READ_ONLY_TOOLS,
            deny=claude_client.SECRET_DENY_RULES,
            cwd=worktree,
        )
    finally:
        remove_pr_worktree(repo_dir, worktree)


DIFF_FILE = "PRALINE_DIFF.patch"


def _review_huge_pr(
    repo_dir: Path,
    repo: str,
    pr: PRInfo,
    model: str,
    custom_prompt: str | None,
    interactive: bool,
    level: int = hardness.DEFAULT,
) -> dict:
    """Review a PR whose diff GitHub refuses to render (406, ~300+ files).

    The diff comes from local git instead, which has no size limit. In explore
    mode the PR head is additionally checked out in a temporary worktree and
    Claude navigates it with read-only tools, paging through the diff from a
    scratch file, so the review survives diffs that would not fit in context."""
    print(_c(f"⚠ Huge PR, careful: #{pr.number} exceeds GitHub's diff API limits.", YELLOW))
    print(_c("  Falling back to a local git diff (fetching the PR head from origin)...", DIM))
    fetch_pr_refs(repo_dir, repo, pr)
    diff = get_pr_diff_local(repo_dir, pr)
    diffstat = get_pr_diffstat_local(repo_dir, pr)
    # The last --stat line reads "N files changed, X insertions(+), Y deletions(-)".
    # PRInfo counts can't be used here: they are 0 unless the PR came from get_pr.
    print(f"  {diffstat.splitlines()[-1].strip()}")
    print(f"  Diff size: {len(diff.splitlines())} lines ({len(diff) // 1024} KB)")

    if hardness.explores(level):
        print(_c(f"  Depth {hardness.label(level)}: exploring the checkout anyway.", DIM))
        explore = True
    elif interactive:
        print(
            "  A diff this size may not fit the model's context in one prompt. In explore\n"
            "  mode, Claude instead reviews from a temporary read-only checkout of the PR\n"
            "  (Read/Glob/Grep only, credential files denied, no shell, no network) and\n"
            "  pages through the diff from a file. Declining inlines the full diff instead."
        )
        explore = confirm("Enable explore mode?")
    else:
        print(_c("  Auto mode: enabling explore mode (nobody around to ask).", DIM))
        explore = True

    if not explore:
        user_msg = _build_user_msg(repo, pr, f"### Diff\n```diff\n{diff}\n```")
        system_prompt = _build_review_prompt(repo_dir, custom_prompt, pr, level)
        raw = claude_client.ask(system_prompt, user_msg, model=model)
        return claude_client.extract_json(raw)

    worktree = add_pr_worktree(repo_dir, pr.head_sha)
    try:
        (worktree / DIFF_FILE).write_text(diff)
        diff_section = (
            f"### Diff stat\n```\n{diffstat}\n```\n\n"
            f"### Diff\nToo large to include here. The full diff is in `{DIFF_FILE}` at the "
            "checkout root; read it in slices as instructed."
        )
        system_prompt = _build_review_prompt(repo_dir, custom_prompt, pr, level)
        system_prompt += prompts.HUGE_PR_EXPLORE_ADDENDUM
        print(_c("  Explore mode: Claude is reviewing from a temporary checkout...", DIM))
        raw = claude_client.ask(
            system_prompt,
            _build_user_msg(repo, pr, diff_section),
            model=model,
            timeout=claude_client.EXPLORE_TIMEOUT_S,
            tools=claude_client.READ_ONLY_TOOLS,
            deny=claude_client.SECRET_DENY_RULES,
            cwd=worktree,
        )
    finally:
        remove_pr_worktree(repo_dir, worktree)
    return claude_client.extract_json(raw)


def _location(c: dict) -> str:
    """`path/to/file.py:12` or `path/to/file.py:12-15` for a range."""
    if not c.get("file"):
        return ""
    if not c.get("line"):
        return c["file"]
    start = c.get("start_line")
    span = f"{start}-{c['line']}" if start and start < c["line"] else str(c["line"])
    return f"{c['file']}:{span}"


def has_suggestion(c: dict) -> bool:
    """Whether the comment carries a one-click GitHub suggestion block."""
    return "```suggestion" in c.get("body", "")


def _quote(text: str, width: int = 76, max_lines: int = 3) -> str:
    wrapped = textwrap.wrap(text.strip().replace("\n", " "), width=width)
    if len(wrapped) > max_lines:
        wrapped = wrapped[:max_lines]
        wrapped[-1] = wrapped[-1].rstrip() + "…"
    return "\n".join(f"  │ {line}" for line in wrapped)


def _display_comment(idx: int, total: int, c: dict, comment_lookup: dict | None = None) -> None:
    label = SEVERITY_LABEL.get(c.get("severity", "nit"), "💬")
    location = _location(c)
    print(f"\n{'─'*60}")
    if c.get("reply_to_id"):
        resolved_tag = "  ✓ marks resolved" if c.get("resolved") else ""
        print(f"[{idx}/{total}] {label} replying to comment #{c['reply_to_id']}{resolved_tag}")
        original = (comment_lookup or {}).get(c["reply_to_id"])
        if original:
            loc = original.get("path") or ""
            if original.get("line"):
                loc += f":{original['line']}"
            print(f"  context: @{original['author']} {('on ' + loc) if loc else ''}:")
            print(_quote(original["body"]))
        print()
    else:
        suggests = _c("  🔧 with suggestion", DIM) if has_suggestion(c) else ""
        print(f"[{idx}/{total}] {label}  {location}{suggests}")
    # A suggestion block is code: wrapping it would break the exact indentation
    # GitHub commits, so bodies carrying one are printed as written.
    body = c["body"]
    print(body if has_suggestion(c) else textwrap.fill(body, width=80, subsequent_indent="  "))
    print()


def _prompt_action(c: dict) -> tuple[str, str]:
    """
    Ask the user: accept / reject / edit.
    Returns (action, final_body) where action is 'accept' | 'reject'.
    """
    while True:
        choice = input("  [a]ccept  [r]eject  [e]dit  > ").strip().lower()
        if choice in ("a", "accept"):
            return "accept", c["body"]
        if choice in ("r", "reject"):
            return "reject", c["body"]
        if choice in ("e", "edit"):
            print("  Enter new comment (blank line to finish):")
            lines = []
            while True:
                line = input("  > ")
                if line == "":
                    break
                lines.append(line)
            new_body = "\n".join(lines).strip()
            if new_body:
                return "accept", new_body
            print("  (empty, keeping the original)")


def run_approval_loop(review: dict, comment_lookup: dict | None = None) -> list[dict]:
    """
    Show all comments first, then go one by one. The overall summary is
    included as the first item — if accepted, it's posted as the top-level
    PR comment; the rest post as line/general review comments or, for
    `reply` items, as a reply into the original thread (see comment_lookup,
    which supplies the original comment's author/body for on-screen context).
    Returns list of accepted comments (with final body).
    """
    all_items = flatten_review(review)
    print(f"\n{'═'*60}")
    print(f"PRaline's verdict: {status_label(review)}")
    if not all_items:
        print("No comments to review.")
        return []

    print("OVERVIEW: all proposed comments")
    print(f"{'═'*60}")
    for i, c in enumerate(all_items, 1):
        label = SEVERITY_LABEL.get(c.get("severity", "nit"), "💬")
        loc = _location(c)
        if has_suggestion(c):
            loc += " 🔧"
        if c.get("reply_to_id"):
            original = (comment_lookup or {}).get(c["reply_to_id"])
            who = f" to @{original['author']}" if original else ""
            resolved_tag = " ✓resolves" if c.get("resolved") else ""
            loc = f"↩ reply{who} on #{c['reply_to_id']}{resolved_tag}"
        print(f"  {i:2}. {label}  {loc or '(general)'}")
        # One line in the overview, so a body with a suggestion block in it
        # doesn't spill its newlines across the list.
        preview = " ".join(c["body"].split())
        print(f"      {preview[:80]}{'…' if len(preview) > 80 else ''}")

    print(f"\n{'═'*60}")
    print("REVIEW: comment by comment")
    print(f"{'═'*60}")

    accepted = []
    for i, c in enumerate(all_items, 1):
        _display_comment(i, len(all_items), c, comment_lookup)
        action, final_body = _prompt_action(c)
        if action == "accept":
            accepted.append({**c, "body": final_body})
            print("  ✓ accepted")
        else:
            print("  ✗ rejected")

    return accepted


def post_accepted_comments(repo: str, pr: PRInfo, accepted: list[dict]) -> None:
    if not accepted:
        print("Nothing to post.")
        return

    print(f"\nPosting {len(accepted)} comment(s) to PR #{pr.number}...")
    for c in accepted:
        file_ = c.get("file")
        line_ = c.get("line")
        reply_to_id = c.get("reply_to_id")
        body = c["body"]
        try:
            if reply_to_id:
                post_pr_review_reply(repo, pr.number, reply_to_id, body)
                print(f"  ✓ reply to comment #{reply_to_id}")
                if c.get("resolved"):
                    try:
                        thread_id = find_review_thread_id(repo, pr.number, reply_to_id)
                        if thread_id:
                            resolve_review_thread(thread_id)
                            print(f"    ✓ marked thread #{reply_to_id} resolved")
                    except Exception as e:
                        print(f"    ✗ could not resolve thread: {e}")
            elif file_ and line_:
                start = c.get("start_line")
                post_pr_review_comment(
                    repo, pr.number, body, file_, line_, pr.head_sha, start_line=start
                )
                span = f"{start}-{line_}" if start and start < line_ else str(line_)
                print(f"  ✓ line comment on {file_}:{span}")
            else:
                post_pr_comment(repo, pr.number, body)
                print("  ✓ general comment")
        except Exception as e:
            print(f"  ✗ failed: {e}")
