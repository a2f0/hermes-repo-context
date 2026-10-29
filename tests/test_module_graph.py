"""Guard the plugin's module layering, its lack of import cycles, and its stdlib-only imports.

Imports are read with ``ast``, so lazy imports inside functions count too: a lazy import keeps a
cycle from failing at load time but still tangles the modules.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin"
PACKAGE = "__init__"
# Lowest first: a module may import only modules in earlier layers. README.md describes them.
LAYERS: tuple[tuple[str, ...], ...] = (
    ("identity",),
    ("claude_memory",),
    ("sync",),
    ("cli",),
    (PACKAGE,),
)
# Hermes's own modules: imported lazily inside a function, so the plugin still loads (and its
# tests run) without Hermes installed.
HERMES_MODULES = frozenset({"agent", "hermes_cli", "hermes_constants", "plugins"})


def _imports(tree: ast.Module) -> Iterator[tuple[ast.Import | ast.ImportFrom, bool]]:
    """Every import statement in *tree*, and whether it runs at import time (outside any function)."""

    def visit(node: ast.AST, top: bool) -> Iterator[tuple[ast.Import | ast.ImportFrom, bool]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import | ast.ImportFrom):
                yield child, top
            else:
                yield from visit(child, top and not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef))

    yield from visit(tree, True)


def module_graph(sources: Mapping[str, str]) -> tuple[dict[str, set[str]], list[str]]:
    """``(edges, problems)`` for plugin modules given as ``{name: source}``: the plugin modules
    each one imports, and every import of something other than the standard library, a sibling,
    or a lazily imported Hermes module."""
    edges: dict[str, set[str]] = {name: set() for name in sources}
    problems: list[str] = []
    for name, text in sources.items():
        for node, top in _imports(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.level != 1:
                    problems.append(f"{name}: relative import above the plugin package")
                    continue
                targets = [node.module] if node.module else [a.name for a in node.names]
                edges[name].update(t.split(".")[0] if t else PACKAGE for t in targets)
                continue
            roots = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
            for root in (r.split(".")[0] for r in roots):
                if root in sys.stdlib_module_names or root == "__future__":
                    continue
                if root in HERMES_MODULES and not top:
                    continue
                kind = "module-level Hermes import" if root in HERMES_MODULES else "non-stdlib import"
                problems.append(f"{name}: {kind} {root!r}")
    return edges, problems


def find_cycle(edges: Mapping[str, set[str]]) -> list[str] | None:
    """One import cycle as a path that returns to its start, or None."""
    done: set[str] = set()

    def visit(node: str, path: list[str]) -> list[str] | None:
        if node in path:
            return [*path[path.index(node) :], node]
        if node in done:
            return None
        for nxt in sorted(edges.get(node, ())):
            found = visit(nxt, [*path, node])
            if found:
                return found
        done.add(node)
        return None

    for start in sorted(edges):
        found = visit(start, [])
        if found:
            return found
    return None


def layer_violations(edges: Mapping[str, set[str]]) -> list[str]:
    rank = {module: i for i, layer in enumerate(LAYERS) for module in layer}
    out = [f"{m} is in no layer; add it to LAYERS" for m in sorted(edges) if m not in rank]
    for module, targets in sorted(edges.items()):
        for target in sorted(targets):
            if module in rank and target in rank and rank[target] >= rank[module] and target != module:
                out.append(f"{module} imports {target}, which is not in a lower layer")
    return out


def plugin_sources() -> dict[str, str]:
    return {p.stem: p.read_text() for p in sorted(PLUGIN_DIR.glob("*.py"))}


def test_plugin_modules_are_layered_without_cycles() -> None:
    edges, problems = module_graph(plugin_sources())
    assert problems == []
    assert find_cycle(edges) is None
    assert layer_violations(edges) == []


@pytest.mark.parametrize(
    ("sources", "problem"),
    [
        (
            {"identity": "from .sync import x\n", "sync": "from .identity import y\n"},
            "identity imports sync, which is not in a lower layer",
        ),
        ({"identity": "import yaml\n"}, "identity: non-stdlib import 'yaml'"),
        ({"identity": "from hermes_cli.config import load_config\n"}, "identity: module-level Hermes import"),
        ({"identity": "from .. import x\n"}, "identity: relative import above the plugin package"),
        ({"helpers": "import os\n"}, "helpers is in no layer; add it to LAYERS"),
    ],
)
def test_guard_reports_planted_violations(sources: dict[str, str], problem: str) -> None:
    edges, problems = module_graph(sources)
    found = problems + layer_violations(edges)
    assert any(problem in p for p in found), found


def test_guard_finds_cycles_through_lazy_imports() -> None:
    edges, _ = module_graph(
        {"identity": "def f():\n    from . import cli\n", "cli": "from . import identity\n", "sync": ""}
    )
    assert find_cycle(edges) == ["cli", "identity", "cli"]


def test_lazy_hermes_imports_are_allowed() -> None:
    assert module_graph({"identity": "def f():\n    from hermes_constants import x\n"})[1] == []
    assert module_graph({"identity": "if True:\n    import hermes_constants\n"})[1] != []
