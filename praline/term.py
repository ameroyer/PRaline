"""Tiny terminal cosmetics shared by the modules that print. No dependency."""

import re

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"

MASCOT = "🍫"


# Escape sequences and other control bytes, which a terminal executes rather
# than shows. A PR title, an author name and a comment body are all whatever
# somebody typed, and GitHub does not strip these on the way out.
_CONTROL_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"          # CSI: colour, cursor moves, erase line
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\\\)"  # OSC: window title and friends
    r"|\x1b[@-_]"                          # other two-byte escapes
    r"|[\x00-\x08\x0b-\x1f\x7f]"          # C0 controls, keeping tab and newline
)


def plain(text: str) -> str:
    """Untrusted text, made safe to print.

    Anything that reaches the terminal from GitHub or from the model is written
    by someone else. A title carrying `\\r\\x1b[2K` erases the line it is on and
    reprints whatever the author wants there, which in a tool that asks you to
    approve things is a way to forge what you are approving. Colour is applied
    by _c around this, never by the text itself."""
    return _CONTROL_RE.sub("", str(text))


def _c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


def _rule(char: str = "─", width: int = 60) -> str:
    return _c(char * width, DIM)


def confirm(question: str, default_yes: bool = True) -> bool:
    """Yes/no prompt. Anything other than an explicit yes/no takes the default."""
    suffix = "[Y/n]" if default_yes else "[y/N]"
    answer = input(f"{question} {suffix}: ").strip().lower()
    if not answer:
        return default_yes
    return answer in ("y", "yes")
