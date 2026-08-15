"""Non-interactive auto-review mode.

Scans open PRs, keeps the ones worth reviewing (newly open, or commented on
since the last review, plus any PR numbers passed explicitly), and posts every
proposed comment without an approval loop. The only prompt is a one-time
confirmation if the total changed-file count crosses --max-changed-files.
"""

from pathlib import Path

from . import budget, config, hardness, watch
from .github import (
    PRInfo,
    get_pr,
    get_pr_issue_comments,
    get_pr_review_comments,
    list_open_prs,
)
from .reviewer import log_review, post_accepted_comments, rerequest_review, review_pr
from .slack import notify_digest, notify_review
from .term import BOLD, DIM, GREEN, MAGENTA, RED, YELLOW, _c, _rule, confirm, plain
from .verdict import count_items, flatten_review, reviewed_entry, status_label


def _commented_since(repo: str, pr: PRInfo, since: str) -> bool:
    """Whether anyone has posted or edited a comment on the PR since `since`."""
    return any(
        c["created_at"] > since or c.get("updated_at", c["created_at"]) > since
        for c in get_pr_issue_comments(repo, pr.number) + get_pr_review_comments(repo, pr.number)
    )


def _has_new_activity(repo: str, pr: PRInfo, since: str) -> bool:
    """Has anything at all touched this PR since `since`?

    The PR's own updated_at answers yes cheaply for the common case (a new
    commit, a new comment, a title edit). Only when it says no do we pay for
    the two comment endpoints, which catch the case it misses: a comment
    edited in place without the PR itself being bumped."""
    if pr.updated_at > since:
        return True
    return _commented_since(repo, pr, since)


def _has_new_comments(repo: str, pr: PRInfo, since: str) -> bool:
    """Has anyone *said* anything since `since`?

    The default trigger for both unattended modes. A push is not a reason to
    review again: the author is mid-work, nobody asked, and re-reviewing every
    commit buries the PR in near-identical comments. A comment is someone
    addressing the review, which is worth answering.

    `updated_at` is a cheap negative filter here: a PR nothing has touched cannot
    have been commented on. It is only a filter, never the answer, so a comment
    edited in place without bumping the PR still counts once we look."""
    if pr.updated_at <= since:
        return False
    return _commented_since(repo, pr, since)


def select_prs(
    repo: str, repo_dir: Path, explicit: list[int], on_new_commits: bool = False
) -> tuple[list[PRInfo], list[int]]:
    """Open PRs to auto-review.

    If `explicit` is given, ONLY those PR numbers are reviewed (draft and
    activity filters are bypassed for them, but nothing else is added).

    Otherwise a PR qualifies when it is open, not a draft, and either has never
    been reviewed here or has been commented on since it last was.
    `on_new_commits` widens that to any activity, so a push requalifies a PR
    too. It is off by default: nobody asked for the re-review a push would
    trigger, and an author iterating on a branch would collect a near-identical
    review per commit.

    Either way the result comes back in watch.order_prs order: oldest first, and
    a stacked PR after the PR it builds on, so a stack is reviewed bottom-up
    instead of in GitHub's newest-first order.
    Returns (selected, explicit-numbers-not-open)."""
    open_prs = list_open_prs(repo)

    if explicit:
        explicit_set = set(explicit)
        selected = [pr for pr in open_prs if pr.number in explicit_set]
        open_numbers = {pr.number for pr in open_prs}
        missing = sorted(n for n in explicit_set if n not in open_numbers)
        return watch.order_prs(selected), missing

    triggers = _has_new_activity if on_new_commits else _has_new_comments
    state = config.load_auto_state(repo_dir)
    selected = []
    for pr in open_prs:
        if pr.draft:
            continue
        last_reviewed = state.get(str(pr.number))
        if last_reviewed and not triggers(repo, pr, last_reviewed):
            continue
        selected.append(pr)

    return watch.order_prs(selected), []


def _preview(candidates: list[PRInfo], level: int) -> None:
    """What is about to be reviewed, and in what order."""
    print(f"\n{_rule()}")
    print(_c("Review order: oldest first, stacked PRs after the PR they build on.", DIM))
    for pr in candidates:
        tag = _c(f"({pr.changed_files} files)", DIM)
        print(f"  {_c(f'#{pr.number:<4}', MAGENTA)} {plain(pr.title)}  {tag}")
    print(_rule())
    total = sum(pr.changed_files for pr in candidates)
    print(f"Total files changed across {len(candidates)} PR(s): {total}")
    print(f"Review depth: {hardness.label(level)}")


