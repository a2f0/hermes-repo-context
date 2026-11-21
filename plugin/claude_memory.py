"""Share Claude Code auto memory per Git remote instead of per checkout path.

Claude Code keeps auto memory at ``<config dir>/projects/<path-key>/memory``, so each
clone of a repository learns separately, and so does each Claude Code config directory
(``claude_config_dirs``): the one ``$CLAUDE_CONFIG_DIR`` names, ``~/.claude``, and any
other ``~/.claude*`` directory holding ``settings.json`` or ``.claude.json``, such as a
second account's ``~/.claude2``. ``plan`` and ``apply`` fold each clone's memory
directory from every config directory into ``~/.claude/repo-memory/<owner>/<repo>``
(files copied by relative path, ``MEMORY.md`` index lines appended when new), set
``autoMemoryDirectory`` in each clone's gitignored ``.claude/settings.local.json``,
which sessions of every config directory read, and only then rename each merged
source to ``memory.merged-<date>``. A source that cannot be merged completely stays
in place and its clone is not redirected, so Claude Code keeps seeing it. Nothing is
written through a symlink inside the shared directory. A symlinked memory directory
does not work for Claude Code either: it resolves the real path and denies the write.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Literal, assert_never

from .identity import CheckoutInfo, git, git_env, remote_url, valid_identity

INDEX = "MEMORY.md"
INDEX_MAX_LINES, INDEX_MAX_BYTES = 200, 25_000
SETTING = "autoMemoryDirectory"
SETTINGS_FILE = ".claude/settings.local.json"
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
# Files that mark a home directory's other .claude* entries as Claude Code config directories:
# Claude Code writes .claude.json in a directory it runs with, and dotfiles link settings.json in.
CONFIG_MARKERS = ("settings.json", ".claude.json")


def claude_runs_with(config_dir: Path, home: Path, environ: Mapping[str, str] | None = None) -> str:
    """Why Claude Code is known to run with *config_dir* (so excluding it would hide memory a
    redirected clone no longer reads), or "" when discovery only guessed it from a marker."""
    env = os.environ if environ is None else environ
    real = os.path.realpath(config_dir)
    if real == os.path.realpath(home / ".claude"):
        return "it is ~/.claude"
    named = env.get(CONFIG_DIR_ENV)
    if named and real == os.path.realpath(os.path.abspath(named)):
        return f"${CONFIG_DIR_ENV} names it"
    if os.path.lexists(config_dir / ".claude.json"):
        return "it holds .claude.json, which Claude Code writes in a directory it runs with"
    return ""


def claude_config_dirs(
    home: Path,
    environ: Mapping[str, str] | None = None,
    extra: Iterable[Path] = (),
    exclude: Iterable[Path] = (),
) -> list[Path]:
    """Every Claude Code config directory, once each by real path: the one ``$CLAUDE_CONFIG_DIR``
    names (when set and non-empty), ``~/.claude`` (when it exists), then, by name, every other
    directory directly in *home* whose name starts with ``.claude`` and that holds one of
    ``CONFIG_MARKERS`` (a broken link counts, so its unreadable settings block the run as they
    would in ``~/.claude``), then *extra*; none whose real path is one of *exclude*, such as a
    backup copy of ``~/.claude``. A file such as ``~/.claude.json`` never counts."""
    env = os.environ if environ is None else environ
    candidates = []
    named = env.get(CONFIG_DIR_ENV)
    if named:
        candidates.append(Path(os.path.abspath(named)))
    default = home / ".claude"
    if os.path.isdir(default):
        candidates.append(default)
    try:
        others = sorted(p for p in home.iterdir() if p.name.startswith(".claude") and p.name != default.name)
    except OSError:
        others = []
    # os.path, not Path: it answers False rather than raising for a directory it cannot read.
    candidates += [
        p for p in others if os.path.isdir(p) and any(os.path.lexists(p / marker) for marker in CONFIG_MARKERS)
    ]
    candidates += [Path(os.path.abspath(p)) for p in extra]
    dirs: list[Path] = []
    seen: set[str] = {os.path.realpath(p) for p in exclude}
    for candidate in candidates:
        real = os.path.realpath(candidate)
        if real not in seen:
            seen.add(real)
            dirs.append(candidate)
    return dirs


def is_ignored(clone: Path, relative: str) -> bool:
    """Whether Git ignores *relative* in *clone*, so writing it leaves the checkout clean."""
    try:
        out = subprocess.run(
            ["git", "-C", str(clone), "check-ignore", "-q", relative],
            capture_output=True,
            timeout=10,
            env=git_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def project_root(clone: Path) -> Path:
    """The main checkout of *clone*'s repository: Claude Code keys auto memory by it, so every
    worktree shares the main checkout's memory directory."""
    common = git(clone, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        common_path = Path(common)
        if common_path.name == ".git":
            return common_path.parent
    return clone


def registered_worktrees(clone: Path) -> list[Path]:
    """Every existing worktree of *clone*'s repository, the main checkout included."""
    # -z: NUL-separated fields, so a path with quotes or newlines arrives verbatim.
    out = git(clone, "worktree", "list", "--porcelain", "-z") or ""
    trees = []
    for field_ in out.split("\0"):
        if field_.startswith("worktree "):
            tree = Path(field_[len("worktree ") :]).resolve()
            if tree.is_dir():
                trees.append(tree)
    return trees


def path_key(path: str) -> str:
    """Claude Code's project directory name for a checkout path."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def memory_subpath(identity: str) -> PurePosixPath | None:
    """``A2F0/Matrix`` -> ``a2f0/matrix``: always two levels, lowercased as GitHub compares names,
    so two repositories never share or nest a directory. None for anything else."""
    if not valid_identity(identity):
        return None
    return PurePosixPath(*identity.lower().split("/"))


def display(path: Path, home: Path) -> str:
    return "~/" + str(path.relative_to(home)) if path.is_relative_to(home) else str(path)


# Claude Code's project directory names: path_key() output, which only ever starts with "-".
_PROJECT_KEY_RE = re.compile(r"^-[A-Za-z0-9-]*$")
_SECTION_RE = re.compile(r"^#{2,6}\s")
_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def _level(heading: str) -> int:
    return len(heading) - len(heading.lstrip("#"))


_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _sections(lines: list[str]) -> list[tuple[tuple[str, ...], list[str]]]:
    """An index split at its ``##``-and-deeper headings, each keyed by its heading path (its
    ancestors' headings and its own); ``()`` holds what precedes the first. A ``#`` title is
    ordinary text, so every index's title section lines up. Each body is a list of units: a
    line, or a whole fenced block (its lines joined by newlines), so merging never splits one."""
    sections: list[tuple[tuple[str, ...], list[str]]] = [((), [])]
    chain: list[str] = []
    fence: str | None = None
    block: list[str] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if fence is not None:
            block.append(line)
            if line.strip().startswith(fence):
                sections[-1][1].append("\n".join(block))
                fence, block = None, []
            continue
        m = _FENCE_RE.match(line)
        if m:
            fence, block = m.group(1), [line]
        elif _SECTION_RE.match(line):
            heading = line.rstrip()
            chain = [h for h in chain if _level(h) < _level(heading)] + [heading]
            sections.append((tuple(chain), []))
        else:
            sections[-1][1].append(line)
    if block:  # an unclosed fence runs to the end
        sections[-1][1].append("\n".join(block))
    return sections


def _unit_key(unit: str) -> str:
    """A line compares without surrounding space unless it is indented code (a tab or four
    spaces); a fenced block keeps its indentation."""
    if "\n" in unit:
        return "\n".join(part.rstrip() for part in unit.split("\n"))
    if unit.startswith(("\t", "    ")):
        return unit.rstrip()
    return unit.strip()


def merge_index(current: list[str], incoming: list[str]) -> tuple[list[str], int]:
    """*current* with what *incoming* adds, and how many lines that is. Each unit (a line or a
    whole fenced block) goes under the same heading path it had in *incoming*, so a repeated
    heading under two parents stays two sections; units compare as multisets within a section,
    so blocks stay intact and an entry filed under two headings survives. A section *current*
    lacks is inserted after its nearest present ancestor's subtree (at the end without one).
    Existing lines keep their order and form."""
    sections = _sections(current)
    added = 0

    def find(path: tuple[str, ...]) -> int | None:
        return next((i for i, (p, _) in enumerate(sections) if p == path), None)

    for path, units in _sections(incoming):
        idx = find(path)
        if idx is None:
            pos = len(sections)
            for n in range(len(path) - 1, 0, -1):
                a = find(path[:n])
                if a is not None:
                    pos = a + 1
                    while pos < len(sections) and sections[pos][0][:n] == path[:n]:
                        pos += 1
                    break
            before = sections[pos - 1][1]
            if before and before[-1].strip():
                before.append("")
            sections.insert(pos, (path, []))
            idx = pos
            added += 1
        body = sections[idx][1]
        have = Counter(_unit_key(u) for u in body if _unit_key(u))
        seen: Counter[str] = Counter()
        new: list[str] = []
        gap = False  # a blank line separates this unit from the previous one in *incoming*
        for unit in units:
            key = _unit_key(unit)
            if not key:
                gap = True
                continue
            seen[key] += 1
            if seen[key] > have[key]:
                # Keep the blank line before a new unit: Markdown reads paragraphs, lists, and
                # indented code differently without it.
                if gap and (new or (body and body[-1].strip())):
                    new.append("")
                new.append(unit)
            gap = False
        if not new:
            continue
        added += sum(unit.count("\n") + 1 for unit in new)
        end = len(body)
        while end and not body[end - 1].strip():
            end -= 1
        body[end:end] = new
    out: list[str] = []
    for path, body in sections:
        if path:
            out.append(path[-1])
        for unit in body:
            out.extend(unit.split("\n"))
    return out, added


def links_into(line: str, held: set[PurePosixPath]) -> bool:
    """Whether *line* links to a file in *held* or beneath a directory in it."""
    return any(t == h or h in t.parents for t in link_targets(line) for h in held)


def link_targets(line: str) -> set[PurePosixPath]:
    """Relative files an index line links to."""
    targets = set()
    for link in _LINK_RE.findall(line):
        path = re.split(r"[#?]", link, maxsplit=1)[0]
        if path and ":" not in path and not path.startswith("/"):
            targets.add(PurePosixPath(urllib.parse.unquote(path)))
    return targets


def symlink_under(dest: Path, rel: PurePosixPath) -> bool:
    """Whether ``dest/rel`` or any directory between *dest* and it is a symlink."""
    current = dest
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def fingerprint(path: Path) -> tuple[tuple[str, int, int], ...]:
    """Name, size, and mtime of everything under *path*, to notice a running session's writes."""
    entries = []
    for f in path.rglob("*"):
        try:
            st = f.lstat()
        except OSError:
            continue
        entries.append((f.relative_to(path).as_posix(), st.st_size, st.st_mtime_ns))
    return tuple(sorted(entries))


@dataclass
class Source:
    path: Path
    clone: Path | None  # None for an alias whose checkout is gone
    fingerprint: tuple[tuple[str, int, int], ...] = ()
    worktrees: list[Path] = field(default_factory=list)  # other worktrees of the clone's repository

    @property
    def all_clones(self) -> list[Path]:
        return ([self.clone] if self.clone is not None else []) + self.worktrees

    def changed(self) -> bool:
        return fingerprint(self.path) != self.fingerprint


# One clone's redirect: which memory its Claude Code sessions read, as planning found it and as
# apply changes it. Every config directory's sessions read the same settings file, so one state
# covers the clone in all of them.


@dataclass(frozen=True)
class OwnMemory:
    """It reads its own memory directories: not redirected."""

    status: Literal["own"] = "own"


@dataclass(frozen=True)
class SharedBefore:
    """It read the shared directory before this run; apply never undoes that."""

    status: Literal["shared_before"] = "shared_before"


@dataclass(frozen=True)
class RedirectedNow:
    """This run pointed it at the shared directory by writing *value* to *path*, and may still
    undo that while its memory merges."""

    path: Path
    value: str
    status: Literal["redirected_now"] = "redirected_now"


@dataclass(frozen=True)
class Undone:
    """This run redirected it, then it was pointed back at its own memory (by apply, or by a
    session that changed the setting meanwhile)."""

    status: Literal["undone"] = "undone"


@dataclass(frozen=True)
class RedirectFailed:
    """Writing the redirect failed, so it still reads its own memory."""

    reason: str
    status: Literal["failed"] = "failed"


@dataclass(frozen=True)
class Stuck:
    """This run redirected it and could not undo that: its own memory stays unread until the
    setting is removed by hand."""

    reason: str
    status: Literal["stuck"] = "stuck"


RedirectState = OwnMemory | SharedBefore | RedirectedNow | Undone | RedirectFailed | Stuck


@dataclass(frozen=True)
class Wrote:
    """The redirect was written."""

    path: Path
    value: str


@dataclass(frozen=True)
class WriteFailed:
    """The redirect could not be written."""

    reason: str


@dataclass(frozen=True)
class Restored:
    """The setting no longer names the shared directory: apply removed it, or a session changed it."""


@dataclass(frozen=True)
class RestoreFailed:
    """Removing the setting failed."""

    reason: str


RedirectEvent = Wrote | WriteFailed | Restored | RestoreFailed


def redirect_transition(state: RedirectState, event: RedirectEvent) -> RedirectState:
    """The next state of a clone's redirect. Each event applies in one state and is ignored in
    every other: only a clone reading its own memory is redirected, and only a redirect this run
    made is undone, at most once."""
    match event:
        case Wrote(path=path, value=value):
            return RedirectedNow(path, value) if isinstance(state, OwnMemory) else state
        case WriteFailed(reason=reason):
            return RedirectFailed(reason) if isinstance(state, OwnMemory) else state
        case Restored():
            return Undone() if isinstance(state, RedirectedNow) else state
        case RestoreFailed(reason=reason):
            return Stuck(reason) if isinstance(state, RedirectedNow) else state
        case _:
            assert_never(event)


def reads_shared(state: RedirectState) -> bool:
    """Whether a clone in *state* reads the shared directory."""
    match state:
        case SharedBefore() | RedirectedNow() | Stuck():
            return True
        case OwnMemory() | Undone() | RedirectFailed():
            return False
        case _:
            assert_never(state)


@dataclass
class GroupPlan:
    identity: str
    root: Path
    dest: Path
    sources: list[Source] = field(default_factory=list)
    copies: list[tuple[Path, Path, PurePosixPath]] = field(default_factory=list)  # (source dir, file, rel)
    providers: dict[PurePosixPath, list[Path]] = field(default_factory=dict)  # rel -> sources holding it
    digests: dict[PurePosixPath, str] = field(default_factory=dict)  # rel -> planned content hash
    # clone -> its memory directory in each config directory
    clone_sources: dict[Path, list[Path]] = field(default_factory=dict)
    target: str = ""  # the autoMemoryDirectory value that points at dest
    clones: list[Path] = field(default_factory=list)  # every clone of this repository
    watch: set[Path] = field(default_factory=set)  # other clones whose memory directory is a source
    # clone -> its memory directories (one per config directory) that an alias claims
    claims: dict[Path, list[Path]] = field(default_factory=dict)
    # clone -> its redirect, once planning or apply has touched it (OwnMemory until then)
    redirects: dict[Path, RedirectState] = field(default_factory=dict)
    failed_rels: set[PurePosixPath] = field(default_factory=set)  # copies that failed at apply time
    conflicts: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    index_before: int = 0
    index: list[str] = field(default_factory=list)
    settings: list[tuple[Path, Path, dict[str, Any] | None, str]] = field(default_factory=list)
    kept: list[tuple[Path, str]] = field(default_factory=list)
    blocked: str = ""

    @property
    def index_text(self) -> str:
        return "\n".join(self.index) + "\n" if self.index else ""

    @property
    def over_limit(self) -> bool:
        return len(self.index) > INDEX_MAX_LINES or len(self.index_text.encode()) > INDEX_MAX_BYTES

    @property
    def settings_changes(self) -> list[tuple[Path, Path, dict[str, Any]]]:
        return [(clone, path, data) for clone, path, data, _ in self.settings if data is not None]

    def redirect(self, clone: Path) -> RedirectState:
        return self.redirects.get(clone, OwnMemory())

    def advance(self, clone: Path, event: RedirectEvent) -> None:
        self.redirects[clone] = redirect_transition(self.redirect(clone), event)

    @property
    def redirected(self) -> set[Path]:
        """Clones that read the shared directory."""
        return {clone for clone, state in self.redirects.items() if reads_shared(state)}

    @property
    def failures(self) -> list[tuple[Path, str]]:
        """Clones apply could not redirect, and why."""
        return [(clone, s.reason) for clone, s in self.redirects.items() if isinstance(s, RedirectFailed)]

    @property
    def stuck(self) -> list[tuple[Path, str]]:
        """Clones apply redirected but could not point back at memory that stayed in place."""
        return [(clone, s.reason) for clone, s in self.redirects.items() if isinstance(s, Stuck)]

    @property
    def kept_paths(self) -> set[Path]:
        return {path for path, _ in self.kept}

    def keep(self, path: Path, reason: str) -> None:
        if path not in self.kept_paths:
            self.kept.append((path, reason))

    def archivable(self) -> list[Source]:
        """Sources that are fully merged and whose clone, if any, reads the shared directory."""
        return [s for s in self.sources if s.path not in self.kept_paths]


def _clashes(rel: PurePosixPath, files: Iterable[PurePosixPath]) -> bool:
    """Whether *rel* needs a directory where a file is, or is a file where a directory is."""
    files = set(files)
    if any(parent in files for parent in rel.parents if parent != PurePosixPath(".")):
        return True
    return any(rel in f.parents for f in files)


def read_settings(path: Path) -> tuple[dict[str, Any] | None, str]:
    """``(settings, "")`` for a clone's settings file (``{}`` when absent), or ``(None, why)``."""
    if path.is_symlink() or path.parent.is_symlink():
        return None, "settings file or .claude is a symlink; left alone"
    if not path.is_file():
        return {}, ""
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return None, "unparsable; left alone"
    if not isinstance(data, dict):
        return None, "not a JSON object; left alone"
    return data, ""


def configured_memory(path: Path) -> tuple[str | None, bool]:
    """``(autoMemoryDirectory or None, readable)`` from a settings file this tool never writes.
    It is only read, so a symlink (a dotfiles checkout, say) is followed; unreadable is False."""
    if not path.exists():
        return None, not path.is_symlink()  # a dangling link is unreadable
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return None, False
    if not isinstance(data, dict):
        return None, False
    value = data.get(SETTING)
    return (str(value) if value else None), True


def _plan_files(g: GroupPlan) -> None:
    existing: dict[PurePosixPath, bytes] = {}
    if g.dest.is_dir():
        for f in g.dest.rglob("*"):
            if f.is_file() and not f.is_symlink():
                existing[PurePosixPath(f.relative_to(g.dest).as_posix())] = f.read_bytes()
    index_rel = PurePosixPath(INDEX)
    for src in g.sources:
        unmerged: set[PurePosixPath] = set()  # files of this source that stay behind

        def conflict(
            f: Path, rel: PurePosixPath, reason: str, src: Source = src, unmerged: set[PurePosixPath] = unmerged
        ) -> None:
            g.conflicts.append(f)
            g.keep(src.path, reason)
            unmerged.add(rel)

        index_file = None
        for f in sorted(src.path.rglob("*")):
            rel = PurePosixPath(f.relative_to(src.path).as_posix())
            if f.is_symlink() or not (f.is_file() or f.is_dir()):
                g.skipped.append(f)
                g.keep(src.path, "holds a symlink or special file")
                unmerged.add(rel)
            elif f.is_dir():
                continue
            elif symlink_under(g.dest, rel):
                conflict(f, rel, "the shared copy is behind a symlink")
            elif rel not in existing and (_clashes(rel, existing.keys()) or (g.dest / rel).is_dir()):
                conflict(f, rel, "a file and a directory share a name")
            elif rel == index_rel:
                index_file = f
            elif rel in existing and existing[rel] != f.read_bytes():
                conflict(f, rel, "a file differs from the shared copy")
            elif rel in g.providers:
                # Identical to a copy another source provides: this source depends on it too.
                g.providers[rel].append(src.path)
            elif rel not in existing:
                g.copies.append((src.path, f, rel))
                g.providers[rel] = [src.path]
                existing[rel] = f.read_bytes()
                g.digests[rel] = hashlib.sha256(existing[rel]).hexdigest()
        if index_file is not None:
            # An entry for a file that stays behind stays behind with it.
            lines = [line for line in index_file.read_text().splitlines() if not links_into(line, unmerged)]
            g.index, _ = merge_index(g.index, lines)


def _plan_settings(g: GroupPlan, clones: list[Path], target: str, unclean: set[Path]) -> None:
    unclean = unclean | {c for s in g.sources if s.path in g.kept_paths for c in s.all_clones}
    for clone in clones:
        path = clone / SETTINGS_FILE
        project_value, project_ok = configured_memory(clone / ".claude" / "settings.json")
        if project_value or not project_ok:
            note = f"set to {project_value}" if project_value else "unreadable"
            g.settings.append((clone, path, None, f"the project's settings are {note}; left alone"))
            continue
        data, note = read_settings(path)
        if data is None:
            g.settings.append((clone, path, None, note))
            continue
        current = data.get(SETTING)
        if current == target:
            g.redirects[clone] = SharedBefore()
        elif current:
            g.settings.append((clone, path, None, f"already set to {current}; left alone"))
        elif clone in unclean:
            g.settings.append((clone, path, None, "left alone until its memory merges cleanly"))
        elif not is_ignored(clone, SETTINGS_FILE):
            g.settings.append((clone, path, None, "not gitignored in this clone; left alone"))
        else:
            g.settings.append((clone, path, {**data, SETTING: target}, "set"))


def _is_mergeable(path: Path, dest: Path) -> bool:
    """A real, non-empty memory directory other than the shared one."""
    return path.is_dir() and not path.is_symlink() and any(path.iterdir()) and path.resolve() != dest.resolve()


def _dest_problem(root: Path, dest: Path) -> str:
    """Why *dest* must not be written, or "" when it is safe: *root* (``repo-memory``) and every
    directory from it down to *dest* must be a real directory or not exist yet."""
    current = root
    for part in (None, *dest.relative_to(root).parts):
        if part is not None:
            current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            return f"{current} is a symlink or not a directory"
    if (dest / INDEX).is_symlink():
        return f"the shared {INDEX} is a symlink"
    return ""


def plan(
    checkouts: Mapping[str, CheckoutInfo],
    *,
    claude_home: Path,
    home: Path,
    config_dirs: Sequence[Path] | None = None,
    aliases: dict[str, str] | None = None,
    only: Iterable[str] = (),
) -> list[GroupPlan]:
    """Plan the merge of the memory each clone (and each ``--alias`` project key) holds under
    ``projects/`` in every Claude Code config directory. *claude_home* (``~/.claude``) only
    holds the shared store, ``repo-memory``; the sources come from *config_dirs*, which
    defaults to ``claude_config_dirs(home)``."""
    dirs = claude_config_dirs(home) if config_dirs is None else list(config_dirs)
    # Two config directories can share one projects directory through a symlink; read it once.
    projects_dirs: list[Path] = []
    seen_projects: set[str] = set()
    for config_dir in dirs:
        real = os.path.realpath(config_dir / "projects")
        if real not in seen_projects:
            seen_projects.add(real)
            projects_dirs.append(config_dir / "projects")
    canonical_root = claude_home / "repo-memory"
    # A user-level setting moves every project's memory out of projects/; leave that to the user.
    user_setting = ""
    for config_dir in dirs:
        user_path = display(config_dir / "settings.json", home)
        user_value, user_ok = configured_memory(config_dir / "settings.json")
        # Discovery can mistake another tool's ~/.claude-* directory for Claude Code's.
        guessed = not claude_runs_with(config_dir, home)
        hint = f" (not Claude Code's? pass --exclude-config-dir={display(config_dir, home)})" if guessed else ""
        if user_value:
            user_setting = f"{SETTING} is set in the user settings {user_path} ({user_value}); left alone{hint}"
        elif not user_ok:
            user_setting = f"the user settings {user_path} cannot be read; left alone{hint}"
        if user_setting:
            break
    worktrees_of: dict[Path, list[Path]] = {}

    def worktrees(clone: Path) -> list[Path]:
        if clone not in worktrees_of:
            worktrees_of[clone] = registered_worktrees(clone)
        return worktrees_of[clone]

    remotes: dict[Path, str | None] = {}

    def remote(clone: Path) -> str | None:
        if clone not in remotes:
            remotes[clone] = remote_url(clone)
        return remotes[clone]

    # Group by destination, not by spelling: Owner/Repo and owner/repo share one directory.
    names: dict[PurePosixPath, str] = {}
    clones: dict[PurePosixPath, list[Path]] = {}
    sources: dict[PurePosixPath, list[Source]] = {}
    stale: dict[PurePosixPath, list[tuple[Path, str]]] = {}
    claims: dict[PurePosixPath, dict[Path, list[Path]]] = {}
    # Live clones first, so their index lines lead the merged MEMORY.md; then aliases. An alias
    # names the owner of a memory directory explicitly, so it claims that directory from any
    # checkout that maps to it; the checkout itself can still be redirected. A clone or alias
    # has one memory directory in each config directory, each a separate source.
    for key in aliases or {}:
        if not _PROJECT_KEY_RE.match(key):
            raise ValueError(f"invalid project key {key!r}; expected a name from a Claude Code projects directory")
    alias_keys = set(aliases or {})
    claimants: dict[Path, list[Path]] = {}  # aliased memory directory -> clones that map to it
    # (identity, memory source, cached remote URL or None for an alias, reused, claimed)
    entries: list[tuple[str, Source, str | None, bool, bool]] = []
    alias_subs = {key: memory_subpath(identity) for key, identity in (aliases or {}).items()}
    owned_keys: set[str] = set()  # aliases that name the checkout's own repository
    for path, info in checkouts.items():
        key = path_key(str(project_root(Path(path))))
        # An alias naming the checkout's own repository confirms that it owns its memory,
        # reused or not; one naming another repository claims the memory from it.
        owned = key in alias_keys and alias_subs[key] == memory_subpath(info["identity"])
        if owned:
            owned_keys.add(key)
        claimed = key in alias_keys and not owned
        if claimed:
            for projects in projects_dirs:
                watchers = claimants.setdefault(projects / key / "memory", [])
                for tree in [Path(path), *worktrees(Path(path))]:
                    if tree not in watchers:
                        watchers.append(tree)
        reused = bool(info.get("reused")) and not claimed and not owned
        entries += [
            (info["identity"], Source(projects / key / "memory", Path(path)), info.get("url"), reused, claimed)
            for projects in projects_dirs
        ]
    entries += [
        (identity, Source(projects / key / "memory", None), None, False, False)
        for key, identity in (aliases or {}).items()
        if key not in owned_keys
        for projects in projects_dirs
    ]
    # Worktrees of one repository share its memory directory (Claude Code keys it by the main
    # checkout): one source carries them all, and each worktree's settings are redirected.
    primary: dict[tuple[Path, PurePosixPath | None], Source] = {}
    merged_entries = []
    for entry in entries:
        identity, src, _url, _reused, claimed = entry
        slot = (src.path, memory_subpath(identity))
        if src.clone is not None and not claimed and slot in primary:
            primary[slot].worktrees.append(src.clone)
            continue
        if src.clone is not None and not claimed:
            primary[slot] = src
        merged_entries.append(entry)
    entries = merged_entries
    # Worktrees outside the scanned directories (.claude/worktrees, say) read the same memory.
    for src in primary.values():
        if src.clone is None:
            continue
        for tree in worktrees(src.clone):
            if tree not in src.all_clones:
                src.worktrees.append(tree)
    # Claude Code's project key folds every non-alphanumeric character to "-", so foo-bar and
    # foo_bar share one memory directory; such a directory belongs to no single clone.
    users: dict[Path, int] = {}
    for _identity, src, _url, _reused, claimed in entries:
        if not claimed:
            users[src.path] = users.get(src.path, 0) + 1
    for identity, src, url, reused, claimed in entries:
        sub = memory_subpath(identity)
        if sub is None:
            continue
        names.setdefault(sub, identity)
        clones.setdefault(sub, [])
        sources.setdefault(sub, [])
        # The cache can be older than the checkout: a path reused for another repository must
        # not pour its memory into the cached one.
        if src.clone is not None and (not url or remote(src.clone) != url):
            stale.setdefault(sub, []).append((src.path, "its clone's remote no longer matches the cache; re-run sync"))
            continue
        if claimed and src.clone is not None:
            # The checkout and every worktree of it read the claimed directory today.
            for tree in [src.clone, *worktrees(src.clone)]:
                if tree not in clones[sub]:
                    clones[sub].append(tree)
                claimed_dirs = claims.setdefault(sub, {}).setdefault(tree, [])
                if src.path not in claimed_dirs:
                    claimed_dirs.append(src.path)
            continue
        if users[src.path] > 1:
            stale.setdefault(sub, []).append(
                (src.path, "Claude Code shares this directory with another path; assign it with --alias")
            )
            continue
        if reused:
            stale.setdefault(sub, []).append(
                (src.path, "this path has held another repository; assign its memory with --alias")
            )
            continue
        sources[sub].append(src)
        for clone in src.all_clones:
            if clone not in clones[sub]:
                clones[sub].append(clone)
    wanted = set()
    for o in only:
        sub = memory_subpath(o)
        if sub is None:
            raise ValueError(f"invalid repository {o!r}; expected OWNER/REPO")
        wanted.add(sub)

    plans = []
    for sub in sorted(sources):
        if wanted and sub not in wanted:
            continue
        dest = canonical_root / sub
        g = GroupPlan(identity=names[sub], root=canonical_root, dest=dest)
        g.sources = [s for s in sources[sub] if _is_mergeable(s.path, dest)]
        for s in g.sources:
            s.fingerprint = fingerprint(s.path)
        for s in sources[sub]:
            for c in s.all_clones:
                g.clone_sources.setdefault(c, []).append(s.path)
        g.target = display(dest, home)
        g.clones = list(clones[sub])
        g.watch = {c for s in sources[sub] for c in claimants.get(s.path, [])}
        g.claims = {clone: list(paths) for clone, paths in claims.get(sub, {}).items()}
        for stale_path, reason in stale.get(sub, []):
            # Only memory that exists stays in place; most clones have none in some config directory.
            if stale_path.exists() or stale_path.is_symlink():
                g.keep(stale_path, reason)
        # A clone whose memory directory is a symlink is not merged, so it must not be redirected.
        unclean = set()
        for s in sources[sub]:
            if s.path.is_symlink():
                g.keep(s.path, "the memory directory is a symlink")
                unclean.update(s.all_clones)
        g.blocked = user_setting or _dest_problem(canonical_root, dest)
        if g.blocked:
            for s in g.sources:
                g.keep(s.path, g.blocked)
            plans.append(g)
            continue
        existing_index = (dest / INDEX).read_text().splitlines() if (dest / INDEX).is_file() else []
        g.index_before = len(existing_index)
        g.index = list(existing_index)
        _plan_files(g)
        _plan_settings(g, clones[sub], display(dest, home), unclean)
        redirectable = g.redirected | {clone for clone, _, _ in g.settings_changes}
        for s in g.sources:
            if any(c not in redirectable for c in s.all_clones):
                g.keep(s.path, "its clone's settings were not redirected")
        if g.sources or g.settings or g.kept:
            plans.append(g)
    return plans


def _replace_if_unchanged(path: Path, text: str, before: bytes | None) -> bool:
    """Write *text* to *path* atomically, but only if *path* still holds *before* (None: absent)
    once the new content is ready; False when a running session changed it meanwhile."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        now = path.read_bytes() if path.is_file() else None
        if now != before:
            os.unlink(tmp)
            return False
        os.replace(tmp, path)
        return True
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _private_dirs(base: Path, path: Path) -> None:
    """Create *path* and each missing directory between *base* and it as ``0700``."""
    current = base
    for part in path.relative_to(base).parts:
        current = current / part
        with contextlib.suppress(FileExistsError):
            current.mkdir(mode=0o700)


def _write_index(g: GroupPlan) -> None:
    """Merge the planned index lines into the shared index as it is now, retrying when a running
    session writes it meanwhile; if it keeps changing, the sources stay in place (``_merged``)."""
    index = g.dest / INDEX
    # Entries for copies that failed at apply time stay behind with their source.
    planned = [line for line in g.index if not links_into(line, g.failed_rels)]
    for _attempt in range(5):
        before = index.read_bytes() if index.is_file() else None
        current = before.decode(errors="replace").splitlines() if before is not None else []
        merged, added = merge_index(current, planned)
        if not added or _replace_if_unchanged(index, "\n".join(merged) + "\n", before):
            return


def _keep_clone(g: GroupPlan, clone: Path, reason: str) -> None:
    """Keep the sources *clone* reads, and record that *clone* was not redirected."""
    g.advance(clone, WriteFailed(reason))
    for s in g.sources:
        if clone in s.all_clones:
            g.keep(s.path, reason)


def _redirect(g: GroupPlan, clone: Path, path: Path, target: str) -> None:
    """Point *clone* at the shared directory, re-reading its settings now so changes made since
    planning (permissions, or this very setting) are kept rather than replaced."""
    try:
        before = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    except OSError as exc:
        _keep_clone(g, clone, f"reading its settings failed: {exc}")
        return
    data, note = read_settings(path)
    if data is None:
        _keep_clone(g, clone, f"its settings changed since planning: {note}")
        return
    current = data.get(SETTING)
    if current and current != target:
        _keep_clone(g, clone, f"its settings changed since planning: {SETTING} is {current}")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        written = _replace_if_unchanged(path, json.dumps({**data, SETTING: target}, indent=2) + "\n", before)
    except OSError as exc:
        _keep_clone(g, clone, f"writing its settings failed: {exc}")
        return
    if not written:
        _keep_clone(g, clone, "its settings changed while being written; re-run")
        return
    g.advance(clone, Wrote(path, target))


def _copy(g: GroupPlan, src_dir: Path, f: Path, rel: PurePosixPath) -> None:
    def failed(reason: str) -> None:
        # Every source holding an identical copy relied on this one, so none of them merged.
        g.conflicts.append(f)
        g.failed_rels.add(rel)
        for provider in g.providers.get(rel, [src_dir]):
            g.keep(provider, reason)

    if symlink_under(g.dest, rel):
        failed("the shared copy is behind a symlink")
        return
    if f.is_symlink() or not f.is_file():
        failed("the source file changed since planning")
        return
    target = g.dest / rel
    content = f.read_bytes()
    if hashlib.sha256(content).hexdigest() != g.digests.get(rel):
        failed("the source file changed since planning")
        return
    try:
        _private_dirs(g.dest, target.parent)
        st = f.stat()
        # Written under a private temporary name (mkstemp: 0600, as the source may have been
        # readable only by its owner), then hard-linked into place: the link is exclusive, so a
        # file written since planning is never overwritten, and a write that fails or is
        # interrupted never leaves a partial copy under the shared name.
        # A short fixed prefix: the target's own name can already be as long as the filesystem allows.
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".repo-context-copy.")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(content)
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.link(tmp, target)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    except FileExistsError:
        if not target.is_file() or target.is_symlink() or target.read_bytes() != content:
            failed("a file differs from the shared copy")
        return
    except (NotADirectoryError, IsADirectoryError):  # a file where a directory is needed
        failed("a file and a directory share a name")
        return
    except OSError as exc:
        failed(f"copying it failed: {exc}")
        return


def _unredirect(g: GroupPlan, clone: Path, path: Path, target: str) -> None:
    """Undo a redirect made in this run, so a clone whose memory changed keeps reading it. The
    settings are re-read and replaced only if unchanged meanwhile; a clone that still reads the
    shared directory afterwards is ``Stuck``, since its own memory stays unread."""
    reason = "its settings kept changing; check them"
    for _attempt in range(5):
        try:
            before = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        except OSError as exc:
            reason = f"reading its settings failed: {exc}"
            break
        data, note = read_settings(path)
        if data is None:
            reason = f"its settings cannot be restored: {note}"
            break
        if data.get(SETTING) != target:
            g.advance(clone, Restored())  # a session changed it meanwhile; it no longer reads dest
            return
        data.pop(SETTING)
        try:
            written = _replace_if_unchanged(path, json.dumps(data, indent=2) + "\n", before)
        except OSError as exc:
            reason = f"restoring its settings failed: {exc}"
            break
        if written:
            g.advance(clone, Restored())
            return
    g.advance(clone, RestoreFailed(reason))


def claude_pids() -> set[int] | None:
    """This user's running Claude Code processes, or None when ``ps`` fails: those whose name
    (argv[0] on macOS, the process name on Linux) starts with ``claude``, as ``lsof -c claude``
    matched. A ``claude2`` shell function still runs ``claude``, and a ``claude2`` link to the
    binary runs as ``claude2``; another ``claude-*`` tool only makes apply refuse, which
    ``--allow-running`` overrides. The kernel's own name for a native install is its versioned
    binary (``2.1.325``), so ``lsof -c claude`` alone finds none of those sessions on macOS.
    Another user's sessions cannot write this user's private memory, and a zombie writes nothing."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=", "-o", "uid=", "-o", "stat=", "-o", "comm="],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    uid = str(os.getuid())
    pids: set[int] = set()
    for line in out.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 4 or not fields[0].isdigit() or fields[1] != uid or fields[2].startswith("Z"):
            continue
        if fields[3].strip().rsplit("/", 1)[-1].startswith("claude"):
            pids.add(int(fields[0]))
    return pids


