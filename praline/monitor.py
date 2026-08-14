"""Monitor mode: keep watching a repo and review PRs as they arrive.

One loop around `auto.run_auto`, which already reviews exactly the PRs with
activity since the last pass. The loop adds the four things that turn a one-shot
command into something you can leave running for a day:

  - **It survives failures.** GitHub briefly unreachable, a malformed model
    reply, a PR that fails to review — none of those may end the watch.
    Anything unexpected is reported and the next pass tries again.
  - **It starts each pass from a clean view.** The comment cache is emptied,
    because a cache scoped to one look at a PR is wrong for a process that
    looks a hundred times (see github.forget_all_comments).
  - **It keeps the knowledge base current**, refreshing it when PRs have been
    merged since it was last built, so reviews do not drift out of date.
  - **It respects a budget**, waiting for the window to roll forward rather
    than spending on, and stops cleanly on Ctrl-C.

Monitor mode posts comments without an approval loop, exactly like `praline
auto`, because there is nobody sitting there to approve them. Everything the
README says about auto mode and untrusted PR content applies here, and more so:
this one runs unattended for hours.
"""

import time
from datetime import datetime, timezone

from . import budget, config, github, hardness, memory
from .auto import run_auto
from .term import BOLD, CYAN, DIM, GREEN, MAGENTA, RED, YELLOW, _c, _rule

DEFAULT_INTERVAL_S = 300
MIN_INTERVAL_S = 60

# How long to sleep after an unexpected failure, rather than hammering a
# service that is already unhappy at the polling interval.
ERROR_BACKOFF_S = 120

# How often to ask GitHub whether anything has been merged. A knowledge base
# goes stale over days, not minutes, and the question costs a paginated API call,
# so asking once an hour rather than once a pass is twelve times less traffic for
# the same freshness.
KNOWLEDGE_CHECK_S = 3600

# What a monitor may spend per hour when the user does not say.
#
# One review of a small PR at depth 0 measured 12.3k billable tokens (~$0.08 of
# Sonnet). This allows roughly three an hour, or about sixteen over a five-hour
# session for around $1.30 — a modest slice of a Pro plan, and PRs rarely arrive
# faster than that. Anything left over is not dropped: it requalifies on the
# next pass, so a busy hour spreads out instead of overspending.
DEFAULT_TOKENS_PER_HOUR = 40_000


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _sleep(seconds: int, reason: str) -> None:
    """Wait, having said how long for and why."""
    mins, secs = divmod(max(1, seconds), 60)
    print(_c(f"  {reason}. Next look in {mins}m{secs:02d}s.", DIM))
    time.sleep(seconds)


def _refresh_knowledge(run: config.Run) -> str:
    """Rebuild the knowledge base if PRs have merged since it was last built.

    The module map is deliberately not redrawn here. It is the most expensive
    call PRaline makes and the shape of a codebase moves far more slowly than
    its merged PRs, so an unattended refresh would spend most of the budget on
    the part that changed least. Redraw it from the menu or the MCP tool."""
    if not config.knowledge_exists(run.repo_dir):
        return ""
    merged = memory.merged_since_last_update(run.repo, run.repo_dir)
    if not merged:
        return ""
    shown = ", ".join(f"#{p['number']}" for p in merged[:5])
    if len(merged) > 5:
        shown += ", …"
    print(_c(f"  {len(merged)} PR(s) merged since the last build ({shown}), refreshing.", CYAN))
    memory.update_knowledge(run.repo_dir, run.repo, model=run.model, with_graph=False)
    return f"knowledge base refreshed for {len(merged)} merged PR(s)"


def _report(passes: int, results: list[dict], note: str) -> str:
    """One line saying what this pass did, and the reason to print next."""
    if not results and not note:
        print(_c(f"  Nothing new. {passes} pass(es) so far, all quiet.", DIM))
        return "nothing new"

    done = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]
    if done:
        added = sum(r.get("comments_added", 0) for r in done)
        replies = sum(r.get("comments_left", 0) for r in done)
        prs = ", ".join(f"#{r['number']}" for r in done)
        print(
            _c(
                f"  ✓ reviewed {len(done)} PR(s) ({prs}): "
                f"{added} comment(s), {replies} reply(ies) posted.",
                GREEN,
            )
        )
    if failed:
        print(_c(f"  ✗ {len(failed)} PR(s) failed; they requalify next pass.", YELLOW))
    if note:
        print(_c(f"  · {note}", DIM))
    return f"{len(done)} PR(s) reviewed" if done else "pass done"


def run_monitor(
    run: config.Run,
    interval_s: int = DEFAULT_INTERVAL_S,
    max_changed_files: int = config.DEFAULT_MAX_CHANGED_FILES,
    refresh_knowledge: bool = True,
    once: bool = False,
) -> None:
    interval_s = max(MIN_INTERVAL_S, interval_s)
    print(f"\n{_rule('═')}")
    print(_c("PRaline is watching", BOLD))
    print(_rule("═"))
    print(f"  repo:     {_c(run.repo, MAGENTA)}")
    print(f"  every:    {interval_s // 60}m{interval_s % 60:02d}s   model: {_c(run.model, CYAN)}")
    print(f"  depth:    {_c(hardness.label(run.hardness), CYAN)}")
    print(f"  slack:    {'on' if run.slack is not None else 'off'}")
    if not config.knowledge_exists(run.repo_dir):
        knowledge = "none yet — see the warning above"
    else:
        knowledge = "refreshed as PRs merge" if refresh_knowledge else "left alone"
    print(f"  knowledge: {knowledge}")
    if budget.ACTIVE is not None:
        print(f"  budget:   {_c(budget.ACTIVE.summary(), CYAN)}")
    else:
        print(_c("  budget:   none, runs until you stop it (--tokens-per-hour caps it)", YELLOW))
    print(_c("  Drafts are skipped. Reviews post with no approval step. Ctrl-C to stop.", YELLOW))

    passes = reviewed = 0
    # Far enough in the past that the first pass always checks.
    last_knowledge_check = float("-inf")
    try:
        while True:
            passes += 1
            print(f"\n{_rule()}  {_c(f'pass {passes}  {_stamp()}', BOLD)}")
            # A cache built for one look at a PR is wrong for the hundredth.
            github.forget_all_comments()

            wait, reason, note = interval_s, "", ""
            try:
                due = time.monotonic() - last_knowledge_check > KNOWLEDGE_CHECK_S
                if refresh_knowledge and due:
                    last_knowledge_check = time.monotonic()
                    note = _refresh_knowledge(run)
                results = run_auto(run, [], max_changed_files, attended=False)
                reviewed += len([r for r in results if "error" not in r])
                reason = _report(passes, results, note)
            except budget.BudgetExceeded as e:
                # Selecting PRs can spend nothing and still hit the cap, when
                # the previous pass used the last of it.
                print(_c(f"  {e}", YELLOW))
                wait, reason = max(interval_s, e.retry_after_s), "budget spent"
            except Exception as e:
                print(_c(f"  pass failed: {e}", RED))
                wait, reason = max(interval_s, ERROR_BACKOFF_S), "backing off after a failure"

            if budget.ACTIVE is not None:
                print(_c(f"  budget: {budget.ACTIVE.summary()}", DIM))
            if once:
                return
            _sleep(wait, reason)
    except KeyboardInterrupt:
        print(f"\n\n{_c('Stopped watching.', GREEN)}")
        print(f"  {passes} pass(es), {reviewed} PR(s) reviewed.")
        if budget.ACTIVE is not None:
            print(f"  {budget.ACTIVE.summary()}")
