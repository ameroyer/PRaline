"""Build and update the local knowledge base: what the repo does, what its
merged PRs taught, and the module map. Rendering to documents lives in
`render`; this module only decides what they say."""

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import claude_client, config, prompts, render
from . import graph as graph_mod
from .github import (
    FetchError,
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
            f"({len(new.strip())} vs {len(previous.strip())} chars), "
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


def scan_codebase(repo_dir: Path, model: str) -> str:
    """First-pass knowledge: let Claude read the checkout itself, read-only.

    Used only when there is no knowledge base yet. Later passes work from the
    file listing and recent commits, which is much cheaper."""
    raw = claude_client.ask(
        prompts.INIT_CODEBASE_PROMPT,
        "Read this repository and write the knowledge base document.",
        model=model,
        timeout=claude_client.EXPLORE_TIMEOUT_S,
        readable=repo_dir,
    )
    return _strip_preamble(raw)


def build_repo_knowledge(repo_dir: Path, repo: str, model: str) -> str:
    default_branch = get_default_branch(repo)
    try:
        ref = fetch_remote_branch(repo_dir, repo, default_branch)
        if behind := local_commits_behind(repo_dir, ref):
            print(
                f"  note: local checkout is {behind} commit(s) behind {ref}; "
                f"reading {ref} from GitHub instead of your working tree."
            )
    except FetchError as e:
        print(
            f"  note: could not fetch origin/{default_branch} ({e}); "
            "reading your local checkout instead."
        )
        ref = "HEAD"

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


def warn_if_no_knowledge(repo_dir: Path) -> bool:
    """Say so when a repo has no knowledge base yet. Returns whether it warned.

    The unattended modes review whatever they are pointed at, so without this a
    repo that was never set up gets reviewed against the bare prompt — a real
    drop in quality, and silent. They deliberately do not build one instead: a
    first build reads the whole codebase, which is the most expensive call
    PRaline makes and not something to start on its own behalf while nobody is
    watching."""
    if config.knowledge_exists(repo_dir):
        return False
    print(
        "⚠ No knowledge base for this repo, so reviews have no architecture, conventions "
        "or past PR lessons to go on."
    )
    print(f"  Build it first (a few minutes, once):  praline --dir {repo_dir}  then pick [2]")
    return True


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


def build_graph(repo_dir: Path, model: str) -> dict:
    """The module map for the HTML page, falling back to the previous one.

    The diagram is the one part of the knowledge base that is decoration: if
    Claude comes back with something unusable, the documents are still worth
    writing, so this never raises."""
    previous = config.load_graph(repo_dir)
    try:
        graph = graph_mod.build(repo_dir, model)
    except Exception as e:
        print(f"  ⚠ could not draw the module map: {e}")
        return previous
    if not graph["nodes"]:
        print("  ⚠ the module map came back empty; keeping the previous one.")
        return previous
    config.save_graph(repo_dir, graph)
    return graph


def rerender_html(repo_dir: Path, repo: str) -> Path:
    """Rewrite knowledge.html from the documents already on disk.

    Free: no model call, no network, no GitHub. It exists because the page and
    the documents age at different rates. The documents change when the repo
    does; the page changes whenever PRaline's template does, and until something
    re-renders, a knowledge base built last week still produces last week's page
    with none of the sections added since."""
    graph = config.load_graph(repo_dir)
    config.save_knowledge_html(
        repo_dir,
        render.knowledge_html(
            repo,
            config.load_repo_knowledge(repo_dir),
            config.load_pr_history(repo_dir),
            graph,
            graph_mod.stats(repo_dir, graph),
        ),
    )
    return config.knowledge_html_file(repo_dir)


def update_knowledge(
    repo_dir: Path,
    repo: str,
    model: str,
    since_days: int = config.DEFAULT_SINCE_DAYS,
    with_graph: bool = True,
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

    if with_graph:
        print("Drawing the module map...")
        graph = build_graph(repo_dir, model)
    else:
        graph = config.load_graph(repo_dir)

    config.save_knowledge_md(repo_dir, render.knowledge_md(repo, repo_knowledge, pr_history))
    config.save_knowledge_html(
        repo_dir,
        render.knowledge_html(
            repo, repo_knowledge, pr_history, graph, graph_mod.stats(repo_dir, graph)
        ),
    )
    config.save_knowledge_state(repo_dir, {"updated_at": config.now_iso()})
    return config.knowledge_md_file(repo_dir), config.knowledge_html_file(repo_dir)
