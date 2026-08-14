"""How hard PRaline looks at a diff.

One knob, four settings. Level 0 is the default and deliberately quiet: the
comments a senior reviewer would actually type in a hurry. Each step up widens
what counts as worth saying, and the top level additionally lets Claude read the
repo around the diff instead of judging the diff alone.

The level only ever *adds* to the review prompt; the response contract, the
severity vocabulary and the approval loop are identical at every level.
"""

from dataclasses import dataclass

DEFAULT = 0
# From this level up, a review runs against a read-only checkout of the PR head
# so Claude can read callers and neighbouring code, not just the diff.
EXPLORE_FROM = 3


@dataclass(frozen=True)
class Level:
    value: int
    name: str
    blurb: str  # one line, shown in the CLI and the MCP tool description
    addendum: str  # appended to the review prompt


_LIGHT = """
## Depth: 0 — light (default)

Review this the way a senior teammate reviews a colleague's PR between two meetings: read it
properly, then say only the things worth saying out loud.

- Comment on what would change the author's mind or the shape of the code: a real bug, a design
  choice heading somewhere bad, something that will confuse the next reader.
- Skip anything the author can see for themselves, anything a linter or formatter owns, and any
  observation you would not bother interrupting someone for.
- Aim for at most five comments. If you have more than that, you are commenting on things that do
  not matter; keep the ones that do.
- Do not file `nit` items at this level unless the nit is genuinely load-bearing (a misleading name,
  a comment that says the opposite of what the code does). Style preferences are not.
- Returning an empty `comments` list with an honest summary is a good, normal outcome.
"""

_STANDARD = """
## Depth: 1 — standard

A full review pass over every changed file, at the level of detail you would give a PR you are
personally accountable for.

- Go file by file through the diff. For each one, ask: is it correct, is it in the right place, will
  it still be right when someone changes the code next to it?
- Cover correctness, error handling, naming, and whether the change fits the architecture described
  in REPO KNOWLEDGE.
- Nits are allowed, but they come last and stay short. Never let nits outnumber substantive comments.
- Say plainly in the summary if a part of the diff is fine, rather than inventing a comment for it.
"""

_THOROUGH = """
## Depth: 2 — thorough

A careful review. Assume this change will be in production for years and that nobody will read it
again before it breaks.

Work through each of these for the diff, and mention only what actually applies:

- **Edge cases.** Empty input, single element, unicode, very large input, zero, negative, null,
  concurrent callers, repeated calls. Name the specific input that breaks it.
- **Error paths.** What happens when the call fails, the file is missing, the network times out, the
  parse fails? Is the failure visible, or silently swallowed?
- **Resource handling.** Files, sockets, locks, subprocesses, temp dirs: opened and closed on every
  path, including the exception path.
- **Contracts and invariants.** Does the change hold the assumptions listed in REPO KNOWLEDGE, and
  does it keep the ones its own callers rely on? Flag a changed signature or behaviour whose callers
  were not updated.
- **Security.** Untrusted input reaching a shell, a query, a path, or a deserializer. Secrets in
  code, logs, or error messages. Permissions widened without a reason.
- **Tests.** Is the new behaviour covered? Does an existing test need to change? A behavioural change
  with no test change is worth one comment, once.
- **Performance.** Only where it is a real problem: work inside a loop that should be outside it, an
  N+1 pattern, an accidental quadratic. Not micro-optimization.

Every comment still has to earn its place. Thorough means you checked all of it, not that you
commented on all of it.
"""

_EXHAUSTIVE = """
## Depth: 3 — exhaustive

An adversarial audit. Your working assumption is that this diff contains at least one defect that
the author, and a normal review, would both miss. Your job is to find it or to establish that it is
not there.

Do everything at depth 2, and additionally:

- **Walk every changed hunk.** For each one, state to yourself what it changes and what would have
  to be true for it to be wrong. A hunk you have not reasoned about is a hunk you have not reviewed.
- **Read the code around the diff, not just the diff.** You have Read, Glob and Grep over a checkout
  of the PR head. Use them: open the full file before commenting on a hunk in it, and grep for every
  caller of anything whose signature, return value, raised exceptions, or timing changed.
- **Check the callers.** A changed function with unupdated callers is the single most common defect
  this level exists to catch. Look, do not assume.
- **Trace the data.** For a value that crosses a boundary (user input, a file, an API response,
  another process), follow it from where it enters to where it is used and check every assumption
  made about it along the way.
- **Look for what is missing.** The absent null check, the unhandled branch, the case the `if` chain
  does not cover, the test that was not written, the migration that was not added.
- **Concurrency and ordering**, where any exists: shared mutable state, check-then-act races,
  operations assumed to happen in an order nothing enforces.

Report what you find, ranked: bugs first, then warnings, then nits. If a thorough audit genuinely
turns up nothing, say exactly that in the summary and return empty lists. An exhaustive review that
pads itself with invented nits is a failed exhaustive review.
"""

LEVELS: dict[int, Level] = {
    0: Level(0, "light", "high-level comments a teammate would actually say out loud", _LIGHT),
    1: Level(1, "standard", "a full pass over every changed file", _STANDARD),
    2: Level(2, "thorough", "edge cases, error paths, security, tests", _THOROUGH),
    3: Level(
        3,
        "exhaustive",
        "adversarial audit, reads the repo around the diff",
        _EXHAUSTIVE,
    ),
}

MIN = min(LEVELS)
MAX = max(LEVELS)


def clamp(level: int) -> int:
    """Bring any integer into range. Callers pass user input straight in."""
    return max(MIN, min(MAX, int(level)))


def get(level: int) -> Level:
    return LEVELS[clamp(level)]


def label(level: int) -> str:
    """`2 (thorough)` — how a level is named everywhere it is reported."""
    lvl = get(level)
    return f"{lvl.value} ({lvl.name})"


def explores(level: int) -> bool:
    """Whether this level reviews from a checkout instead of the diff alone."""
    return clamp(level) >= EXPLORE_FROM


def choices_help() -> str:
    """The `--hardness` help text, built from the levels themselves."""
    return "  ".join(f"{lvl.value}={lvl.name}" for lvl in LEVELS.values())
