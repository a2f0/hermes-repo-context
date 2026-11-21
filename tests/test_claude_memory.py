from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import pytest

from conftest import git_commit, make_clone, make_worktree_clone

# Hermes loads the plugin as ``repo_context`` (see conftest.py); mypy checks it as ``plugin``.
if TYPE_CHECKING:
    from plugin import claude_memory as cm
    from plugin import cli
    from plugin import identity as ident
else:
    from repo_context import claude_memory as cm
    from repo_context import cli
    from repo_context import identity as ident

SHARED = "~/.claude/repo-memory/a2f0/demo"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    claude = home / ".claude"
    one = home / "github" / "demo"
    two = home / "github" / "demo2"
    for clone in (one, two):
        make_clone(clone, "git@github.com:a2f0/demo.git")
        (clone / ".gitignore").write_text(".claude/settings.local.json\n")
        (clone / ".claude").mkdir(parents=True)
    (one / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"allow": ["x"]}}))

    def memory(path: Path | str, files: dict[str, str]) -> Path:
        key = path if isinstance(path, str) else cm.path_key(str(path))
        d = claude / "projects" / key / "memory"
        for name, text in files.items():
            (d / name).parent.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(text)
        return d

    memory(one, {"MEMORY.md": "# Memory Index\n\n- [A](a.md) — a\n", "a.md": "a"})
    memory(two, {"MEMORY.md": "# Memory Index\n- [A](a.md) — a\n- [B](b.md) — b\n", "b.md": "b", "notes/n.md": "n"})
    memory("-old-stealth", {"MEMORY.md": "- [C](c.md) — c\n", "c.md": "c"})
    return {"home": home, "claude": claude, "one": one, "two": two}


URL = "git@github.com:a2f0/demo.git"


def checkouts(env: dict[str, Path]) -> dict[str, ident.CheckoutInfo]:
    return {str(env[k]): {"url": URL, "identity": "a2f0/demo"} for k in ("one", "two")}


def source(env: dict[str, Path], clone: str) -> Path:
    key: str = cm.path_key(str(env[clone]))
    return env["claude"] / "projects" / key / "memory"


def test_path_key_matches_claude_code() -> None:
    assert cm.path_key("/Users/me/.hermes/x.y") == "-Users-me--hermes-x-y"


def test_memory_subpath_keeps_repositories_apart() -> None:
    assert cm.memory_subpath("A2F0/Matrix") == PurePosixPath("a2f0/matrix")
    assert cm.memory_subpath("a-b/c") != cm.memory_subpath("a/b-c")
    assert cm.memory_subpath("gitlab.com/group/project") is None
    assert cm.memory_subpath("a2f0/..") is None
    assert cm.memory_subpath("solo") is None


def test_plan_and_apply_merge_by_remote(env: dict[str, Path]) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"], aliases={"-old-stealth": "a2f0/demo"})
    assert len(plans) == 1
    g = plans[0]
    assert g.dest == env["claude"] / "repo-memory" / "a2f0" / "demo"
    assert sorted(str(rel) for _, _, rel in g.copies) == ["a.md", "b.md", "c.md", "notes/n.md"]
    assert g.index == ["# Memory Index", "", "- [A](a.md) — a", "- [B](b.md) — b", "- [C](c.md) — c"]
    assert len(g.settings_changes) == 2
    assert g.kept == []

    cm.apply(plans, today="2026-10-29")
    assert (g.dest / "notes" / "n.md").read_text() == "n"
    assert sorted(p.name for p in g.dest.iterdir()) == ["MEMORY.md", "a.md", "b.md", "c.md", "notes"]
    assert not source(env, "one").exists()
    assert (source(env, "one").with_name("memory.merged-2026-10-29") / "a.md").is_file()
    one = json.loads((env["one"] / ".claude" / "settings.local.json").read_text())
    assert one == {"permissions": {"allow": ["x"]}, "autoMemoryDirectory": SHARED}
    two = json.loads((env["two"] / ".claude" / "settings.local.json").read_text())
    assert two == {"autoMemoryDirectory": SHARED}

    assert cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"]) == []


