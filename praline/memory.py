"""Build and update the local knowledge base: two markdown halves, one combined
markdown document, and an HTML rendering of it."""

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from string import Template

import markdown

from . import claude_client, config, prompts
from .github import (
    fetch_remote_branch,
    get_default_branch,
    get_merged_prs,
    get_merged_prs_since,
    get_recent_commits,
    get_repo_structure,
    local_commits_behind,
)

# If an "update" pass comes back shorter than this fraction of the previous
# document, treat it as erasure rather than editing and keep the old version.
_MIN_KEPT_FRACTION = 0.5

_HTML_TEMPLATE = Path(__file__).parent / "templates" / "knowledge.html"

_EMPTY_TOC = '<div class="toc">\n<ul></ul>\n</div>'


def _linkify_pr_refs(text: str, repo: str) -> str:
    """Turn bare `(#123)` PR citations into real links to the PR on GitHub."""
    return re.sub(
        r"(?<!\[)#(\d+)\b",
        lambda m: f"[#{m.group(1)}](https://github.com/{repo}/pull/{m.group(1)})",
        text,
    )


def _guard_against_erasure(previous: str, new: str, label: str) -> str:
    """An update pass should edit/enrich the previous doc, never wipe it out.
    If the model came back with something drastically shorter, that's a sign
    it rewrote from scratch instead of merging — keep the previous version
    rather than silently losing accumulated knowledge."""
    if not previous.strip():
        return new
    if len(new.strip()) < _MIN_KEPT_FRACTION * len(previous.strip()):
        print(
            f"  ⚠ new {label} is much shorter than the existing one "
            f"({len(new.strip())} vs {len(previous.strip())} chars) — "
            "keeping the existing version instead of overwriting it."
        )
        return previous
    return new


def _strip_preamble(doc: str) -> str:
    """Drop any chatter the model emitted before the document's first heading."""
    match = re.search(r"(?m)^#", doc)
    return doc[match.start():] if match else doc


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))


def _knowledge_body(repo_knowledge: str, pr_history: str) -> str:
    """The two documents stitched into one, with PR history demoted a level."""
    pr_history_nested = re.sub(r"(?m)^## ", "### ", pr_history)
    return f"{repo_knowledge}\n\n---\n\n## PR history and lessons\n\n{pr_history_nested}"


def render_knowledge_md(repo: str, repo_knowledge: str, pr_history: str) -> str:
    return f"# {repo} knowledge base\n\n{_knowledge_body(repo_knowledge, pr_history)}"


def render_knowledge_html(repo: str, repo_knowledge: str, pr_history: str) -> str:
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc"],
        extension_configs={"toc": {"anchorlink": False, "permalink": False}},
    )
    body = md.convert(_knowledge_body(repo_knowledge, pr_history))
    # safe_substitute, not substitute: the template has literal `$` in its prose.
    return Template(_HTML_TEMPLATE.read_text()).safe_substitute(
        title=f"{repo} — knowledge base",
        repo=repo,
        body=body,
        toc="" if md.toc.strip() == _EMPTY_TOC else md.toc,
    )


def scan_codebase(repo_dir: Path, model: str) -> str:
    """First-pass knowledge: let Claude read the checkout itself, read-only.

    Used only when there is no knowledge base yet. Later passes work from the
    file listing and recent commits, which is much cheaper."""
    raw = claude_client.ask(
        prompts.INIT_CODEBASE_PROMPT,
        "Read this repository and write the knowledge base document.",
        model=model,
        timeout=claude_client.EXPLORE_TIMEOUT_S,
        tools=claude_client.READ_ONLY_TOOLS,
        deny=claude_client.SECRET_DENY_RULES,
        cwd=repo_dir,
    )
    return _strip_preamble(raw)


