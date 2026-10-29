"""Load the plugin directory as the ``repo_context`` package, the way Hermes imports it."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin"


def _load_plugin() -> None:
    spec = importlib.util.spec_from_file_location(
        "repo_context", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the plugin from {PLUGIN_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_context"] = module
    spec.loader.exec_module(module)


_load_plugin()


@pytest.fixture(autouse=True)
def hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Keep the developer's global Git config and excludes out of the tests.
    user = tmp_path / "user-home"
    (user / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return home


_template: Path | None = None


@pytest.fixture(scope="session", autouse=True)
def _clone_template(tmp_path_factory: pytest.TempPathFactory) -> None:
    """One empty repository, made by ``git init`` without the user's config or templates, that
    ``make_clone`` copies: tests create about 200 clones, and two git processes for each were
    half the suite's run time."""
    global _template
    template = tmp_path_factory.mktemp("clone-template")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "-q", "--template=", str(template)], check=True, env=env
    )
    _template = template / ".git"


def make_clone(path: Path, url: str | None) -> Path:
    """A fresh repository at *path* with *url* as its ``origin``, as ``git init`` and ``git remote
    add`` would leave it."""
    assert _template is not None, "the session fixture _clone_template has not run"
    path.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_template, path / ".git")
    if url:
        with (path / ".git" / "config").open("a") as config:
            config.write(f'[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
    return path.resolve()


def git_commit(clone: Path, *args: str) -> None:
    """Commit in *clone* (``git commit -q`` plus *args*) without depending on the user's identity."""
    identity = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "-C", str(clone), *identity, "commit", "-q", *args], check=True)


def make_worktree_clone(main: Path, url: str, worktree: str | Path) -> tuple[Path, Path]:
    """A clone at *main* that ignores Claude Code's local settings, with a linked worktree at
    *worktree* (relative to the clone, or absolute); returns both resolved paths."""
    main = make_clone(main, url)
    (main / ".gitignore").write_text(".claude/settings.local.json\n")
    subprocess.run(["git", "-C", str(main), "add", ".gitignore"], check=True)
    git_commit(main, "-m", "init")
    linked = (main / worktree).resolve()
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(linked)], check=True)
    return main, linked
