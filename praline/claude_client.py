"""Thin wrapper around the `claude` CLI (Claude Code), used as the LLM backend.

Uses the user's existing Claude Code subscription — no Anthropic API key,
no billed API calls. Every call is a single headless turn. By default tool
access is disabled and Claude only reasons over text we hand it; callers that
need it can opt into read-only exploration of ONE directory, named by `readable`.

A review turn reads untrusted text: a PR's diff, its description and its
comments are written by whoever opened it. Everything here assumes that text
will at some point try to talk the model into doing something else, so nothing
is left to the model's judgement:

  - No tools at all unless a caller names a directory, and then only the three
    read tools, each scoped to that directory by a permission rule. A bare
    `Read` would allow reading any path on the machine.
  - No settings and no MCP servers, so a `.claude/settings.json` or `.mcp.json`
    committed in the PR under review cannot run a hook or launch a server.
  - No credentials in the environment (see _clean_env).

None of that depends on the model declining. The permission layer refuses the
call and records it in `permission_denials`.
"""

import json
import os
import re
import subprocess
from pathlib import Path

from . import budget

DEFAULT_TIMEOUT_S = 600
EXPLORE_TIMEOUT_S = 2400

# The tools a turn may use to read a checkout, and nothing that can write to
# it, run a command, or reach the network. Each is scoped to one directory by
# _read_only_rules; the bare name is never passed, because a bare `Read` allows
# reading any path on the machine.
_READ_TOOLS = ("Read", "Glob", "Grep")

# Files a turn must never open. Whatever Claude reads can end up in the
# knowledge base or in a review comment, so secrets are blocked at the source.
# Deny rules beat allow rules, which is what keeps these enforced inside the
# very directory the turn is otherwise allowed to read.
#
# These match credential *files*, not any name containing "secret" or "token": a
# blanket `*token*` would hide tokenizer.py and similar ordinary source, and a
# scan that quietly skips real code is its own kind of failure.
_SECRET_GLOBS = (
    "**/.env*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.jks",
    "**/*.keystore",
    "**/*.tfvars",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.npmrc",
    "**/.netrc",
    "**/.pypirc",
    "**/.htpasswd",
    "**/.git-credentials",
    "**/secrets.*",
    "**/secret.*",
    "**/credentials.*",
    "**/service-account*.json",
)

# Every read tool, not just Read: Grep returns the matching lines of a file it
# was never allowed to open, which is the same disclosure by another route.
_SECRET_DENY_RULES = ",".join(
    f"{tool}({glob})" for glob in _SECRET_GLOBS for tool in _READ_TOOLS
)


def _read_only_rules(directory: Path) -> str:
    """Allow rules confining every read tool to one directory.

    Scoping is the whole mechanism. `--allowedTools Read` permits reading any
    path on the machine, and a PR diff is untrusted text sitting in the same
    prompt, so confinement cannot rest on the model choosing well. With a
    scoped rule the permission layer refuses the call and reports it in
    `permission_denials`."""
    root = directory.resolve()
    return ",".join(f"{tool}({root}/**)" for tool in _READ_TOOLS)


# A review turn is given no settings and no MCP servers at all.
#
# Both are attacker-reachable. Project settings are read from the working
# directory, which in explore mode is a checkout of the PR being reviewed: a
# `.claude/settings.json` in that PR defines hooks, and a hook is an arbitrary
# shell command that runs on this machine. A `.mcp.json` in the same checkout
# names a server *command*, which is the same thing again. Ignoring every
# settings source closes both, and also stops the turn from inheriting the
# user's own MCP servers, whose tools are of no use to a review and whose reach
# is far wider than one repository.
_ISOLATION_ARGS = (
    "--setting-sources", "",
    "--strict-mcp-config",
    "--mcp-config", '{"mcpServers":{}}',
)

# The variable `github.fetch_refs` hands the token to git in. Defined here, next
# to the strip list it has to stay inside, and imported there — naming it in two
# places is how it would end up in only one of them.
FETCH_TOKEN_ENV = "PRALINE_FETCH_TOKEN"

