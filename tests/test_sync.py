from __future__ import annotations

import json
from pathlib import Path

import pytest
from repo_context import identity as ident
from repo_context import sync as sync_mod

from conftest import make_clone


class FakeTrust:
    def __init__(self, dirs: list[str]) -> None:
        self.dirs = list(dirs)
        self.saved: list[list[str]] = []

    def load(self) -> list[str]:
        return list(self.dirs)

    def save(self, dirs: list[str]) -> None:
        self.saved.append(list(dirs))
        self.dirs = list(dirs)


class FakeGitHub:
    renames = {"a2f0/stealth": "a2f0/balebase"}

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, name: str) -> str | None:
        self.calls.append(name)
        return self.renames.get(name, name)


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "github"
    clones = {
        "balebase": make_clone(root / "balebase", "git@github.com:a2f0/stealth.git"),
        "balebase2": make_clone(root / "balebase2", "https://github.com/a2f0/balebase.git"),
        "tearleads": make_clone(root / "tearleads", "git@github.com:a2f0/tearleads.git"),
        "unmanaged": make_clone(root / "unmanaged", "git@github.com:a2f0/unmanaged.git"),
        "matrix": make_clone(root / "matrix", "git@github.com:a2f0/matrix.git"),
        "local": make_clone(root / "local", None),
    }
    (root / "matrix" / "matrix.json").write_text(json.dumps({"repos": ["a2f0/balebase", "a2f0/tearleads"]}))
    clones["checkout"] = make_clone(root / "matrix" / "checkouts" / "tearleads", "git@github.com:a2f0/tearleads.git")
    (root / "notes").mkdir()
    clones["root"] = root
    return clones


def test_find_clones_includes_matrix_checkouts(workspace: dict[str, Path]) -> None:
    found = sync_mod.find_clones([workspace["root"]])
    assert workspace["checkout"] in found
    assert workspace["matrix"] in found
    assert all(p.name != "notes" for p in found)


def test_sync_trusts_managed_clones_and_caches_names(workspace: dict[str, Path], tmp_path: Path) -> None:
    gone = str(tmp_path / "deleted-clone")
    store = FakeTrust([gone, str(workspace["tearleads"]), str(workspace["matrix"])])
    github = FakeGitHub()
    result = sync_mod.sync([workspace["root"]], apply=True, trust_store=store, canonical=github)

    assert result.checkouts[str(workspace["balebase"])]["identity"] == "a2f0/balebase"
    assert result.managed == {"a2f0/matrix", "a2f0/balebase", "a2f0/tearleads"}
    assert set(result.trusted_added) == {str(workspace[k]) for k in ("balebase", "balebase2", "checkout")}
    assert result.trusted_removed == [gone]
    assert str(workspace["local"]) in result.unresolved
    assert store.saved and gone not in store.saved[-1]
    assert str(workspace["unmanaged"]) not in store.saved[-1]
    assert sorted(github.calls) == sorted(set(github.calls))

    cache = json.loads(ident.cache_path().read_text())
    assert cache["urls"]["git@github.com:a2f0/stealth.git"] == "a2f0/balebase"
    assert cache["checkouts"][str(workspace["checkout"])]["identity"] == "a2f0/tearleads"


def test_sync_reuses_cache_until_refresh(workspace: dict[str, Path]) -> None:
    sync_mod.sync([workspace["root"]], trust_store=FakeTrust([]), canonical=FakeGitHub())
    github = FakeGitHub()
    sync_mod.sync([workspace["root"]], trust_store=FakeTrust([]), canonical=github)
    assert github.calls == []
    sync_mod.sync([workspace["root"]], refresh=True, trust_store=FakeTrust([]), canonical=github)
    assert "a2f0/stealth" in github.calls


def test_trust_changes_only_with_apply(workspace: dict[str, Path], tmp_path: Path) -> None:
    store = FakeTrust([str(workspace["matrix"]), str(tmp_path / "deleted-clone")])
    result = sync_mod.sync([workspace["root"]], trust_store=store, canonical=FakeGitHub())
    assert result.trusted_added and result.trusted_removed
    assert store.saved == []
    assert ident.cache_path().is_file()