def build_repo_knowledge(repo_dir: Path, repo: str, model: str) -> str:
    default_branch = get_default_branch(repo)
    ref = fetch_remote_branch(repo_dir, default_branch)
    if ref is None:
        print(
            f"  note: could not fetch origin/{default_branch}; "
            "reading your local checkout instead."
        )
        ref = "HEAD"
    elif behind := local_commits_behind(repo_dir, ref):
        print(
            f"  note: local checkout is {behind} commit(s) behind {ref}; "
            f"reading {ref} from GitHub instead of your working tree."
        )

    structure = get_repo_structure(repo_dir, ref)
    commits = get_recent_commits(repo_dir, ref, 50)
    previous = config.load_repo_knowledge(repo_dir)

    user_msg = f"""## Repository file structure

{structure}

## Recent commits (last 50, newest first)

{commits}
"""
    if previous:
        user_msg += f"\n## PREVIOUS KNOWLEDGE BASE (update this, don't discard it)\n\n{previous}\n"
    return _strip_preamble(claude_client.ask(prompts.INIT_REPO_PROMPT, user_msg, model=model))


def build_pr_history(repo: str, repo_dir: Path, since_days: int, model: str) -> str:
    prs = get_merged_prs(repo, since_days=since_days)
    previous = config.load_pr_history(repo_dir)
    if not prs:
        return previous or "No merged PRs found in the specified time range.\n"

    pr_summaries = []
    for pr in prs:
        pr_summaries.append(
            f"### PR #{pr['number']}: {pr['title']}\n"
            f"Author: {pr['user']['login']}\n"
            f"Merged: {pr.get('merged_at', 'unknown')}\n\n"
            f"{pr.get('body') or '(no description)'}\n"
        )

    user_msg = "\n---\n".join(pr_summaries)
    if previous:
        user_msg += f"\n---\n## PREVIOUS PR HISTORY (update this, don't discard it)\n\n{previous}\n"
    raw = claude_client.ask(prompts.INIT_PR_HISTORY_PROMPT, user_msg, model=model)
    return _linkify_pr_refs(_strip_preamble(raw), repo)


def last_updated_at(repo_dir: Path) -> datetime | None:
    """When the knowledge base was last built, or None if it never was."""
    raw = config.load_knowledge_state(repo_dir).get("updated_at")
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    # Knowledge bases built before the state file existed: fall back to mtime.
    f = config.repo_knowledge_file(repo_dir)
    if f.exists():
        return datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
    return None


def merged_since_last_update(repo: str, repo_dir: Path) -> list[dict]:
    """PRs merged since the knowledge base was last built. Empty if never built."""
    since = last_updated_at(repo_dir)
    return get_merged_prs_since(repo, since) if since else []


def update_knowledge(
    repo_dir: Path, repo: str, model: str, since_days: int = config.DEFAULT_SINCE_DAYS
) -> tuple[Path, Path]:
    """Rebuild both documents and write them out. Returns (markdown, html) paths."""
    previous_repo_knowledge = config.load_repo_knowledge(repo_dir)
    previous_pr_history = config.load_pr_history(repo_dir)
    first_build = not previous_repo_knowledge.strip()

    if first_build:
        print("Reading the whole codebase (first build, this takes a while)...")
        repo_knowledge = scan_codebase(repo_dir, model)
    else:
        print("Building repo knowledge...")
        repo_knowledge = build_repo_knowledge(repo_dir, repo, model)
    repo_knowledge = _guard_against_erasure(
        previous_repo_knowledge, repo_knowledge, "repo knowledge"
    )
    _backup(config.repo_knowledge_file(repo_dir))
    config.save_repo_knowledge(repo_dir, repo_knowledge)
    print(f"  Saved to {config.repo_knowledge_file(repo_dir)}")

    print(f"Building PR history (last {since_days} days)...")
    pr_history = build_pr_history(repo, repo_dir, since_days=since_days, model=model)
    pr_history = _guard_against_erasure(previous_pr_history, pr_history, "PR history")
    _backup(config.pr_history_file(repo_dir))
    config.save_pr_history(repo_dir, pr_history)
    print(f"  Saved to {config.pr_history_file(repo_dir)}")

    config.save_knowledge_md(repo_dir, render_knowledge_md(repo, repo_knowledge, pr_history))
    config.save_knowledge_html(repo_dir, render_knowledge_html(repo, repo_knowledge, pr_history))
    config.save_knowledge_state(repo_dir, {"updated_at": config.now_iso()})
    return config.knowledge_md_file(repo_dir), config.knowledge_html_file(repo_dir)
