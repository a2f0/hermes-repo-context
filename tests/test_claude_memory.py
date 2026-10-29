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

    from repo_context import cli

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

    from repo_context import cli

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


def test_index_written_by_a_session_during_apply_is_merged_not_lost(env: dict[str, Path], monkeypatch) -> None:
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    real = cm._replace_if_unchanged
    calls = []

    def session_writes_first(path, text, before):
        if path.name == "MEMORY.md" and not calls:
            calls.append(1)
            path.write_text("- [S](s.md) — written by a running session\n")
        return real(path, text, before)

    monkeypatch.setattr(cm, "_replace_if_unchanged", session_writes_first)
    cm.apply(plans, today="2026-10-29")
    index = (plans[0].dest / "MEMORY.md").read_text().splitlines()
    assert index[0] == "- [S](s.md) — written by a running session"
    assert "- [B](b.md) — b" in index


def test_fenced_blocks_differing_in_indentation_are_distinct() -> None:
    current = ["```", "  a", "```"]
    incoming = ["```", "a", "```"]
    assert cm.merge_index(current, incoming)[1] == 3


def test_memory_created_while_redirecting_undoes_the_redirect(env: dict[str, Path], monkeypatch) -> None:
    import shutil

    shutil.rmtree(source(env, "two"))
    plans = cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])
    real = cm._redirect

    def redirect_then_session_writes(g, clone, path, target):
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


def test_apply_refuses_while_claude_runs_in_a_clone(env: dict[str, Path], monkeypatch, capsys) -> None:
    import argparse

    from repo_context import cli
    from repo_context import identity as ident

    ident.cache_path().write_text(json.dumps({"checkouts": checkouts(env)}))
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: env["home"]))
    monkeypatch.setattr(cm, "live_claude_dirs", lambda: {env["one"].resolve()})
    parser = argparse.ArgumentParser()
    cli.setup(parser)
    assert cli.main(parser.parse_args(["claude-memory", "--apply"])) == 1
    assert "Claude Code is running in these clones" in capsys.readouterr().out
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
    assert plans[0].blocked == "the user settings cannot be read; left alone"
    dotfiles.write_text(json.dumps({"theme": "dark"}))
    assert not cm.plan(checkouts(env), claude_home=env["claude"], home=env["home"])[0].blocked


def test_merge_index_keeps_blank_lines_that_shape_markdown() -> None:
    incoming = ["- [A](a.md)", "", "    indented code", "", "Paragraph."]
    merged, added = cm.merge_index([], incoming)
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
    main = make_clone(home / "github" / "wt", URL)
    git = ["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    (main / ".gitignore").write_text(".claude/settings.local.json\n")
    subprocess.run([*git, "add", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    linked = (home / "github" / "wt-feature").resolve()
    subprocess.run([*git, "worktree", "add", "-q", str(linked)], check=True)
    assert cm.project_root(linked) == main
    memory = claude / "projects" / cm.path_key(str(main)) / "memory"
    memory.mkdir(parents=True)
    (memory / "a.md").write_text("a")
    found = {str(main): {"url": URL, "identity": "a2f0/demo"}, str(linked): {"url": URL, "identity": "a2f0/demo"}}
    plans = cm.plan(found, claude_home=claude, home=home)
    assert [(s.clone, s.worktrees) for s in plans[0].sources] == [(main, [linked])]
    cm.apply(plans, today="2026-10-29")
    assert (claude / "repo-memory" / "a2f0" / "demo" / "a.md").read_text() == "a"
    assert not memory.exists()
    for clone in (main, linked):
        settings = json.loads((clone / ".claude" / "settings.local.json").read_text())
        assert settings["autoMemoryDirectory"] == "~/.claude/repo-memory/a2f0/demo"