def live_claude_dirs() -> set[Path] | None:
    """Working directories of this user's running Claude Code processes, or None when they
    cannot all be read: ``ps`` or ``lsof`` failed, or lsof cannot see a session that still
    runs (from inside a sandbox, say)."""
    pids = claude_pids()
    if pids is None:
        return None
    if not pids:
        return set()
    try:
        out = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-p", ",".join(map(str, sorted(pids))), "-Fpn"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode not in (0, 1):  # 1: a listed process was not found
        return None
    dirs: set[Path] = set()
    seen: set[int] = set()
    pid: int | None = None
    for line in out.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            seen.add(pid)
            dirs.add(Path(line[1:]).resolve())
    for missing in pids - seen:
        try:
            os.kill(missing, 0)
        except ProcessLookupError:
            continue  # it exited after ps listed it
        except OSError:
            pass  # it exists but cannot be signalled
        return None
    return dirs


def busy_clones(plans: list[GroupPlan], live: set[Path]) -> list[Path]:
    """Clones in *plans* that a running Claude Code session works in (its cwd at or below them).
    The CLI refuses ``apply`` while any session runs; this names the ones in affected clones."""
    busy = set()
    for g in plans:
        for clone in {*g.clone_sources, *g.clones, *g.watch}:
            real = clone.resolve()
            if any(d == real or real in d.parents for d in live):
                busy.add(clone)
    return sorted(busy)