def test_rerun_merges_memory_recreated_by_a_running_session(env: dict[str, Path]) -> None:
    cm.apply(cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"]), today="2026-10-29")
    late = source(env, "two")
    late.mkdir()
    (late / "MEMORY.md").write_text("- [D](d.md) — d\n")
    (late / "d.md").write_text("d")
    cm.apply(cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"]), today="2026-10-29")
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    assert (dest / "d.md").read_text() == "d"
    assert (dest / "MEMORY.md").read_text().splitlines()[-1] == "- [D](d.md) — d"
    assert (late.parent / "memory.merged-2026-10-29.2").is_dir()


def test_conflicting_file_keeps_the_shared_copy_and_the_source(env: dict[str, Path]) -> None:
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    dest.mkdir(parents=True)
    (dest / "a.md").write_text("shared version")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert [f.name for f in plans[0].conflicts] == ["a.md"]
    cm.apply(plans, today="2026-10-29")
    assert (dest / "a.md").read_text() == "shared version"
    assert (source(env, "one") / "a.md").is_file()
    assert not source(env, "two").exists()
    one = json.loads((env["one"] / ".claude" / "settings.local.json").read_text())
    assert "autoMemoryDirectory" not in one


def test_symlink_in_a_source_keeps_it_in_place(env: dict[str, Path], tmp_path: Path) -> None:
    (tmp_path / "elsewhere.md").write_text("x")
    (source(env, "one") / "link.md").symlink_to(tmp_path / "elsewhere.md")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert [f.name for f in plans[0].skipped] == ["link.md"]
    cm.apply(plans, today="2026-10-29")
    assert (source(env, "one") / "link.md").is_symlink()
    assert "autoMemoryDirectory" not in json.loads((env["one"] / ".claude" / "settings.local.json").read_text())


def test_never_writes_through_a_symlink_in_the_shared_directory(env: dict[str, Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    dest.mkdir(parents=True)
    (dest / "notes").symlink_to(outside, target_is_directory=True)
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert [f.name for f in plans[0].conflicts] == ["n.md"]
    cm.apply(plans, today="2026-10-29")
    assert list(outside.iterdir()) == []
    assert (source(env, "two") / "notes" / "n.md").is_file()
    assert not (env["two"] / ".claude" / "settings.local.json").exists()


@pytest.mark.parametrize("linked", ["a2f0/demo", "a2f0"], ids=["shared-directory", "its-ancestor"])
def test_symlinked_shared_directory_blocks_the_group(env: dict[str, Path], tmp_path: Path, linked: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = env["claude"] / "repo-memory" / linked
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].blocked
    cm.apply(plans, today="2026-10-29")
    assert list(outside.iterdir()) == []
    assert source(env, "one").is_dir() and source(env, "two").is_dir()


def test_case_variants_of_a_remote_share_one_plan(env: dict[str, Path]) -> None:
    mixed: dict[str, ident.CheckoutInfo] = {
        str(env["one"]): {"url": URL, "identity": "A2F0/Demo"},
        str(env["two"]): {"url": URL, "identity": "a2f0/demo"},
    }
    plans = cm.plan(mixed, claude_home=env["claude"], home=env["home"])
    assert len(plans) == 1
    assert len(plans[0].sources) == 2


def test_existing_different_setting_is_left_alone(env: dict[str, Path]) -> None:
    path = env["two"] / ".claude" / "settings.local.json"
    path.write_text(json.dumps({"autoMemoryDirectory": "~/elsewhere"}))
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    notes = {p: note for _clone, p, _data, note in plans[0].settings}
    assert notes[path] == "already set to ~/elsewhere; left alone"
    assert [p for p, _ in plans[0].kept] == [source(env, "two")]


def test_clone_that_cannot_be_redirected_keeps_its_memory(env: dict[str, Path]) -> None:
    (env["two"] / ".gitignore").write_text("")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    notes = {p: note for _clone, p, _data, note in plans[0].settings}
    assert notes[env["two"] / ".claude" / "settings.local.json"] == "not gitignored in this clone; left alone"
    assert [p for p, _ in plans[0].kept] == [source(env, "two")]
    cm.apply(plans, today="2026-10-29")
    assert not (env["two"] / ".claude" / "settings.local.json").exists()
    assert (source(env, "two") / "b.md").is_file()
    assert not source(env, "one").exists()


def test_failed_settings_write_keeps_the_source(env: dict[str, Path]) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    (env["two"] / ".claude").chmod(0o500)
    try:
        cm.apply(plans, today="2026-10-29")
    finally:
        (env["two"] / ".claude").chmod(0o700)
    assert (source(env, "two") / "b.md").is_file()
    assert any("writing its settings failed" in reason for _, reason in plans[0].kept)
    assert not source(env, "one").exists()


def test_index_limit_warning(env: dict[str, Path]) -> None:
    big = source(env, "two") / "MEMORY.md"
    big.write_text("".join(f"- [N{i}](n{i}.md) — {'x' * 200}\n" for i in range(150)))
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].over_limit


def test_symlinked_settings_file_is_never_written(env: dict[str, Path], tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}")
    (env["two"] / ".claude" / "settings.local.json").symlink_to(elsewhere)
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    notes = {p: note for _clone, p, _data, note in plans[0].settings}
    assert notes[env["two"] / ".claude" / "settings.local.json"].startswith("settings file or .claude is a symlink")
    cm.apply(plans, today="2026-10-29")
    assert elsewhere.read_text() == "{}"
    assert (source(env, "two") / "b.md").is_file()


def test_symlinked_memory_directory_is_not_redirected(env: dict[str, Path], tmp_path: Path) -> None:
    real = tmp_path / "real-memory"
    source(env, "one").rename(real)
    source(env, "one").symlink_to(real, target_is_directory=True)
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert source(env, "one") in plans[0].kept_paths
    cm.apply(plans, today="2026-10-29")
    assert "autoMemoryDirectory" not in json.loads((env["one"] / ".claude" / "settings.local.json").read_text())
    assert source(env, "one").is_symlink()


def test_invalid_only_is_rejected(env: dict[str, Path]) -> None:
    with pytest.raises(ValueError, match="typo"):
        cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"], only=["typo"])


def test_clone_whose_remote_changed_since_sync_is_left_alone(env: dict[str, Path]) -> None:

    subprocess.run(
        ["git", "-C", str(env["two"]), "remote", "set-url", "origin", "git@github.com:a2f0/other.git"], check=True
    )
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert source(env, "two") in plans[0].kept_paths
    assert all(s.clone != env["two"] for s in plans[0].sources)
    cm.apply(plans, today="2026-10-29")
    assert (source(env, "two") / "b.md").is_file()
    assert not (env["two"] / ".claude" / "settings.local.json").exists()
    assert not (plans[0].dest / "b.md").exists()


def test_writes_since_planning_are_kept(env: dict[str, Path]) -> None:
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    dest.mkdir(parents=True)
    (dest / "MEMORY.md").write_text("- [S](s.md) — written by a running session\n")
    (dest / "a.md").write_text("session version")
    (source(env, "two") / "late.md").write_text("late")
    cm.apply(plans, today="2026-10-29")
    index = (dest / "MEMORY.md").read_text().splitlines()
    assert index[0] == "- [S](s.md) — written by a running session"
    assert "- [B](b.md) — b" in index
    assert (dest / "a.md").read_text() == "session version"
    assert (source(env, "one") / "a.md").is_file()
    assert (source(env, "two") / "late.md").is_file()
    reasons = dict(plans[0].kept)
    assert reasons[source(env, "two")] == "changed since planning; re-run to merge it"
    assert not (env["two"] / ".claude" / "settings.local.json").exists()


def test_planted_temporary_index_symlink_is_not_written_through(env: dict[str, Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("untouched")
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    dest.mkdir(parents=True)
    (dest / ".MEMORY.md.tmp").symlink_to(outside)
    cm.apply(cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"]), today="2026-10-29")
    assert outside.read_text() == "untouched"
    assert (dest / "MEMORY.md").read_text().startswith("# Memory Index")


def test_settings_changed_since_planning_are_kept(env: dict[str, Path]) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    one = env["one"] / ".claude" / "settings.local.json"
    one.write_text(json.dumps({"permissions": {"allow": ["x", "y"]}}))
    two = env["two"] / ".claude" / "settings.local.json"
    two.write_text(json.dumps({"autoMemoryDirectory": "~/elsewhere"}))
    cm.apply(plans, today="2026-10-29")
    assert json.loads(one.read_text()) == {"permissions": {"allow": ["x", "y"]}, "autoMemoryDirectory": SHARED}
    assert json.loads(two.read_text()) == {"autoMemoryDirectory": "~/elsewhere"}
    assert (source(env, "two") / "b.md").is_file()
    assert not source(env, "one").exists()


def test_file_and_directory_with_one_name_are_conflicts(env: dict[str, Path]) -> None:
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    dest.mkdir(parents=True)
    (dest / "notes").write_text("a file where two has a directory")
    (source(env, "one") / "b.md").mkdir()
    (source(env, "one") / "b.md" / "inner.md").write_text("i")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    # one's b.md/inner.md is planned first, so two's file b.md is the one that clashes.
    assert {f.name for f in plans[0].conflicts} == {"n.md", "b.md"}
    cm.apply(plans, today="2026-10-29")
    assert (dest / "notes").read_text() == "a file where two has a directory"
    assert (dest / "b.md" / "inner.md").read_text() == "i"
    assert (source(env, "two") / "notes" / "n.md").is_file()
    assert (source(env, "two") / "b.md").is_file()


def test_collision_appearing_after_planning_does_not_abort(env: dict[str, Path]) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    dest.mkdir(parents=True)
    (dest / "notes").write_text("appeared")
    cm.apply(plans, today="2026-10-29")
    assert (dest / "notes").read_text() == "appeared"
    assert (source(env, "two") / "notes" / "n.md").is_file()
    assert (dest / "a.md").read_text() == "a"


def test_clones_sharing_a_claude_project_directory_are_left_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude = home / ".claude"
    dash = make_clone(home / "github" / "foo-bar", "git@github.com:a2f0/foo-bar.git")
    under = make_clone(home / "github" / "foo_bar", "git@github.com:a2f0/other.git")
    for clone in (dash, under):
        (clone / ".gitignore").write_text(".claude/settings.local.json\n")
    shared = claude / "projects" / cm.path_key(str(dash)) / "memory"
    assert shared == claude / "projects" / cm.path_key(str(under)) / "memory"
    shared.mkdir(parents=True)
    (shared / "x.md").write_text("whose?")
    found: dict[str, ident.CheckoutInfo] = {
        str(dash): {"url": "git@github.com:a2f0/foo-bar.git", "identity": "a2f0/foo-bar"},
        str(under): {"url": "git@github.com:a2f0/other.git", "identity": "a2f0/other"},
    }
    plans = cm.plan(found, claude_home=claude, home=home)
    assert all(not g.sources and not g.settings_changes for g in plans)
    assert all(shared in g.kept_paths for g in plans)
    cm.apply(plans, today="2026-10-29")
    assert (shared / "x.md").is_file()
    assert not (dash / ".claude" / "settings.local.json").exists()


def test_failed_copy_keeps_every_source_that_relied_on_it(env: dict[str, Path]) -> None:
    for clone in ("one", "two"):
        (source(env, clone) / "same.md").write_text("same")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].providers[PurePosixPath("same.md")] == [source(env, "one"), source(env, "two")]
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    dest.mkdir(parents=True)
    (dest / "same.md").write_text("written since planning")
    cm.apply(plans, today="2026-10-29")
    assert (dest / "same.md").read_text() == "written since planning"
    for clone in ("one", "two"):
        assert (source(env, clone) / "same.md").is_file()
        assert "autoMemoryDirectory" not in (cm.read_settings(env[clone] / ".claude" / "settings.local.json")[0] or {})


@pytest.mark.parametrize("error", [OSError(28, "No space left on device"), KeyboardInterrupt()])
def test_interrupted_copy_leaves_no_partial_file(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    import os

    big = "x" * 4096
    (source(env, "one") / "big.md").write_text(big)
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    real_fdopen = os.fdopen

    class HalfWriter:
        """A copy whose write stops halfway, as on a full disk or a Ctrl-C."""

        def __init__(self, out: Any) -> None:
            self.out = out

        def __enter__(self) -> HalfWriter:
            return self

        def __exit__(self, *exc: object) -> None:
            self.out.close()

        def write(self, data: bytes) -> int:
            if data != big.encode():
                return int(self.out.write(data))
            self.out.write(data[: len(data) // 2])
            self.out.flush()
            raise error

    def flaky_fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        out = real_fdopen(fd, mode, *args, **kwargs)
        return HalfWriter(out) if "b" in mode else out

    monkeypatch.setattr(os, "fdopen", flaky_fdopen)
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            cm.apply(plans, today="2026-10-29")
    else:
        cm.apply(plans, today="2026-10-29")
        assert source(env, "one") in plans[0].kept_paths
        one = cm.read_settings(env["one"] / ".claude" / "settings.local.json")[0] or {}
        assert "autoMemoryDirectory" not in one
    assert not (dest / "big.md").exists()
    assert not [p.name for p in dest.iterdir() if p.name.startswith(".")]
    assert (source(env, "one") / "big.md").read_text() == big

    # Nothing half-written blocks the next run.
    monkeypatch.setattr(os, "fdopen", real_fdopen)
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].conflicts == []
    cm.apply(plans, today="2026-10-29")
    assert (dest / "big.md").read_text() == big
    assert not source(env, "one").exists()


def test_copy_of_a_longest_allowed_filename(env: dict[str, Path]) -> None:
    name = "x" * 252 + ".md"  # 255 bytes, the usual NAME_MAX
    (source(env, "one") / name).write_text("long")
    cm.apply(cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"]), today="2026-10-29")
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    assert (dest / name).read_text() == "long"
    assert not source(env, "one").exists()


def test_memory_written_after_redirect_restores_the_setting(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    real_redirect = cm._redirect

    def redirect_then_write(g: Any, clone: Path, path: Path, target: str) -> None:
        real_redirect(g, clone, path, target)
        if clone == env["two"]:
            (source(env, "two") / "during.md").write_text("written by a running session")

    monkeypatch.setattr(cm, "_redirect", redirect_then_write)
    cm.apply(plans, today="2026-10-29")
    two = json.loads((env["two"] / ".claude" / "settings.local.json").read_text())
    assert "autoMemoryDirectory" not in two
    assert (source(env, "two") / "during.md").is_file()
    assert env["two"] not in plans[0].redirected
    one = json.loads((env["one"] / ".claude" / "settings.local.json").read_text())
    assert one["autoMemoryDirectory"] == SHARED
    assert not source(env, "one").exists()


def test_provider_changed_after_planning_keeps_every_dependent(env: dict[str, Path]) -> None:
    for clone in ("one", "two"):
        (source(env, clone) / "same.md").write_text("same")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    (source(env, "one") / "same.md").write_text("changed since planning")
    cm.apply(plans, today="2026-10-29")
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    assert not (dest / "same.md").exists()
    for clone in ("one", "two"):
        assert (source(env, clone) / "same.md").is_file()
        assert "autoMemoryDirectory" not in (cm.read_settings(env[clone] / ".claude" / "settings.local.json")[0] or {})


def test_memory_created_after_planning_blocks_the_redirect(env: dict[str, Path]) -> None:
    import shutil

    shutil.rmtree(source(env, "two"))
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    late = source(env, "two")
    late.mkdir()
    (late / "new.md").write_text("learned after planning")
    cm.apply(plans, today="2026-10-29")
    assert (late / "new.md").is_file()
    assert not (env["two"] / ".claude" / "settings.local.json").exists()
    one = json.loads((env["one"] / ".claude" / "settings.local.json").read_text())
    assert one["autoMemoryDirectory"] == SHARED


def test_shared_file_removed_after_planning_keeps_the_source(env: dict[str, Path]) -> None:
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    dest.mkdir(parents=True)
    (dest / "a.md").write_text("a")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert all(rel != PurePosixPath("a.md") for _, _, rel in plans[0].copies)
    (dest / "a.md").unlink()
    cm.apply(plans, today="2026-10-29")
    assert (source(env, "one") / "a.md").is_file()
    assert "autoMemoryDirectory" not in json.loads((env["one"] / ".claude" / "settings.local.json").read_text())
    assert dict(plans[0].kept)[source(env, "one")] == "the shared directory does not hold all of it"


def test_clone_that_stopped_reading_the_shared_directory_keeps_its_memory(env: dict[str, Path]) -> None:
    settings = env["two"] / ".claude" / "settings.local.json"
    settings.write_text(json.dumps({"autoMemoryDirectory": SHARED}))
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert env["two"] in plans[0].redirected
    settings.write_text("{}")
    cm.apply(plans, today="2026-10-29")
    assert (source(env, "two") / "b.md").is_file()
    assert dict(plans[0].kept)[source(env, "two")] == "its clone no longer reads the shared directory"


def test_alias_with_a_leading_dash_parses_when_attached() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    cli.setup(parser)
    args = parser.parse_args(["claude-memory", "--alias=-Users-me-github-old=me/new"])
    assert args.alias == ["-Users-me-github-old=me/new"]


def test_merge_index_keeps_entries_under_their_headings() -> None:
    current = ["# Memory Index", "", "## Project", "- [P](p.md) — p", "", "## Feedback", "- [F](f.md) — f"]
    incoming = [
        "# Memory Index",
        "- [T](t.md) — top",
        "## Feedback",
        "- [G](g.md) — g",
        "## Reference",
        "- [R](r.md) — r",
    ]
    merged, added = cm.merge_index(current, incoming)
    assert merged == [
        "# Memory Index",
        "- [T](t.md) — top",
        "",
        "## Project",
        "- [P](p.md) — p",
        "",
        "## Feedback",
        "- [F](f.md) — f",
        "- [G](g.md) — g",
        "",
        "## Reference",
        "- [R](r.md) — r",
    ]
    assert added == 4
    assert cm.merge_index(merged, incoming) == (merged, 0)


def test_index_entries_for_conflicting_files_stay_with_their_source(env: dict[str, Path]) -> None:
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    dest.mkdir(parents=True)
    (dest / "b.md").write_text("another clone's b")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert "- [B](b.md) — b" not in plans[0].index
    cm.apply(plans, today="2026-10-29")
    assert "b.md" not in (dest / "MEMORY.md").read_text()
    assert (source(env, "two") / "MEMORY.md").is_file()


def test_index_entries_for_copies_failing_at_apply_stay_behind(env: dict[str, Path]) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    dest = env["claude"] / "repo-memory" / "a2f0" / "demo"
    dest.mkdir(parents=True)
    (dest / "b.md").write_text("written since planning")
    cm.apply(plans, today="2026-10-29")
    index = (dest / "MEMORY.md").read_text()
    assert "a.md" in index and "b.md" not in index


def test_link_targets_ignore_fragments_queries_and_urls() -> None:
    line = "- [T](./topic.md#details) [Q](q%20x.md?v=1) [W](https://example.com/a.md) [M](mailto:x@y)"
    assert cm.link_targets(line) == {PurePosixPath("topic.md"), PurePosixPath("q x.md")}


def test_merge_index_keeps_repeated_lines_and_entries_under_two_headings() -> None:
    current = ["## A", "- [X](x.md)"]
    incoming = ["## A", "```", "code", "```", "## B", "- [X](x.md)"]
    merged, added = cm.merge_index(current, incoming)
    assert merged == ["## A", "- [X](x.md)", "```", "code", "```", "", "## B", "- [X](x.md)"]
    assert added == 5
    assert cm.merge_index(merged, incoming)[1] == 0


def test_alias_only_run_needs_no_cached_clones(capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    parser = argparse.ArgumentParser()
    cli.setup(parser)
    args = parser.parse_args(["claude-memory", "--alias=-Users-me-github-old=me/new"])
    assert cli.main(args) == 0
    assert capsys.readouterr().out.startswith("DRY RUN")


def test_dot_github_memory_directory() -> None:
    assert cm.memory_subpath("a2f0/.github") == PurePosixPath("a2f0/.github")


def test_merge_index_keeps_fenced_blocks_whole() -> None:
    current = ["## A", "```", "one", "```"]
    incoming = ["## A", "```", "two", "```", "```", "one", "```"]
    merged, added = cm.merge_index(current, incoming)
    assert merged == ["## A", "```", "one", "```", "```", "two", "```"]
    assert added == 3
    assert cm.merge_index(merged, incoming)[1] == 0


def test_replace_if_unchanged(tmp_path: Path) -> None:
    (tmp_path / "dir").mkdir()
    path = tmp_path / "dir" / "MEMORY.md"
    path.write_text("a")
    assert not cm._replace_if_unchanged(path, "new", b"b")
    assert path.read_text() == "a"
    assert cm._replace_if_unchanged(path, "new", b"a")
    assert path.read_text() == "new"
    assert sorted(p.name for p in path.parent.iterdir()) == ["MEMORY.md"]


def test_index_written_by_a_session_during_apply_is_merged_not_lost(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    real = cm._replace_if_unchanged
    calls: list[int] = []

    def session_writes_first(path: Path, text: str, before: bytes | None) -> bool:
        if path.name == "MEMORY.md" and not calls:
            calls.append(1)
            path.write_text("- [S](s.md) — written by a running session\n")
        return bool(real(path, text, before))

    monkeypatch.setattr(cm, "_replace_if_unchanged", session_writes_first)
    cm.apply(plans, today="2026-10-29")
    index = (plans[0].dest / "MEMORY.md").read_text().splitlines()
    assert index[0] == "- [S](s.md) — written by a running session"
    assert "- [B](b.md) — b" in index


def test_fenced_blocks_differing_in_indentation_are_distinct() -> None:
    current = ["```", "  a", "```"]
    incoming = ["```", "a", "```"]
    assert cm.merge_index(current, incoming)[1] == 3


def test_memory_created_while_redirecting_undoes_the_redirect(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    shutil.rmtree(source(env, "two"))
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    real = cm._redirect

    def redirect_then_session_writes(g: Any, clone: Path, path: Path, target: str) -> None:
        real(g, clone, path, target)
        if clone == env["two"]:
            source(env, "two").mkdir()
            (source(env, "two") / "new.md").write_text("new")

    monkeypatch.setattr(cm, "_redirect", redirect_then_session_writes)
    cm.apply(plans, today="2026-10-29")
    assert (source(env, "two") / "new.md").is_file()
    assert "autoMemoryDirectory" not in json.loads((env["two"] / ".claude" / "settings.local.json").read_text())


def test_busy_clones(env: dict[str, Path]) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert cm.busy_clones(plans, {(env["two"] / "src").resolve()}) == [env["two"]]
    assert cm.busy_clones(plans, {env["home"].resolve()}) == []


def test_apply_refuses_while_claude_runs_in_a_clone(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    ident.cache_path().write_text(json.dumps({"checkouts": checkouts(env)}))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: env["home"]))
    monkeypatch.setattr(cm, "live_claude_dirs", lambda: {env["one"].resolve()})
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    assert cli.main(parser.parse_args(["claude-memory", "--apply"])) == 1
    assert "in an affected clone: ~/github/demo" in capsys.readouterr().out
    monkeypatch.setattr(cm, "live_claude_dirs", lambda: {(env["home"] / "elsewhere").resolve()})
    assert cli.main(parser.parse_args(["claude-memory", "--apply"])) == 1
    assert "working in: ~/elsewhere" in capsys.readouterr().out
    assert source(env, "one").is_dir() and source(env, "two").is_dir()
    assert cli.main(parser.parse_args(["claude-memory", "--apply", "--allow-running"])) == 0
    assert not source(env, "one").exists()


def test_path_that_held_another_repository_is_left_alone(env: dict[str, Path]) -> None:
    found = checkouts(env)
    found[str(env["two"])]["reused"] = True
    plans = cm.plan(found, claude_home=env["claude"], home=env["home"])
    assert dict(plans[0].kept)[source(env, "two")].startswith("this path has held another repository")
    cm.apply(plans, today="2026-10-29")
    assert (source(env, "two") / "b.md").is_file()
    assert not (env["two"] / ".claude" / "settings.local.json").exists()


def test_user_or_project_memory_setting_is_respected(env: dict[str, Path]) -> None:
    (env["two"] / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/team"}))
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    notes = {p: note for _clone, p, _data, note in plans[0].settings}
    assert notes[env["two"] / ".claude" / "settings.local.json"].startswith("the project's settings are set to")
    assert source(env, "two") in plans[0].kept_paths
    (env["claude"] / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/mine"}))
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].blocked.startswith("autoMemoryDirectory is set in the user settings")
    cm.apply(plans, today="2026-10-29")
    assert source(env, "one").is_dir()


def test_alias_assigns_a_reused_paths_memory(env: dict[str, Path]) -> None:
    found = checkouts(env)
    found[str(env["two"])]["reused"] = True
    key = cm.path_key(str(env["two"]))
    plans = cm.plan(found, claude_home=env["claude"], home=env["home"], aliases={key: "a2f0/old"})
    by_identity = {g.identity: g for g in plans}
    assert [s.path for s in by_identity["a2f0/old"].sources] == [source(env, "two")]
    assert source(env, "two") not in by_identity["a2f0/demo"].kept_paths
    cm.apply(plans, today="2026-10-29")
    assert (env["claude"] / "repo-memory" / "a2f0" / "old" / "b.md").read_text() == "b"
    assert not source(env, "two").exists()
    two = json.loads((env["two"] / ".claude" / "settings.local.json").read_text())
    assert two["autoMemoryDirectory"] == SHARED


def test_merge_index_keeps_nested_sections_under_their_parents() -> None:
    current = ["## Project", "- [P](p.md)", "## Feedback", "- [F](f.md)"]
    merged, added = cm.merge_index(current, ["## Project", "### Build", "- [B](b.md)"])
    assert merged == ["## Project", "- [P](p.md)", "", "### Build", "- [B](b.md)", "## Feedback", "- [F](f.md)"]
    assert added == 2
    merged, added = cm.merge_index(["## Feedback", "- [F](f.md)"], ["## Project", "### Build", "- [B](b.md)"])
    assert merged == ["## Feedback", "- [F](f.md)", "", "## Project", "### Build", "- [B](b.md)"]
    assert cm.merge_index(merged, ["## Project", "### Build", "- [B](b.md)"])[1] == 0


def test_busy_check_covers_clones_whose_memory_an_alias_claims(env: dict[str, Path]) -> None:
    key = cm.path_key(str(env["two"]))
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"], aliases={key: "a2f0/old"})
    assert cm.busy_clones(plans, {env["two"].resolve()}) == [env["two"]]


def test_merge_index_matches_repeated_headings_by_parent() -> None:
    current = ["## A", "### Notes", "- a", "## B", "### Notes", "- b"]
    merged, added = cm.merge_index(current, ["## B", "### Notes", "- b2"])
    assert merged == ["## A", "### Notes", "- a", "## B", "### Notes", "- b", "- b2"]
    assert added == 1


def test_symlinked_user_settings_are_read(env: dict[str, Path], tmp_path: Path) -> None:
    dotfiles = tmp_path / "dotfiles-settings.json"
    dotfiles.write_text(json.dumps({"autoMemoryDirectory": "~/mine"}))
    (env["claude"] / "settings.json").symlink_to(dotfiles)
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].blocked.startswith("autoMemoryDirectory is set in the user settings")
    dotfiles.write_text("{not json")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].blocked == "the user settings ~/.claude/settings.json cannot be read; left alone"
    dotfiles.write_text(json.dumps({"theme": "dark"}))
    assert not cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])[0].blocked


def test_merge_index_keeps_blank_lines_that_shape_markdown() -> None:
    incoming = ["- [A](a.md)", "", "    indented code", "", "Paragraph."]
    merged, _added = cm.merge_index([], incoming)
    assert merged == incoming
    assert cm.merge_index(merged, incoming)[1] == 0
    assert cm.merge_index(["code"], ["    code"])[1] == 1


def test_busy_check_survives_only_filtering_an_aliased_clone(env: dict[str, Path]) -> None:
    key = cm.path_key(str(env["two"]))
    plans = cm.plan(
        checkouts(env), claude_home=env["claude"], home=env["home"], aliases={key: "a2f0/old"}, only=["a2f0/old"]
    )
    assert [g.identity for g in plans] == ["a2f0/old"]
    assert cm.busy_clones(plans, {env["two"].resolve()}) == [env["two"]]


@pytest.mark.parametrize("key", ["/etc", "../x", "-a/../../b", "a", "-a b", ""])
def test_alias_keys_must_be_project_keys(env: dict[str, Path], key: str) -> None:
    with pytest.raises(ValueError, match="invalid project key"):
        cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"], aliases={key: "a2f0/old"})


def test_links_beneath_a_skipped_directory_stay_behind(env: dict[str, Path], tmp_path: Path) -> None:
    import shutil

    shutil.rmtree(source(env, "two") / "notes")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "topic.md").write_text("t")
    (source(env, "two") / "notes").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    (source(env, "two") / "MEMORY.md").write_text("- [B](b.md) — b\n- [T](notes/topic.md#x) — t\n")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert "- [T](notes/topic.md#x) — t" not in plans[0].index
    assert "- [B](b.md) — b" in plans[0].index


def test_claimant_waits_for_its_aliased_memory_to_merge(env: dict[str, Path]) -> None:
    key = cm.path_key(str(env["two"]))
    old_dest = env["claude"] / "repo-memory" / "a2f0" / "old"
    old_dest.mkdir(parents=True)
    (old_dest / "b.md").write_text("old repository's different b")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"], aliases={key: "a2f0/old"})
    cm.apply(plans, today="2026-10-29")
    assert (source(env, "two") / "b.md").read_text() == "b"
    assert not (env["two"] / ".claude" / "settings.local.json").exists()
    (old_dest / "b.md").write_text("b")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"], aliases={key: "a2f0/old"})
    cm.apply(plans, today="2026-10-29")
    assert not source(env, "two").exists()
    two = json.loads((env["two"] / ".claude" / "settings.local.json").read_text())
    assert two["autoMemoryDirectory"] == SHARED


def test_worktrees_share_the_main_checkouts_memory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude = home / ".claude"
    main, linked = make_worktree_clone(home / "github" / "wt", URL, home / "github" / "wt-feature")
    assert cm.project_root(linked) == main
    memory = claude / "projects" / cm.path_key(str(main)) / "memory"
    memory.mkdir(parents=True)
    (memory / "a.md").write_text("a")
    found: dict[str, ident.CheckoutInfo] = {
        str(main): {"url": URL, "identity": "a2f0/demo"},
        str(linked): {"url": URL, "identity": "a2f0/demo"},
    }
    plans = cm.plan(found, claude_home=claude, home=home)
    assert [(s.clone, s.worktrees) for s in plans[0].sources] == [(main, [linked])]
    cm.apply(plans, today="2026-10-29")
    assert (claude / "repo-memory" / "a2f0" / "demo" / "a.md").read_text() == "a"
    assert not memory.exists()
    for clone in (main, linked):
        settings = json.loads((clone / ".claude" / "settings.local.json").read_text())
        assert settings["autoMemoryDirectory"] == "~/.claude/repo-memory/a2f0/demo"


def test_uncached_worktrees_are_redirected_too(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude = home / ".claude"
    main, hidden = make_worktree_clone(home / "github" / "wt", URL, ".claude/worktrees/agent")
    memory = claude / "projects" / cm.path_key(str(main)) / "memory"
    memory.mkdir(parents=True)
    (memory / "a.md").write_text("a")
    plans = cm.plan({str(main): {"url": URL, "identity": "a2f0/demo"}}, claude_home=claude, home=home)
    assert plans[0].sources[0].worktrees == [hidden]
    assert cm.busy_clones(plans, {hidden}) == [main, hidden]  # a session in hidden is inside main too
    cm.apply(plans, today="2026-10-29")
    for clone in (main, hidden):
        assert json.loads((clone / ".claude" / "settings.local.json").read_text())["autoMemoryDirectory"]


def test_settings_failure_without_memory_is_reported(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    import shutil

    shutil.rmtree(source(env, "two"))
    ident.cache_path().write_text(json.dumps({"checkouts": checkouts(env)}))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: env["home"]))
    monkeypatch.setattr(cm, "live_claude_dirs", lambda: set())
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    (env["two"] / ".claude").chmod(0o500)
    try:
        assert cli.main(parser.parse_args(["claude-memory", "--apply"])) == 1
    finally:
        (env["two"] / ".claude").chmod(0o700)
    assert "NOT REDIRECTED" in capsys.readouterr().out


def test_alias_naming_the_checkouts_own_repository_completes_in_one_apply(env: dict[str, Path]) -> None:
    found = checkouts(env)
    found[str(env["two"])]["reused"] = True
    key = cm.path_key(str(env["two"]))
    plans = cm.plan(found, claude_home=env["claude"], home=env["home"], aliases={key: "a2f0/demo"})
    cm.apply(plans, today="2026-10-29")
    assert not source(env, "two").exists()
    assert (plans[0].dest / "b.md").read_text() == "b"
    two = json.loads((env["two"] / ".claude" / "settings.local.json").read_text())
    assert two["autoMemoryDirectory"] == SHARED


def test_claimant_worktrees_are_watched_and_redirected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude = home / ".claude"
    main, hidden = make_worktree_clone(home / "github" / "wt", URL, ".claude/worktrees/agent")
    key = cm.path_key(str(main))
    memory = claude / "projects" / key / "memory"
    memory.mkdir(parents=True)
    (memory / "a.md").write_text("a")
    found: dict[str, ident.CheckoutInfo] = {str(main): {"url": URL, "identity": "a2f0/demo", "reused": True}}
    plans = cm.plan(found, claude_home=claude, home=home, aliases={key: "a2f0/old"}, only=["a2f0/old"])
    assert cm.busy_clones(plans, {hidden}) == [main, hidden]
    plans = cm.plan(found, claude_home=claude, home=home, aliases={key: "a2f0/old"})
    cm.apply(plans, today="2026-10-29")
    assert (claude / "repo-memory" / "a2f0" / "old" / "a.md").read_text() == "a"
    for clone in (main, hidden):
        settings = json.loads((clone / ".claude" / "settings.local.json").read_text())
        assert settings["autoMemoryDirectory"] == "~/.claude/repo-memory/a2f0/demo"


def test_shared_memory_is_private(env: dict[str, Path]) -> None:
    import stat

    cm.apply(cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"]), today="2026-10-29")
    root = env["claude"] / "repo-memory"
    for d in (root, root / "a2f0", root / "a2f0" / "demo", root / "a2f0" / "demo" / "notes"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    for f in (
        root / "a2f0" / "demo" / "a.md",
        root / "a2f0" / "demo" / "notes" / "n.md",
        root / "a2f0" / "demo" / "MEMORY.md",
    ):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


def test_worktree_paths_needing_quotes_are_found(tmp_path: Path) -> None:
    main = make_clone(tmp_path / "wt", URL)
    git_commit(main, "--allow-empty", "-m", "init")
    odd = (tmp_path / "line\nbreak").resolve()
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "--detach", str(odd)], check=True)
    # Line-based porcelain splits this path; -z keeps it whole.
    assert cm.registered_worktrees(main) == [main, odd]


def _lock_settings_after_redirect(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, then: Any = None
) -> list[Path]:
    """Redirect clone two, run *then*, and make its ``.claude`` read-only so undoing fails."""
    real = cm._redirect
    locked: list[Path] = []

    def redirect_then_lock(g: Any, clone: Path, path: Path, target: str) -> None:
        real(g, clone, path, target)
        if clone == env["two"]:
            if then is not None:
                then()
            path.parent.chmod(0o500)
            locked.append(path.parent)

    monkeypatch.setattr(cm, "_redirect", redirect_then_lock)
    return locked


def test_failed_unredirect_after_new_memory_is_reported(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    import shutil

    shutil.rmtree(source(env, "two"))

    def session_writes() -> None:
        source(env, "two").mkdir()
        (source(env, "two") / "new.md").write_text("new")

    locked = _lock_settings_after_redirect(env, monkeypatch, session_writes)
    ident.cache_path().write_text(json.dumps({"checkouts": checkouts(env)}))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: env["home"]))
    monkeypatch.setattr(cm, "live_claude_dirs", lambda: set())
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    try:
        assert cli.main(parser.parse_args(["claude-memory", "--apply"])) == 1
    finally:
        for d in locked:
            d.chmod(0o700)
    out = capsys.readouterr().out
    assert "STILL REDIRECTED" in out
    assert str(env["two"].relative_to(env["home"])) in out
    settings = json.loads((env["two"] / ".claude" / "settings.local.json").read_text())
    assert settings["autoMemoryDirectory"] == SHARED
    assert (source(env, "two") / "new.md").is_file()


def test_failed_unredirect_after_source_changed_is_reported(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    real_merged = cm._merged

    def merged_then_session_writes(g: Any, path: Path) -> bool:
        ok = real_merged(g, path)
        if path == source(env, "two"):
            # After the merge check, before archiving: the redirect is undone in the archive step.
            (path / "late.md").write_text("late")
        return ok

    monkeypatch.setattr(cm, "_merged", merged_then_session_writes)
    locked = _lock_settings_after_redirect(env, monkeypatch)
    try:
        cm.apply(plans, today="2026-10-29")
    finally:
        for d in locked:
            d.chmod(0o700)
    g = plans[0]
    assert [clone for clone, _ in g.stuck] == [env["two"]]
    assert env["two"] in g.redirected
    assert (source(env, "two") / "late.md").is_file()
    assert json.loads((env["two"] / ".claude" / "settings.local.json").read_text())["autoMemoryDirectory"] == SHARED


def test_unredirect_reports_settings_it_cannot_read(env: dict[str, Path], tmp_path: Path) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    g = plans[0]
    path = env["two"] / ".claude" / "settings.local.json"
    path.write_text("{not json")
    g.redirected.add(env["two"])
    cm._unredirect(g, env["two"], path, SHARED)
    assert [clone for clone, _ in g.stuck] == [env["two"]]
    assert path.read_text() == "{not json"


def test_unredirect_leaves_a_setting_changed_meanwhile(env: dict[str, Path]) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    g = plans[0]
    path = env["two"] / ".claude" / "settings.local.json"
    path.write_text(json.dumps({"autoMemoryDirectory": "~/elsewhere"}))
    g.redirected.add(env["two"])
    cm._unredirect(g, env["two"], path, SHARED)
    assert g.stuck == []
    assert env["two"] not in g.redirected
    assert json.loads(path.read_text()) == {"autoMemoryDirectory": "~/elsewhere"}


def test_unredirect_reports_settings_it_cannot_open(env: dict[str, Path]) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    g = plans[0]
    path = env["two"] / ".claude" / "settings.local.json"
    path.write_text(json.dumps({"autoMemoryDirectory": SHARED}))
    g.redirected.add(env["two"])
    path.chmod(0o000)
    try:
        cm._unredirect(g, env["two"], path, SHARED)
    finally:
        path.chmod(0o600)
    assert [clone for clone, _ in g.stuck] == [env["two"]]
    assert env["two"] in g.redirected
    assert json.loads(path.read_text()) == {"autoMemoryDirectory": SHARED}


def test_unreadable_settings_at_apply_time_keep_the_source(env: dict[str, Path]) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    path = env["one"] / ".claude" / "settings.local.json"
    path.chmod(0o000)
    try:
        cm.apply(plans, today="2026-10-29")
    finally:
        path.chmod(0o600)
    assert [clone for clone, _ in plans[0].failures] == [env["one"]]
    assert (source(env, "one") / "a.md").is_file()
    assert "autoMemoryDirectory" not in json.loads(path.read_text())
    assert not source(env, "two").exists()


def config_dir(home: Path, name: str, settings: str = "{}") -> Path:
    """A Claude Code config directory in *home*, marked as dotfiles mark one: with settings.json."""
    d = home / name
    d.mkdir(parents=True)
    (d / "settings.json").write_text(settings)
    return d


def memory_in(config: Path, key: str, files: dict[str, str]) -> Path:
    d = config / "projects" / key / "memory"
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
    return d


def local_settings(env: dict[str, Path], clone: str) -> dict[str, Any]:
    return cm.read_settings(env[clone] / ".claude" / "settings.local.json")[0] or {}


def test_claude_config_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    config_dir(home, ".claude")
    dotfiles = tmp_path / "dotfiles-settings.json"
    dotfiles.write_text("{}")
    (home / ".claude2").mkdir()
    (home / ".claude2" / "settings.json").symlink_to(dotfiles)  # linked in by dotfiles
    (home / ".claude-work").mkdir()
    (home / ".claude-work" / ".claude.json").write_text("{}")  # written by Claude Code
    (home / ".claude-cache" / "projects").mkdir(parents=True)  # no marker
    (home / ".claude.json").write_text("{}")  # a file, never a config directory
    (home / ".claude-default").symlink_to(home / ".claude", target_is_directory=True)  # ~/.claude again
    (home / ".claude2-link").symlink_to(home / ".claude2", target_is_directory=True)  # ~/.claude2 again
    config_dir(home, ".config")
    found = [home / ".claude", home / ".claude-work", home / ".claude2"]
    assert cm.claude_config_dirs(home, {}) == found
    assert cm.claude_config_dirs(home, {"CLAUDE_CONFIG_DIR": ""}) == found
    # The directory the variable names comes first, whether or not it exists or is marked yet.
    elsewhere = tmp_path / "elsewhere"
    assert cm.claude_config_dirs(home, {"CLAUDE_CONFIG_DIR": str(elsewhere)}) == [elsewhere, *found]
    renamed = [home / ".claude2", home / ".claude", home / ".claude-work"]
    assert cm.claude_config_dirs(home, {"CLAUDE_CONFIG_DIR": str(home / ".claude2-link")}) == [
        home / ".claude2-link",
        home / ".claude",
        home / ".claude-work",
    ]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude2"))
    assert cm.claude_config_dirs(home) == renamed
    # Without ~/.claude, a marked directory still counts; a missing home holds none, and an
    # unreadable directory is skipped rather than failing the run.
    other = tmp_path / "other"
    config_dir(other, ".claude2")
    (other / ".claude-locked").mkdir(mode=0o000)
    try:
        assert cm.claude_config_dirs(other, {}) == [other / ".claude2"]
    finally:
        (other / ".claude-locked").chmod(0o700)
    assert cm.claude_config_dirs(tmp_path / "missing", {}) == []


def test_broken_settings_link_marks_a_config_dir_that_blocks(env: dict[str, Path], tmp_path: Path) -> None:
    second = env["home"] / ".claude2"
    second.mkdir()
    (second / "settings.json").symlink_to(tmp_path / "dotfiles-not-checked-out.json")
    assert cm.claude_config_dirs(env["home"], {}) == [env["claude"], second]
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].blocked == "the user settings ~/.claude2/settings.json cannot be read; left alone"


def test_extra_config_dirs_come_last_once_each(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config_dir(home, ".claude")
    outside = tmp_path / "xdg" / "claude2"
    assert cm.claude_config_dirs(home, {}, extra=[outside, home / ".claude", outside]) == [home / ".claude", outside]


def test_memory_in_every_config_dir_is_merged(env: dict[str, Path]) -> None:
    second = config_dir(env["home"], ".claude2")
    other = memory_in(
        second,
        cm.path_key(str(env["one"])),
        {"MEMORY.md": "# Memory Index\n- [D](d.md) — d\n", "d.md": "d", "a.md": "a"},
    )
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    g = plans[0]
    assert [s.path for s in g.sources] == [source(env, "one"), other, source(env, "two")]
    assert g.dest == env["claude"] / "repo-memory" / "a2f0" / "demo"
    assert len(g.settings_changes) == 2  # one redirect per clone, whatever its config directories
    cm.apply(plans, today="2026-10-29")
    assert g.kept == []
    assert (g.dest / "d.md").read_text() == "d"
    assert "- [D](d.md) — d" in (g.dest / "MEMORY.md").read_text().splitlines()
    for path in (source(env, "one"), other, source(env, "two")):
        assert not path.exists()
        assert path.with_name("memory.merged-2026-10-29").is_dir()
    assert local_settings(env, "one")["autoMemoryDirectory"] == SHARED
    assert local_settings(env, "two")["autoMemoryDirectory"] == SHARED


def test_config_dir_named_by_the_environment_is_merged(
    env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"  # outside home and unmarked: the variable alone names it
    other = memory_in(elsewhere, cm.path_key(str(env["two"])), {"e.md": "e"})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert other in [s.path for s in plans[0].sources]
    cm.apply(plans, today="2026-10-29")
    assert (plans[0].dest / "e.md").read_text() == "e"
    assert other.with_name("memory.merged-2026-10-29").is_dir()
    assert not (elsewhere / "repo-memory").exists()  # the shared store stays under ~/.claude


def test_shared_store_is_created_without_a_default_config_dir(tmp_path: Path) -> None:
    import stat

    home = tmp_path / "home"
    clone = make_clone(home / "github" / "demo", URL)
    (clone / ".gitignore").write_text(".claude/settings.local.json\n")
    second = config_dir(home, ".claude2")
    memory = memory_in(second, cm.path_key(str(clone)), {"a.md": "a"})
    plans = cm.plan({str(clone): {"url": URL, "identity": "a2f0/demo"}}, claude_home=home / ".claude", home=home)
    cm.apply(plans, today="2026-10-29")
    assert (home / ".claude" / "repo-memory" / "a2f0" / "demo" / "a.md").read_text() == "a"
    assert stat.S_IMODE((home / ".claude").stat().st_mode) == 0o700
    assert memory.with_name("memory.merged-2026-10-29").is_dir()
    assert json.loads((clone / ".claude" / "settings.local.json").read_text())["autoMemoryDirectory"] == SHARED


def test_unmarked_claude_dir_is_not_a_config_dir(env: dict[str, Path]) -> None:
    stray = memory_in(env["home"] / ".claude-backup", cm.path_key(str(env["one"])), {"x.md": "x"})
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert stray not in [s.path for s in plans[0].sources]
    cm.apply(plans, today="2026-10-29")
    assert (stray / "x.md").read_text() == "x"
    assert not (plans[0].dest / "x.md").exists()


def test_config_dirs_sharing_a_real_path_are_read_once(env: dict[str, Path]) -> None:
    (env["claude"] / "settings.json").write_text("{}")
    (env["home"] / ".claude2").symlink_to(env["claude"], target_is_directory=True)
    third = config_dir(env["home"], ".claude3")
    (third / "projects").symlink_to(env["claude"] / "projects", target_is_directory=True)
    assert cm.claude_config_dirs(env["home"], {}) == [env["claude"], third]
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert [s.path for s in plans[0].sources] == [source(env, "one"), source(env, "two")]
    cm.apply(plans, today="2026-10-29")
    assert plans[0].kept == []
    assert not source(env, "one").exists() and not source(env, "two").exists()
    assert local_settings(env, "one")["autoMemoryDirectory"] == SHARED


def test_memory_setting_in_another_config_dirs_user_settings_is_respected(env: dict[str, Path]) -> None:
    second = config_dir(env["home"], ".claude2", json.dumps({"autoMemoryDirectory": "~/mine"}))
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].blocked == (
        "autoMemoryDirectory is set in the user settings ~/.claude2/settings.json (~/mine); left alone"
    )
    cm.apply(plans, today="2026-10-29")
    assert source(env, "one").is_dir() and source(env, "two").is_dir()
    assert "autoMemoryDirectory" not in local_settings(env, "one")
    (second / "settings.json").write_text("{not json")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].blocked == "the user settings ~/.claude2/settings.json cannot be read; left alone"


def test_alias_finds_its_memory_in_every_config_dir(env: dict[str, Path]) -> None:
    second = config_dir(env["home"], ".claude2")
    first = env["claude"] / "projects" / "-old-stealth" / "memory"
    other = memory_in(second, "-old-stealth", {"MEMORY.md": "- [E](e.md) — e\n", "e.md": "e"})
    gone = memory_in(second, "-gone", {"g.md": "g"})  # held only by the second config directory
    aliases = {"-old-stealth": "a2f0/demo", "-gone": "a2f0/gone"}
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"], aliases=aliases)
    by_identity = {g.identity: g for g in plans}
    assert {first, other} <= {s.path for s in by_identity["a2f0/demo"].sources}
    assert [s.path for s in by_identity["a2f0/gone"].sources] == [gone]
    cm.apply(plans, today="2026-10-29")
    dest = by_identity["a2f0/demo"].dest
    assert (dest / "c.md").read_text() == "c"
    assert (dest / "e.md").read_text() == "e"
    assert {"- [C](c.md) — c", "- [E](e.md) — e"} <= set((dest / "MEMORY.md").read_text().splitlines())
    assert (by_identity["a2f0/gone"].dest / "g.md").read_text() == "g"
    for path in (first, other, gone):
        assert not path.exists()
        assert path.with_name("memory.merged-2026-10-29").is_dir()


def test_conflict_between_config_dirs_keeps_both_in_place(env: dict[str, Path]) -> None:
    second = config_dir(env["home"], ".claude2")
    other = memory_in(second, cm.path_key(str(env["one"])), {"a.md": "the second account's a"})
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].conflicts == [other / "a.md"]
    cm.apply(plans, today="2026-10-29")
    assert (plans[0].dest / "a.md").read_text() == "a"
    assert (other / "a.md").read_text() == "the second account's a"
    assert (source(env, "one") / "a.md").is_file()  # its clone was not redirected, so it stays readable
    assert "autoMemoryDirectory" not in local_settings(env, "one")
    assert not source(env, "two").exists()


def test_memory_appearing_in_another_config_dir_blocks_the_redirect(env: dict[str, Path]) -> None:
    second = config_dir(env["home"], ".claude2")
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    late = memory_in(second, cm.path_key(str(env["one"])), {"late.md": "late"})
    cm.apply(plans, today="2026-10-29")
    assert dict(plans[0].kept)[late] == "memory appeared since planning; re-run to merge it"
    assert (late / "late.md").is_file()
    assert (source(env, "one") / "a.md").is_file()
    assert "autoMemoryDirectory" not in local_settings(env, "one")
    assert local_settings(env, "two")["autoMemoryDirectory"] == SHARED


def test_source_changing_in_one_config_dir_keeps_its_siblings(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    second = config_dir(env["home"], ".claude2")
    other = memory_in(second, cm.path_key(str(env["one"])), {"d.md": "d"})
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    real = cm._redirect

    def redirect_then_session_writes(g: Any, clone: Path, path: Path, target: str) -> None:
        real(g, clone, path, target)
        if clone == env["one"]:
            (other / "late.md").write_text("late")

    monkeypatch.setattr(cm, "_redirect", redirect_then_session_writes)
    cm.apply(plans, today="2026-10-29")
    # The redirect is undone, so clone one reads its own memory again in both config directories.
    assert "autoMemoryDirectory" not in local_settings(env, "one")
    assert (other / "late.md").is_file()
    assert (source(env, "one") / "a.md").is_file()
    assert {source(env, "one"), other} <= plans[0].kept_paths
    assert not source(env, "two").exists()


def test_redirect_is_undone_once_when_sources_in_two_config_dirs_change(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    second = config_dir(env["home"], ".claude2")
    other = memory_in(second, cm.path_key(str(env["one"])), {"d.md": "d"})
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    undone: list[Path] = []
    real_unredirect = cm._unredirect

    def unredirect(g: Any, clone: Path, path: Path, target: str) -> None:
        undone.append(clone)
        real_unredirect(g, clone, path, target)

    def session_writes() -> None:
        (source(env, "one") / "late.md").write_text("late")
        (other / "late.md").write_text("late")

    real_redirect = cm._redirect
    locked: list[Path] = []

    def redirect_then_lock(g: Any, clone: Path, path: Path, target: str) -> None:
        real_redirect(g, clone, path, target)
        if clone == env["one"]:
            session_writes()
            path.parent.chmod(0o500)  # so undoing the redirect fails
            locked.append(path.parent)

    monkeypatch.setattr(cm, "_unredirect", unredirect)
    monkeypatch.setattr(cm, "_redirect", redirect_then_lock)
    try:
        cm.apply(plans, today="2026-10-29")
    finally:
        for d in locked:
            d.chmod(0o700)
    g = plans[0]
    assert undone == [env["one"]]
    assert [clone for clone, _ in g.stuck] == [env["one"]]
    assert {source(env, "one"), other} <= g.kept_paths
    assert (source(env, "one") / "late.md").is_file() and (other / "late.md").is_file()


def _redirect_then(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, then: Any) -> list[Path]:
    """After clone one is redirected, run *then* and make its ``.claude`` read-only, so undoing
    the redirect fails; returns the directories to unlock."""
    real = cm._redirect
    locked: list[Path] = []

    def redirect_then_lock(g: Any, clone: Path, path: Path, target: str) -> None:
        real(g, clone, path, target)
        if clone == env["one"]:
            then()
            path.parent.chmod(0o500)
            locked.append(path.parent)

    monkeypatch.setattr(cm, "_redirect", redirect_then_lock)
    return locked


@pytest.mark.parametrize("late", ["changed", "appeared"])
def test_clone_whose_redirect_cannot_be_undone_keeps_every_source(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, late: str
) -> None:
    second = config_dir(env["home"], ".claude2")
    key = cm.path_key(str(env["one"]))
    if late == "changed":
        other = memory_in(second, key, {"d.md": "d"})
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])

    def session_writes() -> None:
        memory_in(second, key, {"late.md": "late"})

    locked = _redirect_then(env, monkeypatch, session_writes)
    try:
        cm.apply(plans, today="2026-10-29")
    finally:
        for d in locked:
            d.chmod(0o700)
    g = plans[0]
    assert [clone for clone, _ in g.stuck] == [env["one"]]
    # The setting stays until removed by hand; clone one must then find all its memory in place.
    assert dict(g.kept)[source(env, "one")] == "its clone's redirect could not be undone"
    assert (source(env, "one") / "a.md").is_file()
    if late == "changed":
        assert (other / "d.md").is_file()
    assert not source(env, "two").exists()


def test_cli_names_the_config_dirs_it_reads(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    config_dir(env["home"], ".claude2")
    outside = env["home"].parent / "xdg-claude2"  # named only by a wrapper's CLAUDE_CONFIG_DIR
    memory_in(outside, cm.path_key(str(env["two"])), {"x.md": "x"})
    ident.cache_path().write_text(json.dumps({"checkouts": checkouts(env)}))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: env["home"]))
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    assert cli.main(parser.parse_args(["claude-memory"])) == 0
    assert "Claude Code config directories: ~/.claude, ~/.claude2\n" in capsys.readouterr().out
    assert cli.main(parser.parse_args(["claude-memory", f"--config-dir={outside}"])) == 0
    out = capsys.readouterr().out
    assert f"Claude Code config directories: ~/.claude, ~/.claude2, {outside}\n" in out
    assert f"    from {outside / 'projects' / cm.path_key(str(env['two'])) / 'memory'}\n" in out


def _fake_processes(monkeypatch: pytest.MonkeyPatch, ps: str, lsof: str, ps_status: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "ps":
            return subprocess.CompletedProcess(cmd, ps_status, ps, "")
        return subprocess.CompletedProcess(cmd, 0 if lsof else 1, lsof, "")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_live_claude_dirs_finds_sessions_by_the_name_they_started_as(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    me, other = os.getuid(), os.getuid() + 1
    # A native install's kernel name is its version (2.1.325), so lsof -c claude finds nothing;
    # ps reports the name it started as, which a claude2 wrapper keeps. Another user's session
    # cannot write this user's memory, and lsof could not read its directory.
    ps = (
        f"    1     0 /sbin/launchd\n   12 {me:5} claude\n   13 {me:5} /Users/me/.local/bin/claude\n"
        f"   14 {me:5} -zsh\n   15 {me:5} node\n   16 {other:5} claude\n"
        f"   17 {me:5} /Applications/My Tools/claude\n"
    )
    calls = _fake_processes(monkeypatch, ps, f"p12\nfcwd\nn{a}\np13\nfcwd\nn{b}\np17\nfcwd\nn{b}\n")
    assert cm.live_claude_dirs() == {a.resolve(), b.resolve()}
    assert calls == [
        ["ps", "-A", "-o", "pid=", "-o", "uid=", "-o", "comm="],
        ["lsof", "-a", "-d", "cwd", "-p", "12,13,17", "-Fpn"],
    ]


def test_live_claude_dirs_without_sessions_skips_lsof(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_processes(monkeypatch, f"    1     0 /sbin/launchd\n   14 {os.getuid()} -zsh\n", "")
    assert cm.live_claude_dirs() == set()
    assert len(calls) == 1
    _fake_processes(monkeypatch, "", "", ps_status=1)
    assert cm.live_claude_dirs() is None


def test_live_claude_dirs_cannot_tell_when_a_running_session_is_unseen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    me = os.getuid()
    _fake_processes(monkeypatch, f"   12 {me} claude\n   13 {me} claude\n", f"p12\nfcwd\nn{tmp_path}\n")
    gone: set[int] = set()

    def kill(pid: int, _signal: int) -> None:
        if pid in gone:
            raise ProcessLookupError(pid)
        # Running, but lsof could not see it (from inside a sandbox, say).

    monkeypatch.setattr(os, "kill", kill)
    assert cm.live_claude_dirs() is None
    gone.add(13)  # exited between ps and lsof
    assert cm.live_claude_dirs() == {tmp_path.resolve()}


@pytest.mark.skipif(shutil.which("ps") is None, reason="needs ps")
def test_claude_pids_runs_on_this_platform() -> None:
    assert cm.claude_pids() is not None
