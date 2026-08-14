"""A ceiling on what PRaline may spend on the model, over a rolling window.

Unattended modes can review PRs for as long as PRs keep arriving, which is
exactly the thing you do not want billed against a subscription with no upper
bound. A budget caps it: once the window's tokens are gone, the next model call
raises instead of running, and the caller waits for the window to roll forward.

Two decisions worth knowing:

  - **The numbers are the ones Anthropic reported**, read off each `claude`
    reply's `usage` block, not estimated from string lengths.
  - **Spending is persisted** (`.praline/budget.json`). A monitor that crashes
    and restarts must not get a fresh allowance; that would make the cap
    meaningless precisely when something is looping.

The active budget is module state rather than an argument, because
`claude_client.ask` is called from a dozen places that have no business
knowing about budgets. Setting it is a deliberate, one-line act; the default is
no budget at all, and then nothing here does anything.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

WINDOW_S = 3600


class BudgetExceeded(RuntimeError):
    """The window's allowance is gone. Carries how long until it frees up."""

    def __init__(self, message: str, retry_after_s: int) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


def billable(usage: dict) -> int:
    """Tokens charged for one call, from the CLI's `usage` block.

    Cache *reads* are excluded on purpose. A cached system prompt reports tens
    of thousands of read tokens on even a trivial call, at a fraction of the
    price, so counting them would exhaust a sensible budget in four calls and
    tell the user nothing true about what they spent. Fresh input, cache
    writes and output are what actually cost."""
    if not usage:
        return 0
    return (
        int(usage.get("input_tokens", 0))
        + int(usage.get("cache_creation_input_tokens", 0))
        + int(usage.get("output_tokens", 0))
    )


@dataclass
class Budget:
    """`limit` tokens per `window_s` seconds, counted across restarts."""

    limit: int
    window_s: int = WINDOW_S
    path: Path | None = None
    # (unix timestamp, tokens, usd) per model call, oldest first.
    calls: list[tuple[float, int, float]] = field(default_factory=list)

    def _prune(self) -> None:
        cutoff = time.time() - self.window_s
        self.calls = [c for c in self.calls if c[0] > cutoff]

    def spent(self) -> int:
        self._prune()
        return sum(c[1] for c in self.calls)

    def cost(self) -> float:
        self._prune()
        return sum(c[2] for c in self.calls)

    def remaining(self) -> int:
        return max(0, self.limit - self.spent())

    def retry_after_s(self) -> int:
        """How long until the oldest call falls out of the window and frees up
        room. 0 when there is already room."""
        if self.remaining() > 0 or not self.calls:
            return 0
        return max(1, int(self.calls[0][0] + self.window_s - time.time()) + 1)

    def guard(self) -> None:
        """Raise if there is nothing left to spend.

        Checked before a call rather than after, since a single review can be
        large: the cap is on starting new work, not on finishing what started."""
        if self.remaining() > 0:
            return
        wait = self.retry_after_s()
        raise BudgetExceeded(
            f"token budget spent: {self.spent():,} of {self.limit:,} in the last "
            f"{self.window_s // 60} min. Frees up in {wait // 60}m{wait % 60:02d}s.",
            retry_after_s=wait,
        )

    def record(self, usage: dict, usd: float | None) -> int:
        """Charge one model call to the budget. Returns the tokens counted."""
        tokens = billable(usage)
        self.calls.append((time.time(), tokens, float(usd or 0.0)))
        self.save()
        return tokens

    def save(self) -> None:
        if self.path is None:
            return
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"window_s": self.window_s, "calls": self.calls}, indent=2)
        )

    @classmethod
    def load(cls, path: Path, limit: int, window_s: int = WINDOW_S) -> "Budget":
        """A budget with any spending already recorded at `path` carried over.

        A window that no longer matches the stored one starts clean: entries
        counted against a different window cannot be reinterpreted honestly."""
        budget = cls(limit=limit, window_s=window_s, path=path)
        try:
            stored = json.loads(path.read_text())
        except (OSError, ValueError):
            return budget
        if stored.get("window_s") != window_s:
            return budget
        budget.calls = [
            (float(ts), int(tok), float(usd))
            for ts, tok, usd in stored.get("calls", [])
            if isinstance(ts, (int, float))
        ]
        budget._prune()
        return budget

    def summary(self) -> str:
        """One line: what has been spent, and what is left."""
        return (
            f"{self.spent():,}/{self.limit:,} tokens "
            f"(${self.cost():.2f}) in the last {self.window_s // 60} min, "
            f"{self.remaining():,} left"
        )


# The budget every model call is charged to. None means no cap, which is the
# default everywhere except when --token-budget is passed.
ACTIVE: Budget | None = None


def activate(budget: Budget | None) -> None:
    global ACTIVE
    ACTIVE = budget


def guard() -> None:
    if ACTIVE is not None:
        ACTIVE.guard()


def record(usage: dict, usd: float | None) -> None:
    if ACTIVE is not None:
        ACTIVE.record(usage, usd)
