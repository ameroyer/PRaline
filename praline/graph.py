"""The repo's module map: ask Claude for it, then make it safe to render.

Claude produces the graph by reading the checkout (prompts.ARCH_GRAPH_PROMPT);
everything after that is validation. Whatever comes back is treated as
untrusted: unknown node kinds, edges pointing at nodes that were never
declared, duplicate ids and self-loops are all things a model does occasionally,
and each of them draws a broken diagram rather than failing loudly. They are
dropped here so the template only ever receives a graph it can lay out.

The stat tiles alongside the diagram are counted from git, not asked for. A
number a model wrote down is a number nobody can check.
"""

from pathlib import Path

from . import claude_client, prompts
from .github import repo_counts

KINDS = ("entry", "module", "external")

MAX_NODES = 24
MAX_LABEL = 28
MAX_SUMMARY = 200


def _clean_text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize(data: dict) -> dict:
    """Drop everything the renderer cannot draw, keep the rest.

    Never raises on a malformed graph: a diagram with two of the four nodes is
    still worth showing, and the knowledge base must not fail to build because
    the map came back slightly wrong."""
    nodes = []
    seen: set[str] = set()
    for raw_node in data.get("nodes") or []:
        if not isinstance(raw_node, dict):
            continue
        node_id = _clean_text(raw_node.get("id"), 64)
        if not node_id or node_id in seen:
            continue
        kind = str(raw_node.get("kind", "")).strip().lower()
        seen.add(node_id)
        nodes.append(
            {
                "id": node_id,
                "label": _clean_text(raw_node.get("label") or node_id, MAX_LABEL),
                "kind": kind if kind in KINDS else "module",
                "summary": _clean_text(raw_node.get("summary"), MAX_SUMMARY),
            }
        )
        if len(nodes) >= MAX_NODES:
            break

    ids = {n["id"] for n in nodes}
    edges = []
    pairs: set[tuple[str, str]] = set()
    for raw_edge in data.get("edges") or []:
        if not isinstance(raw_edge, dict):
            continue
        src = _clean_text(raw_edge.get("from"), 64)
        dst = _clean_text(raw_edge.get("to"), 64)
        # A self-loop and a repeat both render as noise; an edge to a node that
        # was never declared has nowhere to land.
        if src not in ids or dst not in ids or src == dst or (src, dst) in pairs:
            continue
        pairs.add((src, dst))
        edges.append({"from": src, "to": dst, "label": _clean_text(raw_edge.get("label"), 24)})

    return {"nodes": nodes, "edges": edges}


def build(repo_dir: Path, model: str) -> dict:
    """Read the repo and return the normalized module map."""
    raw = claude_client.ask(
        prompts.ARCH_GRAPH_PROMPT,
        "Map this repository's modules and how they depend on each other.",
        model=model,
        timeout=claude_client.EXPLORE_TIMEOUT_S,
        readable=repo_dir,
    )
    return normalize(claude_client.extract_json(raw, "module map"))


def stats(repo_dir: Path, graph: dict) -> list[dict]:
    """The tiles above the diagram: size of the repo, size of the map.

    Every number here is counted by git, so it is checkable and cannot drift
    from what the model felt like reporting. Zero-valued tiles are dropped."""
    counts = repo_counts(repo_dir)
    tiles = [
        {"label": "Modules mapped", "value": len(graph.get("nodes", []))},
        {"label": "Dependencies", "value": len(graph.get("edges", []))},
        {"label": "Tracked files", "value": counts["tracked_files"]},
        {"label": "Commits", "value": counts["commits"]},
        {"label": "Contributors", "value": counts["contributors"]},
    ]
    return [t for t in tiles if t["value"]]