def _has_files(path: Path) -> bool:
    return path.is_symlink() or (path.is_dir() and any(path.iterdir()))


def _merged(g: GroupPlan, source: Path) -> bool:
    """Whether the shared directory holds every file of *source* unchanged and every line of its
    index, so archiving it loses nothing; checked after copying, just before redirecting."""
    current = (g.dest / INDEX).read_text().splitlines() if (g.dest / INDEX).is_file() else []
    for f in source.rglob("*"):
        if f.is_dir() and not f.is_symlink():
            continue
        rel = PurePosixPath(f.relative_to(source).as_posix())
        if rel == PurePosixPath(INDEX):
            if merge_index(current, f.read_text().splitlines())[1]:
                return False
            continue
        target = g.dest / rel
        if f.is_symlink() or symlink_under(g.dest, rel) or not target.is_file():
            return False
        if target.read_bytes() != f.read_bytes():
            return False
    return True


def apply(plans: list[GroupPlan], *, today: str | None = None) -> None:
    """Copy, redirect, then archive: a source is renamed only once its clone reads the shared
    directory, so Claude Code never loses sight of a memory it was using."""
    stamp = today or date.today().isoformat()
    # Aliased memory first, so a clone whose old directory an alias claims can be redirected
    # once that directory is merged and archived.
    for g in sorted(plans, key=lambda g: not g.watch):
        if g.blocked or _dest_problem(g.root, g.dest):
            continue
        # ~/.claude itself is missing when only another config directory is in use.
        with contextlib.suppress(FileExistsError):
            g.root.parent.mkdir(mode=0o700)
        _private_dirs(g.root.parent, g.dest)
        for src_dir, f, rel in g.copies:
            _copy(g, src_dir, f, rel)
        _write_index(g)
        for s in g.sources:
            if s.path in g.kept_paths:
                continue
            if s.changed():
                g.keep(s.path, "changed since planning; re-run to merge it")
            elif not _merged(g, s.path):
                g.keep(s.path, "the shared directory does not hold all of it")
        planned = {s.path for s in g.sources}
        for clone, path, data in g.settings_changes:
            claimed = [p for p in g.claims.get(clone, []) if p.exists() or p.is_symlink()]
            if claimed:
                # Its old memory belongs to an alias that has not merged it; keep reading it.
                for p in claimed:
                    g.keep(p, "its alias has not merged it yet; re-run")
                continue
            appeared = [p for p in _unplanned(g, [clone], planned) if _has_files(p)]
            if appeared:
                for p in appeared:
                    g.keep(p, "memory appeared since planning; re-run to merge it")
                continue
            if not any(clone in s.all_clones and s.path in g.kept_paths for s in g.sources):
                _redirect(g, clone, path, data[SETTING])
        # Memory a session created while the settings were being written was never planned.
        for clone in [c for c, state in g.redirects.items() if isinstance(state, RedirectedNow)]:
            appeared = [p for p in _unplanned(g, [clone], planned) if _has_files(p)]
            if appeared:
                for p in appeared:
                    g.keep(p, "memory appeared while redirecting; re-run to merge it")
                _undo(g, clone)
        # A clone reads a memory directory in each config directory, so all of them are checked
        # together just before any of its sources is renamed, and its redirect is undone at most once.
        units: dict[frozenset[Path], list[Source]] = {}
        for src in g.archivable():
            units.setdefault(frozenset(src.all_clones) or frozenset({src.path}), []).append(src)
        for unit in units.values():
            clones = unit[0].all_clones
            if not all(_reads(c, g.target) for c in clones):
                for src in unit:
                    g.keep(src.path, "its clone no longer reads the shared directory")
                continue
            changed = [src for src in unit if src.changed()]
            for src in changed:
                g.keep(src.path, "changed since planning; re-run to merge it")
            # Memory a session created since the redirect, in a config directory that had none.
            appeared = [p for p in dict.fromkeys(_unplanned(g, clones, planned)) if _has_files(p)]
            for p in appeared:
                g.keep(p, "memory appeared while redirecting; re-run to merge it")
            if changed or appeared:
                for c in clones:
                    _undo(g, c)
            rest = [src for src in unit if src not in changed]
            if any(isinstance(g.redirect(c), Stuck) for c in clones):
                # Its setting must be removed by hand; it must then find all its memory in place.
                for src in rest:
                    g.keep(src.path, "its clone's redirect could not be undone")
                continue
            if (changed or appeared) and not all(_reads(c, g.target) for c in clones):
                # Undone: the clone reads its own memory again, in every config directory.
                for src in rest:
                    g.keep(src.path, "its clone no longer reads the shared directory")
                continue
            for src in rest:
                _archive(g, src, stamp)


def _undo(g: GroupPlan, clone: Path) -> None:
    """Undo *clone*'s redirect if this run made it; any other redirect stays."""
    state = g.redirect(clone)
    if isinstance(state, RedirectedNow):
        _unredirect(g, clone, state.path, state.value)


def _unplanned(g: GroupPlan, clones: Iterable[Path], planned: set[Path]) -> list[Path]:
    """The memory directories of *clones* that apply is not merging."""
    return [p for c in clones for p in g.clone_sources.get(c, []) if p not in planned]


def _reads(clone: Path, target: str) -> bool:
    """Whether *clone*'s settings point Claude Code at *target* now."""
    return (read_settings(clone / SETTINGS_FILE)[0] or {}).get(SETTING) == target


def _archive(g: GroupPlan, src: Source, stamp: str) -> None:
    """Rename a merged source to ``memory.merged-<stamp>`` (``.2`` and on when taken)."""
    target = src.path.with_name(f"memory.merged-{stamp}")
    n = 1
    while target.exists():
        n += 1
        target = src.path.with_name(f"memory.merged-{stamp}.{n}")
    try:
        src.path.rename(target)
    except OSError as exc:
        g.keep(src.path, f"archiving it failed: {exc}")
