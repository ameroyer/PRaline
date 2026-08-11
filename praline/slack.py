"""Optional Slack notifications: ping the author of a PR once it has been reviewed.

Everything here is credentials-adjacent, so two rules hold throughout:

  - The bot token and the GitHub -> Slack user mapping live OUTSIDE the
    reviewed repo by default (`~/.config/praline/slack.json`), and every
    in-repo fallback path sits under `.praline/`, which is gitignored.
    Nothing in this module ever writes a token anywhere.
  - Slack is strictly optional. If it is not configured, PRaline behaves
    exactly as before; notification failures are reported and never abort
    a review.

The message goes to a group conversation holding the bot, the PR author and
you (the person running the review), so a question about a comment can be
answered right there. It falls back to a plain DM when only one of the two is
mapped, or when you reviewed your own PR.

Config file format (JSON):

    {
      "bot_token": "xoxb-...",          # optional if SLACK_BOT_TOKEN is set
      "users": {                        # GitHub login -> Slack member ID,
        "octocat": "U01234ABCDE",       # @handle, or email address
        "hubot": "hubot@example.com"
      }
    }

Map your own GitHub login too: that entry is what puts you in the group chat.

The bot needs `chat:write` and `mpim:write` (the latter only to open the group
conversation; a 1:1 DM needs no extra scope), plus `users:read` if you map to
@handles and `users:read.email` if you map to email addresses. Mapping straight
to member IDs (profile -> "Copy member ID") avoids the two `users:*` scopes.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .term import DIM, GREEN, YELLOW, _c
from .verdict import count_items, overview_of, status_label

API_ROOT = "https://slack.com/api"

CONFIG_ENV = "PRALINE_SLACK_CONFIG"
TOKEN_ENVS = ("PRALINE_SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN")


class SlackNotConfigured(RuntimeError):
    """No usable Slack config was found. Callers treat this as 'skip Slack'."""


def config_paths(repo_dir: Path) -> list[Path]:
    """Where a Slack config may live, most specific first.

    The in-repo path is last and lives under `.praline/`, which PRaline
    already asks you to gitignore — see the README."""
    paths = []
    env_path = os.environ.get(CONFIG_ENV)
    if env_path:
        paths.append(Path(env_path).expanduser())
    paths.append(Path.home() / ".config" / "praline" / "slack.json")
    paths.append(repo_dir / ".praline" / "slack.json")
    return paths


@dataclass
class SlackConfig:
    token: str
    users: dict[str, str] = field(default_factory=dict)
    source: str = "environment"
    reviewer_login: str = ""  # GitHub login of whoever is running PRaline
    _by_login: dict[str, str] = field(default_factory=dict, repr=False)
    _resolved: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_login = {k.lower(): v for k, v in self.users.items()}

    def slack_id_for(self, github_login: str) -> str | None:
        """Case-insensitive lookup of a GitHub login in the mapping."""
        return self._by_login.get(github_login.lower())


def load_config(repo_dir: Path, reviewer_login: str = "") -> SlackConfig:
    """Load the Slack config, or raise SlackNotConfigured.

    `reviewer_login` is the GitHub login PRaline is authenticated as; it is
    what lets notify_review put you in the conversation alongside the author.

    The token may come from the environment even when the file only holds
    the user mapping; that keeps the secret out of any file if you prefer."""
    data: dict = {}
    source = "environment"
    for path in config_paths(repo_dir):
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError as e:
                raise SlackNotConfigured(f"{path} is not valid JSON: {e}")
            source = str(path)
            break

    token = ""
    for env in TOKEN_ENVS:
        token = os.environ.get(env, "").strip()
        if token:
            break
    token = token or str(data.get("bot_token", "")).strip()
    if not token:
        raise SlackNotConfigured(
            'No Slack bot token. Set SLACK_BOT_TOKEN, or add "bot_token" to '
            f"{config_paths(repo_dir)[-2]}."
        )

    users = data.get("users") or {}
    if not isinstance(users, dict):
        raise SlackNotConfigured(f'"users" in {source} must be an object of github -> slack.')
    return SlackConfig(
        token=token,
        users={str(k): str(v) for k, v in users.items()},
        source=source,
        reviewer_login=reviewer_login,
    )


def _call(token: str, method: str, payload: dict | None = None, http: str = "POST") -> dict:
    url = f"{API_ROOT}/{method}"
    headers = {"Authorization": f"Bearer {token}"}
    if http == "GET":
        resp = requests.get(url, headers=headers, params=payload or {}, timeout=30)
    else:
        resp = requests.post(url, headers=headers, json=payload or {}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {data.get('error', 'unknown error')}")
    return data


def check_auth(cfg: SlackConfig) -> str:
    """Return the bot's own name, proving the token works."""
    data = _call(cfg.token, "auth.test")
    return data.get("user") or data.get("bot_id", "?")


