"""PRaline: automated PR review, interactive terminal tool.

Run from anywhere, pointing at the target repo with --dir:
    uv run --project /path/to/git_reviewer praline --dir ~/src/some-repo
    uv run --project /path/to/git_reviewer praline --dir ~/src/some-repo --model opus

Requires a GitHub personal access token in GITHUB_TOKEN or GH_TOKEN
(fine-grained: Contents read-only, Pull requests read/write, Issues read/write —
the last only because top-level PR comments use the issue-comment endpoint).
"""

import argparse
import sys
from pathlib import Path

from . import budget, config, hardness, slack, watch
from .auto import run_auto
from .github import check_auth, infer_repo_slug, is_git_ignored, list_open_prs
from .memory import (
    last_updated_at,
    merged_since_last_update,
    update_knowledge,
    warn_if_no_knowledge,
)
from .monitor import DEFAULT_INTERVAL_S, DEFAULT_TOKENS_PER_HOUR, run_monitor
from .reviewer import (
    build_comment_lookup,
    get_pr_status,
    log_review,
    post_accepted_comments,
    rerequest_review,
    review_pr,
    run_approval_loop,
)
from .term import (
    BOLD,
    CYAN,
    DIM,
    GREEN,
    MAGENTA,
    MASCOT,
    RED,
    RESET,
    YELLOW,
    _c,
    _rule,
    confirm,
    plain,
)
from .verdict import reviewed_entry

BANNER = f"""{CYAN}{BOLD}
   {MASCOT}  PRaline
{RESET}{DIM}   your friendly neighborhood code review chocolate{RESET}"""


def _ask_since_days() -> int:
    default = config.DEFAULT_SINCE_DAYS
    raw = input(_c(f"How many days back should I look at merged PRs? [{default}]: ", CYAN)).strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _do_init_or_update(repo_dir: Path, repo: str, model: str) -> None:
    since_days = _ask_since_days()
    print(f"\n{MASCOT} Let me go dig through the history...")
    md_path, html_path = update_knowledge(repo_dir, repo, model=model, since_days=since_days)
    print(f"\n{_c('✓ Knowledge base ready.', GREEN)}")
    print(f"  markdown: {_c(str(md_path), CYAN)}")
    print(f"  html:     {_c(str(html_path), CYAN)}")
    print(
        _c(
            f"  Tip: ask me (Claude) to publish {html_path.name} "
            "as an artifact so you can read it in the browser.",
            DIM,
        )
    )


def _offer_update_for_new_prs(repo_dir: Path, repo: str, model: str) -> None:
    """Prompt to refresh the knowledge base if PRs were merged since it was built."""
    try:
        prs = merged_since_last_update(repo, repo_dir)
    except Exception as e:
        print(_c(f"Could not check GitHub for newly merged PRs: {e}", YELLOW))
        return
    if not prs:
        return

    shown = ", ".join(f"#{p['number']}" for p in prs[:8])
    if len(prs) > 8:
        shown += ", ..."
    built_on = last_updated_at(repo_dir)
    print(
        _c(
            f"\n{len(prs)} PR(s) merged since the knowledge base was built "
            f"({built_on:%Y-%m-%d %H:%M} UTC): {shown}",
            YELLOW,
        )
    )
    if confirm("Update the knowledge base first?"):
        _do_init_or_update(repo_dir, repo, model)


def _pick_pr(repo_dir: Path, prs):
    changes = watch.classify(repo_dir, prs)
    fresh = sum(1 for c in changes if c.status != watch.SEEN)
    legend = _c(f"   {watch.ICON[watch.NEW]} new   {watch.ICON[watch.UPDATED]} updated", DIM)
    print(f"\n{_c('Open PRs', BOLD)}" + (f"  {legend}" if fresh else ""))
    for change in changes:
        print(f"  {watch.format_line(change)}")
    print(_c("   b) back to main menu", DIM))
    while True:
        raw = input(_c("\nPR number to review: ", CYAN)).strip()
        if raw.lower() == "b":
            return None
        try:
            n = int(raw)
        except ValueError:
            print(_c("  Please enter a number, or 'b' to go back.", YELLOW))
            continue
        match = [p for p in prs if p.number == n]
        if match:
            return match[0]
        print(_c(f"  PR #{n} not in the list above.", YELLOW))


