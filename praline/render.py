"""Turning the knowledge base into documents: one markdown file, and one
self-contained HTML page with the module map drawn at the bottom.

Kept apart from `memory`, which decides *what* the knowledge base says. Nothing
here calls Claude or GitHub: it is templating over strings that are already
written, so it can be exercised on its own.
"""

import html
import json
import re
from pathlib import Path
from string import Template

import markdown
from markdown.extensions import Extension

_HTML_TEMPLATE = Path(__file__).parent / "templates" / "knowledge.html"

_EMPTY_TOC = '<div class="toc">\n<ul></ul>\n</div>'


class _NoRawHtml(Extension):
    """Treat raw HTML in the source as text rather than markup.

    Markdown passes it through by default, which is wrong for this document at
    every step of how it is produced and used. The knowledge base is written by
    a model reading a repository, and its PR history is distilled from PR titles
    and bodies, which anyone able to open a PR controls. The result is rendered
    to a page that gets opened from disk and published as an artifact, and a
    `<script>` surviving that chain runs with no same-origin protection at all
    over `file://`. Escaping is done here rather than trusted to a prompt
    telling the model not to emit any.

    The two processors dropped below are the only places source HTML is
    recognised. Fenced code keeps working because `fenced_code_block` stashes
    its own output separately; escaping the stash instead would double-escape
    every code block in the document."""

    def extendMarkdown(self, md) -> None:
        md.preprocessors.deregister("html_block")
        md.inlinePatterns.deregister("html")


def _knowledge_body(repo_knowledge: str, pr_history: str) -> str:
    """The two documents stitched into one, with PR history demoted a level."""
    pr_history_nested = re.sub(r"(?m)^## ", "### ", pr_history)
    return f"{repo_knowledge}\n\n---\n\n## PR history and lessons\n\n{pr_history_nested}"


def knowledge_md(repo: str, repo_knowledge: str, pr_history: str) -> str:
    return f"# {repo} knowledge base\n\n{_knowledge_body(repo_knowledge, pr_history)}"


_KIND_LEGEND = {
    "entry": "▸ entry point",
    "module": "module",
    "external": "external",
}


def _tiles_html(stats: list[dict]) -> str:
    if not stats:
        return ""
    tiles = "\n".join(
        f'        <div class="tile"><div class="tile-value">{html.escape(str(t["value"]))}</div>'
        f'<span class="tile-label">{html.escape(str(t["label"]))}</span></div>'
        for t in stats
    )
    return f'      <div class="tiles">\n{tiles}\n      </div>'


def _map_table_html(graph: dict) -> str:
    """The diagram as a table.

    A node-link diagram is unreadable to a screen reader and unusable in print,
    so the same edges are always available as rows underneath it."""
    deps: dict[str, list[str]] = {}
    labels = {n["id"]: n["label"] for n in graph["nodes"]}
    for edge in graph["edges"]:
        deps.setdefault(edge["from"], []).append(labels.get(edge["to"], edge["to"]))
    rows = "\n".join(
        "          <tr>"
        f'<td><code>{html.escape(n["label"])}</code></td>'
        f'<td>{html.escape(n["kind"])}</td>'
        f'<td>{html.escape(", ".join(deps.get(n["id"], [])) or "—")}</td>'
        f'<td>{html.escape(n["summary"])}</td>'
        "</tr>"
        for n in graph["nodes"]
    )
    return (
        '      <details class="map-table">\n'
        "        <summary>Show the map as a table</summary>\n"
        "        <table>\n"
        "          <tr><th>Module</th><th>Kind</th><th>Depends on</th><th>Does</th></tr>\n"
        f"{rows}\n"
        "        </table>\n"
        "      </details>"
    )


def _map_html(graph: dict) -> str:
    if not graph.get("nodes"):
        return ""
    present = [k for k in _KIND_LEGEND if any(n["kind"] == k for n in graph["nodes"])]
    legend = "".join(
        f'<span class="k-{k}"><i></i>{html.escape(_KIND_LEGEND[k])}</span>' for k in present
    )
    return (
        '      <div class="map-head">\n'
        '        <span class="map-title">Module map</span>\n'
        f'        <span class="legend">{legend}</span>\n'
        "      </div>\n"
        '      <div class="map-frame" id="map-frame"></div>\n'
        f"{_map_table_html(graph)}"
    )


def _glance_html(graph: dict, stats: list[dict]) -> str:
    """The "repo at a glance" section: the counts, then the module map.

    Returns "" when there is nothing to show, so a knowledge base built without
    a graph renders exactly as it did before."""
    tiles = _tiles_html(stats)
    diagram = _map_html(graph)
    if not tiles and not diagram:
        return ""
    lede = (
        "How the modules fit together, read off the code. Hover a box to see what it does and "
        "what it touches; arrows point from a module to what it depends on."
        if diagram
        else "Counted from this repository's git history."
    )
    return (
        '      <section class="glance" id="repo-at-a-glance">\n'
        "        <h2>Repo at a glance</h2>\n"
        f'        <p class="lede">{lede}</p>\n'
        f"{tiles}\n{diagram}\n"
        "      </section>"
    )


def knowledge_html(
    repo: str,
    repo_knowledge: str,
    pr_history: str,
    graph: dict | None = None,
    stats: list[dict] | None = None,
) -> str:
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc", _NoRawHtml()],
        extension_configs={"toc": {"anchorlink": False, "permalink": False}},
    )
    body = md.convert(_knowledge_body(repo_knowledge, pr_history))
    graph = graph or {"nodes": [], "edges": []}
    glance = _glance_html(graph, stats or [])
    # The graph rides in a <script type="application/json"> block, so the only
    # sequence that can break out of it is a literal `</script`.
    graph_json = json.dumps(graph).replace("<", "\\u003c")
    # safe_substitute, not substitute: the template has literal `$` in its prose.
    return Template(_HTML_TEMPLATE.read_text()).safe_substitute(
        title=html.escape(f"{repo} knowledge base"),
        repo=html.escape(repo),
        body=body,
        toc="" if md.toc.strip() == _EMPTY_TOC else md.toc,
        glance=glance,
        # The nav link ships with the section, so a page without one has no
        # entry pointing at an anchor that isn't there.
        glance_nav=(
            '<a class="toc-extra" href="#repo-at-a-glance">Repo at a glance</a>' if glance else ""
        ),
        graph_data=graph_json,
    )