def _resolve_user(cfg: SlackConfig, handle: str) -> str:
    """Turn a mapping value into a Slack member ID.

    Accepts a member ID (U…/W…, used as-is), an email (`users.lookupByEmail`),
    or an @handle / display name (scan of `users.list`). Results are memoized on
    the config: a run that reviews ten PRs by the same author should not page
    through the whole workspace directory ten times."""
    handle = handle.strip()
    if handle in cfg._resolved:
        return cfg._resolved[handle]
    if handle[:1] in ("U", "W") and handle[1:].isalnum() and handle.isupper():
        return handle
    if "@" in handle and "." in handle.split("@")[-1]:
        found = _call(cfg.token, "users.lookupByEmail", {"email": handle}, http="GET")
        return cfg._resolved.setdefault(handle, found["user"]["id"])

    wanted = handle.lstrip("@").lower()
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _call(cfg.token, "users.list", params, http="GET")
        for member in data.get("members", []):
            names = {
                str(member.get("name", "")).lower(),
                str(member.get("profile", {}).get("display_name", "")).lower(),
                str(member.get("profile", {}).get("real_name", "")).lower(),
            }
            if wanted in names - {""}:
                return cfg._resolved.setdefault(handle, member["id"])
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    raise RuntimeError(f"No Slack user matches '{handle}'.")


def resolve_logins(cfg: SlackConfig, github_logins: list[str]) -> list[str]:
    """Slack member IDs for these GitHub logins, in order, without duplicates.

    Unmapped logins are skipped, so a conversation still happens with whoever
    could be resolved. Raises LookupError only if nobody could."""
    ids: list[str] = []
    for login in github_logins:
        mapped = cfg.slack_id_for(login)
        if not mapped:
            continue
        user_id = _resolve_user(cfg, mapped)
        if user_id not in ids:
            ids.append(user_id)
    if not ids:
        raise LookupError(f"No Slack mapping for any of: {', '.join(github_logins) or '(nobody)'}.")
    return ids


def conversation_channel(cfg: SlackConfig, user_ids: list[str]) -> str:
    """The channel to post to for these users.

    A single user needs no setup call at all: chat.postMessage accepts a member
    ID as `channel` and opens the 1:1 DM itself, which is why `im:write` is not
    in the required scopes. Several users do need conversations.open, to create
    (or find) the group DM holding the bot and everyone listed — that is what
    `mpim:write` buys. Slack reuses one group conversation per set of people, so
    repeated reviews keep landing in a single history rather than N new chats."""
    if len(user_ids) == 1:
        return user_ids[0]
    data = _call(cfg.token, "conversations.open", {"users": ",".join(user_ids)})
    return data["channel"]["id"]


def post_to_logins(cfg: SlackConfig, github_logins: list[str], text: str) -> list[str]:
    """Post `text` to the conversation with these GitHub users. Returns the
    member IDs actually reached."""
    ids = resolve_logins(cfg, github_logins)
    _call(cfg.token, "chat.postMessage", {"channel": conversation_channel(cfg, ids), "text": text})
    return ids


