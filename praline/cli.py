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

from . import config, slack, watch
from .auto import run_auto
from .github import check_auth, infer_repo_slug, is_git_ignored, list_open_prs
from .memory import last_updated_at, merged_since_last_update, update_knowledge
from .reviewer import (
    build_comment_lookup,
    get_pr_status,
    log_review,
    post_accepted_comments,
    rerequest_review,
    review_pr,
    run_approval_loop,
)
from .term import BOLD, CYAN, DIM, GREEN, MAGENTA, MASCOT, RED, RESET, YELLOW, _c, _rule, confirm
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
    print(f"{_c(f'#{pr.number}', MAGENTA)} {_c(pr.title, BOLD)}  {_c(f'by {pr.author}', DIM)}")
    additions = _c(f"+{status['additions']}", GREEN)
    deletions = _c(f"-{status['deletions']}", RED)
    print(
        f"  📁 {status['changed_files']} file(s) changed   "
        f"{additions} {deletions}   "
        f"💬 {status['comment_count']} comment(s)"
    )
    if status["comment_authors"]:
        by = ", ".join(f"{u} ({n})" for u, n in status["comment_authors"].items())
        print(f"  🗣  by: {by}")
    print(_rule())


def _do_view_knowledge(repo_dir: Path) -> None:
    if not config.knowledge_exists(repo_dir):
        print(_c("No knowledge base found yet — build it first.", YELLOW))
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
        print(_c("No knowledge base found yet — reviews will be less informed.", YELLOW))
        if confirm("Build it now?"):
            _do_init_or_update(repo_dir, repo, model)
    else:
        _offer_update_for_new_prs(repo_dir, repo, model)

    prs = list_open_prs(repo)
    if not prs:
        print(_c("No open PRs found. Nothing to review — enjoy the quiet.", GREEN))
        return

    if len(prs) == 1:
        pr = prs[0]
        print(f"Only one open PR — reviewing #{pr.number}: {pr.title}")
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
            print(_c(f"  Not found: {raw_path} — using default prompt.", YELLOW))

    print(f"\n{MASCOT} Reviewing PR #{pr.number} with {model}, one moment...")
    try:
        review = review_pr(repo_dir, repo, pr, model=model, custom_prompt=custom_prompt)
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
        print()
        print("  [1] 🔍 Review a PR")
        print(
            "  [2] 🧠 "
            + ("Update" if config.knowledge_exists(repo_dir) else "Build")
            + " knowledge base"
        )
        print("  [3] 📖 View knowledge base")
        print("  [4] 🆕 What's new since last time")
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
    auto_parser.add_argument(
        "--max-changed-files",
        type=int,
        default=config.DEFAULT_MAX_CHANGED_FILES,
        help=f"Prompt for confirmation above this total changed-file count across all "
        f"selected PRs (default: {config.DEFAULT_MAX_CHANGED_FILES})",
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
    )

    if args.command == "auto":
        run_auto(run, pr_numbers=args.pr_numbers, max_changed_files=args.max_changed_files)
    else:
        _main_menu(run)


if __name__ == "__main__":
    main()
