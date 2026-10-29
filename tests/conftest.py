"""Load the plugin directory as the ``repo_context`` package, the way Hermes imports it."""

from __future__ import annotations

import importlib.util
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


def make_clone(path: Path, url: str | None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", str(path)], check=True)
    if url:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)
    return path.resolve()