def _show_pr_status(repo: str, pr) -> None:
    print(_c(f"\n{MASCOT} Fetching PR #{pr.number} details from GitHub...", DIM))
    status = get_pr_status(repo, pr.number)
    print(f"\n{_rule()}")
    draft = _c("  [draft, not open for review yet]", YELLOW) if pr.draft else ""
    print(
        f"{_c(f'#{pr.number}', MAGENTA)} {_c(plain(pr.title), BOLD)}  "
        f"{_c(f'by {plain(pr.author)}', DIM)}{draft}"
    )
    additions = _c(f"+{status['additions']}", GREEN)
    deletions = _c(f"-{status['deletions']}", RED)
    print(
        f"  📁 {status['changed_files']} file(s) changed   "
        f"{additions} {deletions}   "
        f"💬 {status['comment_count']} comment(s)"
    )
    if status["comment_authors"]:
        by = ", ".join(f"{plain(u)} ({n})" for u, n in status["comment_authors"].items())
        print(f"  🗣  by: {by}")
    print(_rule())


def _do_view_knowledge(repo_dir: Path) -> None:
    if not config.knowledge_exists(repo_dir):
        print(_c("No knowledge base found yet. Build it first.", YELLOW))
        return
    print(f"\n{_rule('═')}")
    print(_c("REPO KNOWLEDGE", BOLD))
    print(_rule("═"))
    print(config.load_repo_knowledge(repo_dir))
    print(f"\n{_rule('═')}")
    print(_c("PR HISTORY", BOLD))
    print(_rule("═"))
    print(config.load_pr_history(repo_dir))
    print(f"Markdown version: {config.knowledge_md_file(repo_dir)}")
    print(f"HTML version: {config.knowledge_html_file(repo_dir)}")
    artifact_url = config.load_artifact_url(repo_dir)
    if artifact_url:
        print(f"Published artifact: {artifact_url}")


def _load_slack(repo_dir: Path, enabled: bool, reviewer_login: str) -> slack.SlackConfig | None:
    """Resolve the Slack config when --slack was passed. A misconfiguration is
    fatal here rather than silently mid-run: if you asked for notifications,
    you should hear about it before any review starts."""
    if not enabled:
        return None
    try:
        cfg = slack.load_config(repo_dir, reviewer_login=reviewer_login)
        bot = slack.check_auth(cfg)
        print(
            f"{MASCOT} Slack: authenticated as {_c(bot, GREEN)} "
            f"({len(cfg.users)} user mapping(s) from {cfg.source})"
        )
        if not cfg.slack_id_for(reviewer_login):
            print(
                _c(
                    f"  Note: @{reviewer_login} has no Slack mapping, so you won't be added "
                    "to the conversation with the PR author. Add yourself to \"users\".",
                    YELLOW,
                )
            )
        return cfg
    except Exception as e:
        print(_c(f"Slack setup failed: {e}", RED))
        print(
            _c(
                "Expected ~/.config/praline/slack.json with {\"bot_token\": …, "
                '"users": {"github-login": "U0123…"}} — see the README.',
                DIM,
            )
        )
        sys.exit(1)


def _warn_if_praline_dir_committable(repo_dir: Path) -> None:
    """`.praline/` holds the knowledge base, the review log, and possibly a
    Slack config. None of that belongs in a repo, so say so loudly if the
    target repo would happily commit it."""
    if is_git_ignored(repo_dir, ".praline/"):
        return
    print(
        _c(
            "⚠ .praline/ is not gitignored in this repo. It holds the knowledge base, "
            "the review log, and any local Slack config — add `.praline/` to .gitignore "
            "before committing.",
            YELLOW,
        )
    )


