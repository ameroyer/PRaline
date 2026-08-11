"""Paths and file IO for the knowledge base, kept under `.praline/` inside the
reviewed repo. No logic beyond reading and writing."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "sonnet"
DEFAULT_MAX_CHANGED_FILES = 500
DEFAULT_SINCE_DAYS = 30


@dataclass
class Run:
    """What one invocation is working on: which repo, as whom, with what
    switched on. Passed around instead of six positional arguments, so adding a
    setting means touching one class rather than every signature in between.

    `slack` is a slack.SlackConfig or None, typed loosely to keep this module
    free of imports from the rest of the package."""

    repo_dir: Path
    repo: str
    model: str
    reviewer_login: str = ""
    request_review: bool = True
    slack: Any = None


def now_iso() -> str:
    """The timestamp format the state files use."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def knowledge_dir(repo_dir: Path) -> Path:
    return repo_dir / ".praline"


def repo_knowledge_file(repo_dir: Path) -> Path:
    return knowledge_dir(repo_dir) / "repo.md"


def pr_history_file(repo_dir: Path) -> Path:
    return knowledge_dir(repo_dir) / "pr_history.md"


def knowledge_md_file(repo_dir: Path) -> Path:
    return knowledge_dir(repo_dir) / "knowledge.md"


def knowledge_html_file(repo_dir: Path) -> Path:
    return knowledge_dir(repo_dir) / "knowledge.html"


def knowledge_state_file(repo_dir: Path) -> Path:
    return knowledge_dir(repo_dir) / "knowledge_state.json"


def artifact_url_file(repo_dir: Path) -> Path:
    return knowledge_dir(repo_dir) / "artifact_url.txt"


def auto_state_file(repo_dir: Path) -> Path:
    return knowledge_dir(repo_dir) / "auto_state.json"


def seen_state_file(repo_dir: Path) -> Path:
    return knowledge_dir(repo_dir) / "seen_state.json"


def review_log_file(repo_dir: Path) -> Path:
    return knowledge_dir(repo_dir) / "review_log.json"


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _write_json(path: Path, data: dict) -> None:
    _write(path, json.dumps(data, indent=2, sort_keys=True))


def knowledge_exists(repo_dir: Path) -> bool:
    return repo_knowledge_file(repo_dir).exists() and pr_history_file(repo_dir).exists()


def load_repo_knowledge(repo_dir: Path) -> str:
    return _read(repo_knowledge_file(repo_dir))


def save_repo_knowledge(repo_dir: Path, content: str) -> None:
    _write(repo_knowledge_file(repo_dir), content)


def load_pr_history(repo_dir: Path) -> str:
    return _read(pr_history_file(repo_dir))


def save_pr_history(repo_dir: Path, content: str) -> None:
    _write(pr_history_file(repo_dir), content)


def save_knowledge_md(repo_dir: Path, content: str) -> None:
    _write(knowledge_md_file(repo_dir), content)


def save_knowledge_html(repo_dir: Path, html: str) -> None:
    _write(knowledge_html_file(repo_dir), html)


def load_knowledge_state(repo_dir: Path) -> dict:
    """Metadata about the last knowledge-base build (currently: updated_at)."""
    return _read_json(knowledge_state_file(repo_dir))


def save_knowledge_state(repo_dir: Path, state: dict) -> None:
    _write_json(knowledge_state_file(repo_dir), state)


def load_auto_state(repo_dir: Path) -> dict:
    """Map PR number (str) -> ISO timestamp of this user's last auto review."""
    return _read_json(auto_state_file(repo_dir))


def save_auto_state(repo_dir: Path, state: dict) -> None:
    _write_json(auto_state_file(repo_dir), state)


def load_seen_state(repo_dir: Path) -> dict:
    """What PRaline saw the last time it looked at the open PR list:
    {"last_checked": iso, "prs": {number: {"created_at":…, "updated_at":…}}}."""
    return _read_json(seen_state_file(repo_dir))


def save_seen_state(repo_dir: Path, state: dict) -> None:
    _write_json(seen_state_file(repo_dir), state)


REVIEW_LOG_LIMIT = 200


def load_review_log(repo_dir: Path) -> list[dict]:
    """Every review PRaline has done in this repo, oldest first. One entry per
    review: PR number, title, author, head sha, status, summary, counts, time."""
    data = _read_json(review_log_file(repo_dir))
    entries = data.get("reviews", []) if isinstance(data, dict) else data
    return entries if isinstance(entries, list) else []


def append_review_log(repo_dir: Path, entry: dict) -> None:
    """Add one review to the log, keeping the most recent REVIEW_LOG_LIMIT.

    The cap exists so the file stays readable and cheap to load; the prompt
    only ever quotes a handful of the newest entries anyway."""
    entries = load_review_log(repo_dir)
    entries.append(entry)
    _write_json(review_log_file(repo_dir), {"reviews": entries[-REVIEW_LOG_LIMIT:]})


def load_artifact_url(repo_dir: Path) -> str | None:
    return _read(artifact_url_file(repo_dir)).strip() or None


def save_artifact_url(repo_dir: Path, url: str) -> None:
    _write(artifact_url_file(repo_dir), url.strip() + "\n")