def notify_review(
    cfg: "SlackConfig | None",
    repo: str,
    pr,
    items: list[dict],
    review: dict | None = None,
) -> None:
    """Tell the PR author about a finished review, with the reviewer in the
    room: if the reviewer is mapped too and isn't the author, this opens a
    group conversation (bot + author + reviewer) instead of a one-way DM, so
    the two can just talk there.

    Never fatal: a Slack outage or a missing mapping must not undo a review
    that already posted to GitHub."""
    if cfg is None:
        return
    audience = [pr.author]
    if cfg.reviewer_login and cfg.reviewer_login.lower() != pr.author.lower():
        audience.append(cfg.reviewer_login)
    text = review_message(repo, pr, count_items(items), review or {}, items)
    try:
        reached = post_to_logins(cfg, audience, text)
        who = " + ".join(f"@{login}" for login in audience if cfg.slack_id_for(login))
        kind = "group chat" if len(reached) > 1 else "DM"
        print(_c(f"  ✓ posted to Slack {kind} with {who}", GREEN))
    except LookupError as e:
        print(_c(f"  · no Slack ping: {e}", DIM))
    except Exception as e:
        print(_c(f"  ✗ Slack notification failed: {e}", YELLOW))


def notify_digest(cfg: "SlackConfig | None", repo: str, reviewed: list[dict]) -> None:
    """DM you, the reviewer, the round-up of everything just reviewed.

    Ready-to-approve PRs come first: those are the ones where you only have a
    button to press. Nothing is sent if you have no mapping, or if there is
    nothing to report. Like every Slack call here, failures are non-fatal."""
    if cfg is None or not reviewed or not cfg.reviewer_login:
        return
    try:
        post_to_logins(cfg, [cfg.reviewer_login], digest_message(repo, reviewed))
        print(_c(f"  ✓ sent your review round-up on Slack ({len(reviewed)} PR(s))", GREEN))
    except LookupError as e:
        print(_c(f"  · no Slack round-up: {e}", DIM))
    except Exception as e:
        print(_c(f"  ✗ Slack round-up failed: {e}", YELLOW))


def digest_message(repo: str, reviewed: list[dict]) -> str:
    """The reviewer's round-up, as Slack mrkdwn: what was reviewed, grouped by
    verdict, ready-to-approve first, one clickable line each.

    `reviewed` items carry number, title, url, status (a STATUS_LABEL key) and
    optionally the counts — the same shape log_review writes."""
    order = ["ready", "minor", "wip", "unknown", ""]
    heading = {
        "ready": "*✅ Ready to approve*",
        "minor": "*🛠️ Needs minor revisions*",
        "wip": "*🚧 Work in progress*",
    }
    lines = [f"🍫 *PRaline* just reviewed {len(reviewed)} PR(s) in *{repo}* for you:"]
    for key in order:
        group = [r for r in reviewed if str(r.get("status", "")) == key]
        if not group:
            continue
        lines.append("")
        lines.append(heading.get(key, "*❔ No verdict*"))
        for r in group:
            counts = ""
            if r.get("comments_added") or r.get("comments_left"):
                counts = (
                    f" — {r.get('comments_added', 0)} comment(s), "
                    f"{r.get('comments_left', 0)} reply(ies)"
                )
            lines.append(f"• <{r.get('url', '')}|#{r.get('number')} {r.get('title', '')}>{counts}")
    return "\n".join(lines)


def _quote_block(text: str) -> str:
    """Slack mrkdwn blockquote, one `>` per line so multi-line overviews keep
    their shape."""
    return "\n".join(f"> {line}" if line.strip() else ">" for line in text.strip().splitlines())


def review_message(
    repo: str, pr, counts: dict, review: dict, items: list[dict] | None = None
) -> str:
    """The message posted after a review, as Slack mrkdwn: the PR link, the
    status, the comment counts, and PRaline's overview at the end."""
    added = counts.get("comments_added", 0)
    replies = counts.get("comments_left", 0)
    resolved = counts.get("comments_resolved", 0)
    if added or replies:
        detail = f"{added} new comment(s), {replies} reply(ies), {resolved} thread(s) resolved."
    else:
        detail = "Nothing to flag — it looks good to me. 🎉"

    lines = [
        f"🍫 *PRaline* reviewed <{pr.url}|{repo}#{pr.number}: {pr.title}> (by @{pr.author})",
        f"*Status:* {status_label(review)}",
        detail,
    ]
    overview = overview_of(review, items)
    if overview:
        lines.append("\n*Overview*\n" + _quote_block(overview))
    return "\n".join(lines)
