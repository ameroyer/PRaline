"""Thin wrapper around the `claude` CLI (Claude Code), used as the LLM backend.

Uses the user's existing Claude Code subscription — no Anthropic API key,
no billed API calls. Every call is a single headless turn. By default tool
access is disabled and Claude only reasons over text we hand it; callers that
need it can opt into read-only exploration of a directory (see READ_ONLY_TOOLS).

The turn is deliberately starved of credentials: PRaline's own secrets are
stripped from the child environment (see _clean_env), so nothing the model or
any tool it reaches can do will surface a GitHub or Slack token.
"""

import json
import os
import subprocess
from pathlib import Path

DEFAULT_TIMEOUT_S = 600
EXPLORE_TIMEOUT_S = 2400

# Enough to read and search a checkout, and nothing that can write to it or
# reach the network.
READ_ONLY_TOOLS = "Read,Glob,Grep"

# Files a codebase scan must never open. Whatever Claude reads can end up in the
# knowledge base, which is fed back into review prompts and can be published, so
# secrets have to be blocked at the source. These are deny rules, which the CLI
# enforces over the allowlist above, and they cover Grep as well as Read.
#
# These match credential *files*, not any name containing "secret" or "token": a
# blanket `*token*` would hide tokenizer.py and similar ordinary source, and a
# scan that quietly skips real code is its own kind of failure.
_SECRET_DENY_GLOBS = (
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
SECRET_DENY_RULES = ",".join(f"Read({glob})" for glob in _SECRET_DENY_GLOBS)

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
)


def _clean_env() -> dict:
    """The current environment minus PRaline's credentials."""
    return {k: v for k, v in os.environ.items() if k not in _CREDENTIAL_ENV_VARS}


def ask(
    system_prompt: str,
    user_message: str,
    model: str,
    timeout: int = DEFAULT_TIMEOUT_S,
    tools: str = "",
    deny: str = "",
    cwd: Path | None = None,
) -> str:
    """Run one headless Claude Code turn and return the raw text result.

    The user message (e.g. a PR diff) is passed via stdin rather than argv —
    large diffs blow past the OS argv size limit (E2BIG) if passed as an
    argument instead.

    `tools` is a comma-separated allowlist ("" means no tools at all), `deny` a
    matching denylist that overrides it, and `cwd` the directory the turn runs
    in, which bounds what those tools can reach.
    """
    cmd = [
        "claude",
        "-p",
        "--output-format", "json",
        "--model", model,
        "--allowedTools", tools,
        "--disallowedTools", deny,
        "--system-prompt", system_prompt,
    ]
    try:
        result = subprocess.run(
            cmd,
            input=user_message,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
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
    if not payload["result"].strip():
        raise RuntimeError(
            f"claude returned an empty result (model={model}, "
            f"stop_reason={payload.get('stop_reason')}, "
            f"subtype={payload.get('subtype')}, "
            f"num_turns={payload.get('num_turns')}, "
            f"permission_denials={payload.get('permission_denials')})"
        )
    return payload["result"]