def _do_review(run: config.Run, session: list) -> None:
    """One interactive review, start to finish. Appends to `session`, the list
    the Slack round-up is built from when you quit."""
    repo_dir, repo, model = run.repo_dir, run.repo, run.model
    if not config.knowledge_exists(repo_dir):
        print(_c("No knowledge base found yet, so reviews will be less informed.", YELLOW))
        if confirm("Build it now?"):
            _do_init_or_update(repo_dir, repo, model)
    else:
        _offer_update_for_new_prs(repo_dir, repo, model)

    prs = list_open_prs(repo)
    if not prs:
        print(_c("No open PRs found. Nothing to review, enjoy the quiet.", GREEN))
        return

    if len(prs) == 1:
        pr = prs[0]
        print(f"Only one open PR, reviewing #{pr.number}: {plain(pr.title)}")
    else:
        pr = _pick_pr(repo_dir, prs)
        if pr is None:
            return

    _show_pr_status(repo, pr)

    custom_prompt = None
    if confirm("Use a custom review prompt file?", default_yes=False):
        raw_path = input("Path to prompt file: ").strip()
        p = Path(raw_path).expanduser()
        if p.exists():
            custom_prompt = p.read_text()
        else:
            print(_c(f"  Not found: {raw_path}, using the default prompt.", YELLOW))

    print(
        f"\n{MASCOT} Reviewing PR #{pr.number} with {model} "
        f"at depth {hardness.label(run.hardness)}, one moment..."
    )
    try:
        review = review_pr(
            repo_dir, repo, pr, model=model, custom_prompt=custom_prompt, level=run.hardness
        )
    except Exception as e:
        print(_c(f"Review failed: {e}", RED))
        return

    comment_lookup = build_comment_lookup(repo, pr.number)
    accepted = run_approval_loop(review, comment_lookup)
    if not accepted:
        print(_c("\nNo comments accepted. Nothing posted.", DIM))
        return

    print(f"\n{_c(f'{len(accepted)} comment(s) accepted.', GREEN)}")
    if not confirm("Post to GitHub under your account?"):
        print(_c("Aborted. Nothing posted.", DIM))
        return

    post_accepted_comments(repo, pr, accepted)
    # Logged before the optional extras so the memory survives either failing.
    log_review(repo_dir, pr, review, accepted)
    session.append(reviewed_entry(pr, review, accepted))
    if run.request_review:
        rerequest_review(repo, pr, run.reviewer_login)
    if run.slack is not None and confirm(f"Ping @{pr.author} on Slack about it?"):
        slack.notify_review(run.slack, repo, pr, accepted, review)


def _ask_hardness(run: config.Run) -> None:
    """Change how hard reviews look, for the rest of this sitting."""
    print(f"\n{_c('Review depth', BOLD)}")
    for level in hardness.LEVELS.values():
        marker = _c(" ← current", GREEN) if level.value == run.hardness else ""
        print(f"  [{level.value}] {_c(level.name, CYAN)}: {level.blurb}{marker}")
    raw = input(_c(f"\nDepth [{run.hardness}]: ", CYAN)).strip()
    if not raw:
        return
    try:
        run.hardness = hardness.clamp(int(raw))
    except ValueError:
        print(_c("  Not a number, leaving it as it was.", YELLOW))
        return
    print(_c(f"  Depth set to {hardness.label(run.hardness)}.", GREEN))


def _main_menu(run: config.Run) -> None:
    repo_dir, repo, model = run.repo_dir, run.repo, run.model
    session: list[dict] = []  # PRs reviewed in this sitting, for the Slack round-up
    while True:
        print(f"\n{_rule('═')}")
        print(BANNER)
        print(_rule('═'))
        print(f"  dir: {repo_dir}")
        print(f"  repo: {_c(repo, MAGENTA)}   model: {_c(model, CYAN)}")
        print(
            "  knowledge base: "
            + (
                _c("found", GREEN)
                if config.knowledge_exists(repo_dir)
                else _c("not built yet", YELLOW)
            )
        )
        print(f"  review depth: {_c(hardness.label(run.hardness), CYAN)}")
        print()
        print("  [1] 🔍 Review a PR")
        print(
            "  [2] 🧠 "
            + ("Update" if config.knowledge_exists(repo_dir) else "Build")
            + " knowledge base"
        )
        print("  [3] 📖 View knowledge base")
        print("  [4] 🆕 What's new since last time")
        print("  [5] 🎚  Change review depth")
        print("  [q] 👋 Quit")
        choice = input(_c("\n> ", CYAN)).strip().lower()

        if choice in ("1", "review"):
            _do_review(run, session)
        elif choice in ("2", "init", "update"):
            _do_init_or_update(repo_dir, repo, model)
        elif choice in ("3", "view"):
            _do_view_knowledge(repo_dir)
        elif choice in ("4", "check", "new"):
            watch.run_check(repo_dir, repo, mark=None)
        elif choice in ("5", "depth", "hardness"):
            _ask_hardness(run)
        elif choice in ("q", "quit", "exit"):
            slack.notify_digest(run.slack, repo, session)
            print(f"\n{MASCOT} Bye!")
            return
        else:
            print(_c("  Unrecognized choice.", YELLOW))