def test_extra_trusted_repository(workspace: dict[str, Path]) -> None:
    store = FakeTrust([])
    result = sync_mod.sync(
        [workspace["root"]], extra_trusted=["a2f0/unmanaged"], trust_store=store, canonical=FakeGitHub()
    )
    assert str(workspace["unmanaged"]) in result.trusted_added


def test_manifest_in_an_untrusted_clone_grants_nothing(workspace: dict[str, Path]) -> None:
    store = FakeTrust([])
    result = sync_mod.sync([workspace["root"]], apply=True, trust_store=store, canonical=FakeGitHub())
    assert result.managed == set()
    assert result.trusted_added == []
    assert store.saved == []


def test_clones_on_other_hosts_are_unresolved(tmp_path: Path) -> None:
    root = tmp_path / "src"
    other = make_clone(root / "other", "git@git.example.com:team/app.git")
    result = sync_mod.sync([root], trust_store=FakeTrust([]), canonical=FakeGitHub())
    assert result.checkouts == {}
    assert result.unresolved == [str(other)]


def test_trust_names_follow_renames(workspace: dict[str, Path]) -> None:
    result = sync_mod.sync(
        [workspace["root"]], extra_trusted=["a2f0/stealth"], trust_store=FakeTrust([]), canonical=FakeGitHub()
    )
    assert result.managed == {"a2f0/balebase"}
    assert set(result.trusted_added) == {str(workspace["balebase"]), str(workspace["balebase2"])}


@pytest.mark.parametrize("manifest", ["[]", '{"repos": [1, "a2f0/balebase", "bad name"]}', '{"repos": "x"}', "{"])
def test_malformed_manifests_do_not_abort_sync(workspace: dict[str, Path], manifest: str) -> None:
    (workspace["matrix"] / "matrix.json").write_text(manifest)
    result = sync_mod.sync(
        [workspace["root"]], trust_store=FakeTrust([str(workspace["matrix"])]), canonical=FakeGitHub()
    )
    assert "a2f0/matrix" in result.managed
    assert result.managed <= {"a2f0/matrix", "a2f0/balebase"}


def test_reused_path_is_marked_but_a_rename_is_not(workspace: dict[str, Path]) -> None:
    import subprocess

    sync_mod.sync([workspace["root"]], trust_store=FakeTrust([]), canonical=FakeGitHub())
    clone = str(workspace["unmanaged"])
    subprocess.run(["git", "-C", clone, "remote", "set-url", "origin", "git@github.com:a2f0/another.git"], check=True)
    result = sync_mod.sync([workspace["root"]], trust_store=FakeTrust([]), canonical=FakeGitHub())
    assert result.checkouts[clone].get("reused") is True
    # balebase's clone was cached as a2f0/stealth's URL, which resolves to a2f0/balebase: not reused.
    assert "reused" not in result.checkouts[str(workspace["balebase"])]
    result = sync_mod.sync([workspace["root"]], refresh=True, trust_store=FakeTrust([]), canonical=FakeGitHub())
    assert result.checkouts[clone].get("reused") is True


def test_project_key_history_survives_a_deleted_clone(tmp_path: Path) -> None:
    import shutil

    root = tmp_path / "src"
    make_clone(root / "foo-bar", "git@github.com:a2f0/first.git")
    sync_mod.sync([root], trust_store=FakeTrust([]), canonical=FakeGitHub())
    shutil.rmtree(root / "foo-bar")
    second = make_clone(root / "foo_bar", "git@github.com:a2f0/second.git")
    result = sync_mod.sync([root], trust_store=FakeTrust([]), canonical=FakeGitHub())
    assert result.checkouts[str(second)].get("reused") is True


def test_history_is_keyed_by_the_main_checkout(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "src"
    main = make_clone(root / "main", "git@github.com:a2f0/first.git")
    git = ["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    sync_mod.sync([root], trust_store=FakeTrust([]), canonical=FakeGitHub())
    subprocess.run([*git, "remote", "set-url", "origin", "git@github.com:a2f0/second.git"], check=True)
    tree = (root / "feature").resolve()
    subprocess.run([*git, "worktree", "add", "-q", str(tree)], check=True)
    result = sync_mod.sync([root], trust_store=FakeTrust([]), canonical=FakeGitHub())
    assert result.checkouts[str(tree)].get("reused") is True
