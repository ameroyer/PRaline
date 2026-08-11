"""Tiny terminal cosmetics shared by cli.py and auto.py. No dependency."""

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"

MASCOT = "🍫"


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