def _add_common_args(parser: argparse.ArgumentParser, subcommand: bool = False) -> None:
    """Add the flags every mode accepts.

    They are declared on the top-level parser and on each subparser, so both
    `praline --dir X check` and `praline check --dir X` work. On a subparser the
    defaults must be SUPPRESS: argparse applies a subparser's defaults *after*
    the top-level parse, so a real default there would silently overwrite the
    value the user already gave before the subcommand."""
    def default(value):
        return argparse.SUPPRESS if subcommand else value

    parser.add_argument(
        "--dir",
        default=default("."),
        help="Path to the target git repo to review (default: current directory)",
    )
    parser.add_argument(
        "--model",
        default=default(config.DEFAULT_MODEL),
        help=f"Claude model alias to use (default: {config.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--repo",
        default=default(None),
        help="GitHub repo in OWNER/REPO format (default: inferred from --dir's git remote)",
    )
    parser.add_argument(
        "--no-request-review",
        action="store_true",
        default=default(False),
        help="Don't put you back on the PR's reviewer list after a review is posted",
    )
    parser.add_argument(
        "--slack",
        action="store_true",
        default=default(False),
        help="Notify PR authors on Slack once their PR has been reviewed. Needs a bot "
        "token and a github->slack user mapping (~/.config/praline/slack.json)",
    )
    parser.add_argument(
        "--tokens-per-hour",
        type=int,
        default=default(None),
        metavar="N",
        help="Cap model spending at N tokens per rolling hour, counted across restarts "
        f"(0 = no cap). Monitor mode defaults to {DEFAULT_TOKENS_PER_HOUR:,}; every other "
        "mode is uncapped unless you pass this. Counts fresh input + cache writes + "
        "output, not cached reads",
    )
    parser.add_argument(
        "--hardness",
        "-H",
        type=int,
        default=default(hardness.DEFAULT),
        metavar="N",
        help=f"How hard to look at the diff: {hardness.choices_help()} "
        f"(default: {hardness.DEFAULT})",
    )


def _add_review_trigger_arg(parser: argparse.ArgumentParser) -> None:
    """The push trigger, shared by the two modes that select PRs themselves."""
    parser.add_argument(
        "--review-new-commits",
        action="store_true",
        help="Also re-review a PR when it gets new commits. Off by default: only a "
        "PR that is newly open, or that someone has commented on, starts a review",
    )


