"""What changed on GitHub since PRaline last looked.

A tiny bookkeeping layer over the open PR list: it remembers each open PR's
`updated_at` in `.praline/seen_state.json`, so a PR can be classified as

    NEW      — not in the state file at all (opened since the last look)
    UPDATED  — its `updated_at` moved since the last look
    SEEN     — unchanged

The interactive PR list uses this only for display (it never writes state,
so the icons stay put until you acknowledge them); `praline check` prints
the digest and, unless told otherwise, marks everything as seen.
"""

from dataclasses import dataclass
from pathlib import Path

from . import config
from .github import PRInfo, list_open_prs
from .term import BOLD, CYAN, DIM, GREEN, MAGENTA, _c, _rule, confirm

NEW = "new"
UPDATED = "updated"
SEEN = "seen"

ICON = {NEW: "🆕", UPDATED: "🔄", SEEN: "  "}


@dataclass
class PRChange:
    pr: PRInfo
    status: str
    since: str = ""  # the previously seen updated_at, for UPDATED entries
    parent: int | None = None  # the open PR this one is stacked on, if any

    @property
    def icon(self) -> str:
        return ICON[self.status]


def order_prs(prs: list[PRInfo]) -> list[PRInfo]:
    """Sort PRs into the order a human would read them: oldest first, and a
    stacked PR after the PR it is built on.

    A stack shows up in the branch names: PR B is stacked on PR A when B's base
    branch is A's head branch. Reviewing B before A means reviewing changes
    whose foundation you haven't seen yet, so each stack is walked bottom-up
    and depth first, staying contiguous instead of interleaving with unrelated
    PRs. Everywhere else, PR number ascending — chronological, since numbers are
    handed out in order.

    Any PR left over (a dependency cycle, or a base branch belonging to a PR
    that isn't open) falls back to number order, so nothing is ever dropped."""
    ordered: list[PRInfo] = []
    emitted: set[int] = set()
    by_number = sorted(prs, key=lambda p: p.number)
    by_head = _head_index(by_number)

    children: dict[int, list[PRInfo]] = {}
    for pr in by_number:
        parent = by_head.get(pr.base_branch)
        if parent and parent.number != pr.number:
            children.setdefault(parent.number, []).append(pr)

    def emit(pr: PRInfo) -> None:
        """A PR, then whatever is stacked on top of it — depth first, so one
        stack is reviewed end to end before the next unrelated PR starts."""
        if pr.number in emitted:
            return
        emitted.add(pr.number)
        ordered.append(pr)
        for child in children.get(pr.number, []):
            emit(child)

    for pr in by_number:
        if by_head.get(pr.base_branch, pr).number == pr.number:  # bottom of a stack
            emit(pr)
    for pr in by_number:  # anything left over: a cycle, so number order it is
        emit(pr)
    return ordered


def _head_index(prs: list[PRInfo]) -> dict[str, PRInfo]:
    """Head branch -> the open PR that publishes it (lowest number wins)."""
    index: dict[str, PRInfo] = {}
    for pr in sorted(prs, key=lambda p: p.number):
        index.setdefault(pr.head_branch, pr)
    return index


def stack_parents(prs: list[PRInfo]) -> dict[int, int]:
    """PR number -> the number of the open PR it is stacked on, for those that
    are. Built once for the whole list rather than scanned per PR."""
    by_head = _head_index(prs)
    parents = {}
    for pr in prs:
        parent = by_head.get(pr.base_branch)
        if parent and parent.number != pr.number:
            parents[pr.number] = parent.number
    return parents


def classify(repo_dir: Path, prs: list[PRInfo]) -> list[PRChange]:
    """Tag each PR as NEW / UPDATED / SEEN against the stored state, in
    order_prs order.

    On the very first run there is no state, so everything is NEW — correct,
    if noisy exactly once."""
    known = config.load_seen_state(repo_dir).get("prs", {})
    parents = stack_parents(prs)
    changes = []
    for pr in order_prs(prs):
        entry = known.get(str(pr.number))
        if entry is None:
            status, since = NEW, ""
        elif pr.updated_at and pr.updated_at > entry.get("updated_at", ""):
            status, since = UPDATED, entry.get("updated_at", "")
        else:
            status, since = SEEN, ""
        changes.append(PRChange(pr, status, since=since, parent=parents.get(pr.number)))
    return changes


def is_first_look(repo_dir: Path) -> bool:
    """True until the first check is recorded — `prs` alone can't say, since a
    repo with no open PRs legitimately stores an empty map."""
    return not config.load_seen_state(repo_dir).get("last_checked")


def mark_seen(repo_dir: Path, prs: list[PRInfo]) -> None:
    """Record the current state of the open PRs. PRs that are no longer open
    are dropped, so a reopened PR shows up as NEW again — which is what you
    want to be told about."""
    state = config.load_seen_state(repo_dir)
    state["last_checked"] = config.now_iso()
    state["prs"] = {
        str(pr.number): {
            "title": pr.title,
            "author": pr.author,
            "created_at": pr.created_at,
            "updated_at": pr.updated_at,
        }
        for pr in prs
    }
    config.save_seen_state(repo_dir, state)


def last_checked(repo_dir: Path) -> str | None:
    return config.load_seen_state(repo_dir).get("last_checked")


def format_line(change: PRChange, width: int = 4) -> str:
    """One PR as a list row: icon, number, title, author."""
    pr = change.pr
    draft = _c(" [draft]", DIM) if pr.draft else ""
    stacked = _c(f" ↳ on #{change.parent}", CYAN) if change.parent else ""
    return (
        f"{change.icon} {_c(f'#{pr.number:<{width}}', MAGENTA)} "
        f"{pr.title}{draft}  {_c(f'({pr.author})', DIM)}{stacked}"
    )


def run_check(repo_dir: Path, repo: str, mark: bool | None = True) -> int:
    """Print the PRs opened or updated since the last look. Returns their count.

    `mark` acknowledges them, so the next check starts from here; pass None to
    be asked once the digest is on screen."""
    prs = list_open_prs(repo)
    first = is_first_look(repo_dir)
    previous = last_checked(repo_dir)
    changes = classify(repo_dir, prs)
    fresh = [c for c in changes if c.status != SEEN]

    print(f"\n{_rule('═')}")
    header = f"New or updated since {previous}" if previous else "Open PRs, first check"
    print(_c(header, BOLD))
    print(_rule("═"))

    if first:
        print(_c("First check on this repo, so everything is new by definition.", DIM))
    if not prs:
        print(_c("No open PRs at all. Enjoy the quiet.", GREEN))
    elif not fresh:
        print(_c(f"Nothing new. {len(prs)} open PR(s), all unchanged since the last check.", GREEN))
    else:
        for change in fresh:
            print("  " + format_line(change))
            if change.status == UPDATED and change.since:
                print(_c(f"       updated since {change.since}", DIM))
        unchanged = len(changes) - len(fresh)
        if unchanged:
            print(_c(f"\n  (+ {unchanged} open PR(s) unchanged)", DIM))
        print(f"\n{len(fresh)} PR(s) worth a look: {_c(repo, MAGENTA)}")

    if mark is None:
        mark = bool(fresh) and confirm("\nMark these as seen?")
    if mark:
        mark_seen(repo_dir, prs)
        print(_c("Marked as seen.", DIM))
    else:
        print(_c("State left untouched.", DIM))
    print(_c(f"State: {config.seen_state_file(repo_dir)}", CYAN))
    return len(fresh)
