"""Non-interactive auto-review mode.

Scans open PRs, keeps the ones with activity since this user's last auto run
(plus any PR numbers passed explicitly), and posts every proposed comment
without an approval loop. The only prompt is a one-time confirmation if the
total changed-file count crosses --max-changed-files.
"""

from pathlib import Path

from . import config, watch
from .github import (
    PRInfo,
    get_pr,
    get_pr_issue_comments,
    get_pr_review_comments,
    list_open_prs,
)
from .reviewer import log_review, post_accepted_comments, rerequest_review, review_pr
from .slack import notify_digest, notify_review
from .term import BOLD, DIM, GREEN, MAGENTA, RED, YELLOW, _c, _rule, confirm
from .verdict import count_items, flatten_review, reviewed_entry, status_label


def _has_new_activity(repo: str, pr: PRInfo, since: str) -> bool:
    """Has anything touched this PR since `since`?

    The PR's own updated_at answers yes cheaply for the common case (a new
    commit, a new comment, a title edit). Only when it says no do we pay for
    the two comment endpoints, which catch the case it misses: a comment
    edited in place without the PR itself being bumped."""
    if pr.updated_at > since:
        return True
    return any(
        c["created_at"] > since or c.get("updated_at", c["created_at"]) > since
        for c in get_pr_issue_comments(repo, pr.number) + get_pr_review_comments(repo, pr.number)
    )


def select_prs(repo: str, repo_dir: Path, explicit: list[int]) -> tuple[list[PRInfo], list[int]]:
    """Open PRs to auto-review.

    If `explicit` is given, ONLY those PR numbers are reviewed (draft and
    new-activity filters are bypassed for them, but nothing else is added).
    Otherwise, every open non-draft PR with activity since this user's last
    auto run qualifies. Either way the result comes back in watch.order_prs
    order: oldest first, and a stacked PR after the PR it builds on, so a
    stack is reviewed bottom-up instead of in GitHub's newest-first order.
    Returns (selected, explicit-numbers-not-open)."""
    open_prs = list_open_prs(repo)

    if explicit:
        explicit_set = set(explicit)
        selected = [pr for pr in open_prs if pr.number in explicit_set]
        open_numbers = {pr.number for pr in open_prs}
        missing = sorted(n for n in explicit_set if n not in open_numbers)
        return watch.order_prs(selected), missing

    state = config.load_auto_state(repo_dir)
    selected = []
    for pr in open_prs:
        if pr.draft:
            continue
        last_reviewed = state.get(str(pr.number))
        if last_reviewed and not _has_new_activity(repo, pr, last_reviewed):
            continue
        selected.append(pr)

    return watch.order_prs(selected), []


def _confirm_budget(candidates: list[PRInfo], max_changed_files: int) -> bool:
    total_files = sum(pr.changed_files for pr in candidates)
    print(f"\n{_rule()}")
    print(_c("Review order: oldest first, stacked PRs after the PR they build on.", DIM))
    for pr in candidates:
        tag = _c(f"({pr.changed_files} files)", DIM)
        print(f"  {_c(f'#{pr.number:<4}', MAGENTA)} {pr.title}  {tag}")
    print(_rule())
    print(f"Total files changed across {len(candidates)} PR(s): {total_files}")

    if total_files <= max_changed_files:
        return True
    print(
        _c(
            f"\nThis exceeds the configured limit of {max_changed_files} files "
            "(--max-changed-files).",
            YELLOW,
        )
    )
    return confirm("Continue anyway?", default_yes=False)


def _print_summary(results: list[dict]) -> None:
    print(f"\n{_rule('═')}")
    print(_c("AUTO REVIEW SUMMARY", BOLD))
    print(_rule("═"))
    for r in results:
        num = r["number"]
        tag = _c(f"({r['changed_files']} files)", DIM)
        print(f"  {_c(f'#{num:<4}', MAGENTA)} {r['title']}  {tag}")
        if "error" in r:
            print(f"    {_c('failed: ' + r['error'], RED)}")
            continue
        print(f"    {r['status']}")
        print(
            f"    added: {r['comments_added']}   "
            f"left: {r['comments_left']}   "
            f"resolved: {r['comments_resolved']}"
        )
    print(_rule("═"))


def run_auto(run: config.Run, pr_numbers: list[int], max_changed_files: int) -> None:
    repo_dir, repo = run.repo_dir, run.repo
    print(_c("Scanning for PRs to auto-review...", DIM))
    candidates, missing = select_prs(repo, repo_dir, pr_numbers)
    for n in missing:
        print(_c(f"  PR #{n} is not open, skipping.", YELLOW))
    if not candidates:
        print(_c("Nothing to review.", GREEN))
        return

    # The list endpoint omits changed_files/additions/deletions; refetch per PR,
    # keeping the review order select_prs settled on.
    candidates = [get_pr(repo, pr.number) for pr in candidates]

    if not _confirm_budget(candidates, max_changed_files):
        print(_c("Aborted. Nothing reviewed.", DIM))
        return

    state = config.load_auto_state(repo_dir)
    results = []
    reviewed = []
    for pr in candidates:
        print(f"\n{_c(f'Reviewing #{pr.number}', BOLD)}: {pr.title}")
        try:
            review = review_pr(
                repo_dir, repo, pr, model=run.model, custom_prompt=None, interactive=False
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
    _print_summary(results)