def main():
    parser = argparse.ArgumentParser(
        prog="praline",
        description="Interactive, automated PR review powered by Claude Code.",
    )
    _add_common_args(parser)
    subparsers = parser.add_subparsers(dest="command")

    auto_parser = subparsers.add_parser(
        "auto",
        help="Non-interactive mode: review open PRs with new activity and post all comments",
    )
    _add_common_args(auto_parser, subcommand=True)
    auto_parser.add_argument(
        "pr_numbers",
        nargs="*",
        type=int,
        metavar="PR",
        help="Specific PR number(s) to review, bypassing the draft and new-activity filters",
    )
    _add_review_trigger_arg(auto_parser)
    auto_parser.add_argument(
        "--max-changed-files",
        type=int,
        default=config.DEFAULT_MAX_CHANGED_FILES,
        help=f"Prompt for confirmation above this total changed-file count across all "
        f"selected PRs (default: {config.DEFAULT_MAX_CHANGED_FILES})",
    )

    monitor_parser = subparsers.add_parser(
        "monitor",
        help="Keep watching: review PRs as they are opened or updated, unattended",
    )
    _add_common_args(monitor_parser, subcommand=True)
    monitor_parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_S,
        metavar="SECONDS",
        help=f"How long to wait between looks (default: {DEFAULT_INTERVAL_S})",
    )
    monitor_parser.add_argument(
        "--max-changed-files",
        type=int,
        default=config.DEFAULT_MAX_CHANGED_FILES,
        help=f"Skip a batch of PRs larger than this, leaving them for a human "
        f"(default: {config.DEFAULT_MAX_CHANGED_FILES})",
    )
    _add_review_trigger_arg(monitor_parser)
    monitor_parser.add_argument(
        "--no-knowledge-refresh",
        action="store_true",
        help="Don't rebuild the knowledge base when PRs are merged while watching",
    )
    monitor_parser.add_argument(
        "--once",
        action="store_true",
        help="Do a single pass and exit, rather than looping",
    )

    check_parser = subparsers.add_parser(
        "check",
        help="Just look: list open PRs opened or updated since the last PRaline check",
    )
    _add_common_args(check_parser, subcommand=True)
    check_parser.add_argument(
        "--no-mark",
        action="store_true",
        help="Report what's new without acknowledging it, so the next check reports it again",
    )

    args = parser.parse_args()

    repo_dir = Path(args.dir).expanduser().resolve()
    if not repo_dir.is_dir():
        print(_c(f"Not a directory: {repo_dir}", RED))
        sys.exit(1)
    if not (repo_dir / ".git").exists():
        print(_c(f"Not a git repo: {repo_dir}", RED))
        sys.exit(1)

    try:
        repo = args.repo or infer_repo_slug(repo_dir)
    except Exception as e:
        print(_c(f"Could not determine GitHub owner/repo: {e}", RED))
        print("Pass --repo owner/repo explicitly.")
        sys.exit(1)

    _warn_if_praline_dir_committable(repo_dir)

    try:
        username = check_auth()
        print(f"{MASCOT} GitHub: authenticated as {_c(f'@{username}', GREEN)}")
    except Exception as e:
        print(_c(f"GitHub auth failed: {e}", RED))
        sys.exit(1)

    if args.command == "check":
        watch.run_check(repo_dir, repo, mark=not args.no_mark)
        return

    run = config.Run(
        repo_dir=repo_dir,
        repo=repo,
        model=args.model,
        reviewer_login=username,
        request_review=not args.no_request_review,
        slack=_load_slack(repo_dir, args.slack, username),
        hardness=hardness.clamp(args.hardness),
    )

    # Unset means "no cap", except in monitor mode, which is the one that runs
    # unattended for hours and so is the one that must not be uncapped by
    # default. An explicit 0 turns it off anywhere.
    tokens_per_hour = args.tokens_per_hour
    if tokens_per_hour is None:
        tokens_per_hour = DEFAULT_TOKENS_PER_HOUR if args.command == "monitor" else 0
    if tokens_per_hour > 0:
        budget.activate(
            budget.Budget.load(config.budget_file(repo_dir), limit=tokens_per_hour)
        )
        print(f"{MASCOT} Budget: {_c(budget.ACTIVE.summary(), CYAN)}")

    # Both unattended modes review whatever they are pointed at; the interactive
    # menu offers to build instead, so it does not need this.
    if args.command in ("auto", "monitor"):
        warn_if_no_knowledge(repo_dir)

    if args.command == "auto":
        run_auto(
            run,
            pr_numbers=args.pr_numbers,
            max_changed_files=args.max_changed_files,
            on_new_commits=args.review_new_commits,
        )
    elif args.command == "monitor":
        run_monitor(
            run,
            interval_s=args.interval,
            max_changed_files=args.max_changed_files,
            refresh_knowledge=not args.no_knowledge_refresh,
            on_new_commits=args.review_new_commits,
            once=args.once,
        )
    else:
        _main_menu(run)


if __name__ == "__main__":
    main()
