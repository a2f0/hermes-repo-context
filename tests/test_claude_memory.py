from __future__ import annotations

import json
import subprocess
from pathlib import Path, PurePosixPath

import pytest
from repo_context import claude_memory as cm

from conftest import make_clone

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


def checkouts(env: dict[str, Path]) -> dict[str, dict[str, str]]:
    return {str(env[k]): {"url": URL, "identity": "a2f0/demo"} for k in ("one", "two")}


def source(env: dict[str, Path], clone: str) -> Path:
    return env["claude"] / "projects" / cm.path_key(str(env[clone])) / "memory"


def test_path_key_matches_claude_code() -> None:
    assert cm.path_key("/Users/me/.hermes/x.y") == "-Users-me--hermes-x-y"


def test_memory_subpath_keeps_repositories_apart() -> None:
    assert cm.memory_subpath("A2F0/Matrix") == PurePosixPath("a2f0/matrix")
    assert cm.memory_subpath("a-b/c") != cm.memory_subpath("a/b-c")
    assert str(cm.memory_subpath("gitlab.com/group/project")).startswith("gitlab.com/group/project-")
    assert cm.memory_subpath("a2f0/..") is None
    assert cm.memory_subpath("solo") is None


def test_plan_and_apply_merge_by_remote(env: dict[str, Path]) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"], aliases={"-old-stealth": "a2f0/demo"})
    assert len(plans) == 1
    g = plans[0]
    assert g.dest == env["claude"] / "repo-memory" / "a2f0" / "demo"
    assert sorted(str(rel) for _, _, rel in g.copies) == ["a.md", "b.md", "c.md", "notes/n.md"]
    assert g.index == ["# Memory Index", "- [A](a.md) — a", "- [B](b.md) — b", "- [C](c.md) — c"]
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


def test_symlinked_shared_directory_blocks_the_group(env: dict[str, Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (env["claude"] / "repo-memory" / "a2f0").mkdir(parents=True)
    (env["claude"] / "repo-memory" / "a2f0" / "demo").symlink_to(outside, target_is_directory=True)
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].blocked
    cm.apply(plans, today="2026-10-29")
    assert list(outside.iterdir()) == []
    assert source(env, "one").is_dir() and source(env, "two").is_dir()


def test_case_variants_of_a_remote_share_one_plan(env: dict[str, Path]) -> None:
    mixed = {
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


def test_case_sensitive_hosts_keep_case_variants_apart() -> None:
    upper = cm.memory_subpath("git.example.com/team/App")
    lower = cm.memory_subpath("git.example.com/team/app")
    assert upper != lower
    assert str(upper).lower() != str(lower).lower()
    assert str(upper).startswith("git.example.com/team/app-")
    # A repository named after another's hashed directory gets its own hash.
    assert cm.memory_subpath(f"git.example.com/team/{upper.name}") not in (upper, lower)


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


def test_symlinked_ancestor_of_the_shared_directory_blocks_the_group(env: dict[str, Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (env["claude"] / "repo-memory").mkdir(parents=True)
    (env["claude"] / "repo-memory" / "a2f0").symlink_to(outside, target_is_directory=True)
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    assert plans[0].blocked
    cm.apply(plans, today="2026-10-29")
    assert list(outside.iterdir()) == []
    assert source(env, "one").is_dir() and source(env, "two").is_dir()


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
    found = {
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
        assert "autoMemoryDirectory" not in cm.read_settings(env[clone] / ".claude" / "settings.local.json")[0]


def test_memory_written_after_redirect_restores_the_setting(env: dict[str, Path], monkeypatch) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    real_redirect = cm._redirect

    def redirect_then_write(g, clone, path, target):
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
        assert "autoMemoryDirectory" not in cm.read_settings(env[clone] / ".claude" / "settings.local.json")[0]