# Credentials PRaline itself reads from the environment. They are of no use to a
# review turn, and a subprocess that never receives them cannot leak them —
# through a tool, a hook, an MCP server, or a diff that talks the model into
# echoing its environment. Defence in depth behind the tool allowlist, which
# already withholds Bash.
_CREDENTIAL_ENV_VARS = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "SLACK_BOT_TOKEN",
    "PRALINE_SLACK_BOT_TOKEN",
    FETCH_TOKEN_ENV,
)


def _clean_env() -> dict:
    """The current environment minus PRaline's credentials."""
    return {k: v for k, v in os.environ.items() if k not in _CREDENTIAL_ENV_VARS}


def ask(
    system_prompt: str,
    user_message: str,
    model: str,
    timeout: int = DEFAULT_TIMEOUT_S,
    readable: Path | None = None,
) -> str:
    """Run one headless Claude Code turn and return the raw text result.

    The user message (e.g. a PR diff) is passed via stdin rather than argv —
    large diffs blow past the OS argv size limit (E2BIG) if passed as an
    argument instead.

    `readable` is the single directory this turn may read, and the directory it
    runs in. Leave it None, as most callers do, and the turn gets no tools at
    all. There is deliberately no way to ask for a tool without naming the
    directory it is confined to."""
    budget.guard()
    allow = _read_only_rules(readable) if readable else ""
    cmd = [
        "claude",
        "-p",
        "--output-format", "json",
        "--model", model,
        "--allowedTools", allow,
        "--disallowedTools", _SECRET_DENY_RULES,
        *_ISOLATION_ARGS,
        "--system-prompt", system_prompt,
    ]
    try:
        result = subprocess.run(
            cmd,
            input=user_message,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=readable,
            env=_clean_env(),
        )
    except subprocess.TimeoutExpired as e:
        stderr = (e.stderr or "").strip()
        raise RuntimeError(
            f"claude CLI timed out after {timeout}s (model={model}).\n"
            f"stderr so far: {stderr[:2000] or '(empty)'}"
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited with code {result.returncode} (model={model}).\n"
            f"stderr: {result.stderr.strip()[:2000] or '(empty)'}\n"
            f"stdout: {result.stdout.strip()[:2000] or '(empty)'}"
        )

    if not result.stdout.strip():
        raise RuntimeError(
            f"claude CLI returned exit 0 but empty stdout (model={model}).\n"
            f"stderr: {result.stderr.strip()[:2000] or '(empty)'}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"claude CLI did not return valid JSON: {e}\n"
            f"raw stdout (first 2000 chars): {result.stdout[:2000]}\n"
            f"stderr: {result.stderr.strip()[:2000] or '(empty)'}"
        )

    if payload.get("is_error"):
        raise RuntimeError(f"claude CLI returned an error payload: {payload}")
    if "result" not in payload:
        raise RuntimeError(f"claude CLI JSON missing 'result' key: {payload}")
    budget.record(payload.get("usage") or {}, payload.get("total_cost_usd"))
    if not payload["result"].strip():
        raise RuntimeError(
            f"claude returned an empty result (model={model}, "
            f"stop_reason={payload.get('stop_reason')}, "
            f"subtype={payload.get('subtype')}, "
            f"num_turns={payload.get('num_turns')}, "
            f"permission_denials={payload.get('permission_denials')})"
        )
    return payload["result"]


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def extract_json(raw: str, what: str = "response") -> dict:
    """Parse a model reply as a JSON object, tolerating the ways it deviates
    from "just the object": a fenced code block, or prose either side of it.

    Tries progressively looser extractions and keeps the first that parses to
    an object. Every prompt that asks for JSON goes through this, so the
    tolerance is defined once rather than per caller."""
    candidates = [raw.strip()]

    fence = _FENCE_RE.search(raw)
    if fence:
        candidates.append(fence.group(1).strip())

    first, last = raw.find("{"), raw.rfind("}")
    if first != -1 and last > first:
        candidates.append(raw[first : last + 1].strip())

    last_error = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as e:
            last_error = e
            continue
        if isinstance(parsed, dict):
            return parsed

    raise RuntimeError(
        f"Claude's {what} wasn't valid JSON: {last_error}\n"
        f"raw response (first 2000 chars): {raw[:2000]}"
    )