def _within_file_limit(
    candidates: list[PRInfo], max_changed_files: int, interactive: bool
) -> bool:
    """Whether this batch is small enough to review.

    Over the limit, an interactive run asks; an unattended one declines, so a
    huge batch waits for a human instead of being reviewed by default."""
    total = sum(pr.changed_files for pr in candidates)
    if total <= max_changed_files:
        return True
    print(
        _c(
            f"\n{total} changed files exceeds the configured limit of {max_changed_files} "
            "(--max-changed-files).",
            YELLOW,
        )
    )
    if not interactive:
        print(_c("Nobody to ask, so leaving these for a human.", DIM))
        return False
    return confirm("Continue anyway?", default_yes=False)


def _print_summary(results: list[dict]) -> None:
    print(f"\n{_rule('═')}")
    print(_c("AUTO REVIEW SUMMARY", BOLD))
    print(_rule("═"))
    for r in results:
        num = r["number"]
        tag = _c(f"({r['changed_files']} files)", DIM)
        print(f"  {_c(f'#{num:<4}', MAGENTA)} {plain(r['title'])}  {tag}")
        if "error" in r:
            print(f"    {_c('failed: ' + plain(r['error']), RED)}")
            continue
        print(f"    {r['status']}")
        print(
            f"    added: {r['comments_added']}   "
            f"left: {r['comments_left']}   "
            f"resolved: {r['comments_resolved']}"
        )
    print(_rule("═"))


def run_auto(
    run: config.Run,
    pr_numbers: list[int],
    max_changed_files: int,
    attended: bool = True,
    on_new_commits: bool = False,
) -> list[dict]:
    """Review every qualifying PR and post the comments. Returns the per-PR
    results.

    `attended` says whether a person is reading. It gates the two things that
    only make sense for someone watching one run: the confirmation when a batch
    is over the file limit, and the scan/summary chatter. Monitor mode passes
    False and prints its own per-pass digest instead.

    `on_new_commits` is passed to select_prs: see there for what it changes."""
    repo_dir, repo = run.repo_dir, run.repo
    if attended:
        print(_c("Scanning for PRs to auto-review...", DIM))
    candidates, missing = select_prs(repo, repo_dir, pr_numbers, on_new_commits)
    for n in missing:
        print(_c(f"  PR #{n} is not open, skipping.", YELLOW))
    if not candidates:
        if attended:
            print(_c("Nothing to review.", GREEN))
        return []

    # The list endpoint omits changed_files/additions/deletions; refetch per PR,
    # keeping the review order select_prs settled on.
    candidates = [get_pr(repo, pr.number) for pr in candidates]

    if attended:
        _preview(candidates, run.hardness)
    if not _within_file_limit(candidates, max_changed_files, attended):
        return []

    state = config.load_auto_state(repo_dir)
    results = []
    reviewed = []
    for pr in candidates:
        print(f"\n{_c(f'Reviewing #{pr.number}', BOLD)}: {plain(pr.title)}")
        try:
            budget.guard()
        except budget.BudgetExceeded as e:
            print(_c(f"  {e}", YELLOW))
            print(_c("  Stopping here; the PRs left keep their state and requalify next run.", DIM))
            break

        try:
            review = review_pr(
                repo_dir,
                repo,
                pr,
                model=run.model,
                custom_prompt=None,
                interactive=False,
                level=run.hardness,
            )
        except Exception as e:
            print(_c(f"  Review failed: {e}", RED))
            results.append(
                {
                    "number": pr.number,
                    "title": pr.title,
                    "changed_files": pr.changed_files,
                    "error": str(e),
                }
            )
            continue

        items = flatten_review(review)
        print(f"  verdict: {status_label(review)}")
        if items:
            post_accepted_comments(repo, pr, items)
        # Logged before the notifications so the memory survives a Slack outage.
        log_review(repo_dir, pr, review, items)
        if run.request_review:
            rerequest_review(repo, pr, run.reviewer_login)
        notify_review(run.slack, repo, pr, items, review)
        reviewed.append(reviewed_entry(pr, review, items))
        results.append(
            {
                "number": pr.number,
                "title": pr.title,
                "changed_files": pr.changed_files,
                "status": status_label(review),
                **count_items(items),
            }
        )
        state[str(pr.number)] = config.now_iso()

    config.save_auto_state(repo_dir, state)
    notify_digest(run.slack, repo, reviewed)
    if attended:
        _print_summary(results)
    return results
