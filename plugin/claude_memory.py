"""Share Claude Code auto memory per Git remote instead of per checkout path.

Claude Code keeps auto memory at ``~/.claude/projects/<path-key>/memory``, so each
clone of a repository learns separately. ``plan`` and ``apply`` fold each clone's
memory directory into ``~/.claude/repo-memory/<owner>/<repo>`` (files copied by
relative path, ``MEMORY.md`` index lines appended when new), set
``autoMemoryDirectory`` in each clone's gitignored ``.claude/settings.local.json``,
and only then rename the merged source to ``memory.merged-<date>``. A source that
cannot be merged completely stays in place and its clone is not redirected, so
Claude Code keeps seeing it. Nothing is written through a symlink inside the
shared directory. A symlinked memory directory does not work for Claude Code
either: it resolves the real path and denies the write.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath

from .identity import atomic_write as _atomic_write
from .identity import git_env, remote_url, valid_identity

INDEX = "MEMORY.md"
INDEX_MAX_LINES, INDEX_MAX_BYTES = 200, 25_000
SETTING = "autoMemoryDirectory"
SETTINGS_FILE = ".claude/settings.local.json"


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

    def changed(self) -> bool:
        return fingerprint(self.path) != self.fingerprint


@dataclass
class GroupPlan:
    identity: str
    root: Path
    dest: Path
    sources: list[Source] = field(default_factory=list)
    copies: list[tuple[Path, Path, PurePosixPath]] = field(default_factory=list)  # (source dir, file, rel)
    providers: dict[PurePosixPath, list[Path]] = field(default_factory=dict)  # rel -> sources holding it
    digests: dict[PurePosixPath, str] = field(default_factory=dict)  # rel -> planned content hash
    clone_sources: dict[Path, Path] = field(default_factory=dict)  # clone -> its memory directory
    target: str = ""  # the autoMemoryDirectory value that points at dest
    clones: list[Path] = field(default_factory=list)  # every clone of this repository
    watch: set[Path] = field(default_factory=set)  # other clones whose memory directory is a source
    failed_rels: set[PurePosixPath] = field(default_factory=set)  # copies that failed at apply time
    conflicts: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    index_before: int = 0
    index: list[str] = field(default_factory=list)
    settings: list[tuple[Path, Path, dict | None, str]] = field(default_factory=list)
    redirected: set[Path] = field(default_factory=set)
    kept: list[tuple[Path, str]] = field(default_factory=list)
    blocked: str = ""

    @property
    def index_text(self) -> str:
        return "\n".join(self.index) + "\n" if self.index else ""

    @property
    def over_limit(self) -> bool:
        return len(self.index) > INDEX_MAX_LINES or len(self.index_text.encode()) > INDEX_MAX_BYTES

    @property
    def settings_changes(self) -> list[tuple[Path, Path, dict]]:
        return [(clone, path, data) for clone, path, data, _ in self.settings if data is not None]

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


def read_settings(path: Path) -> tuple[dict | None, str]:
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
    unclean = unclean | {s.clone for s in g.sources if s.path in g.kept_paths}
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
            g.redirected.add(clone)
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
    checkouts: dict[str, dict[str, str]],
    *,
    claude_home: Path,
    home: Path,
    aliases: dict[str, str] | None = None,
    only: Iterable[str] = (),
) -> list[GroupPlan]:
    projects = claude_home / "projects"
    canonical_root = claude_home / "repo-memory"
    # A user-level setting moves every project's memory out of projects/; leave that to the user.
    user_value, user_ok = configured_memory(claude_home / "settings.json")
    user_setting = (
        f"{SETTING} is set in the user settings ({user_value}); left alone"
        if user_value
        else ""
        if user_ok
        else "the user settings cannot be read; left alone"
    )
    # Group by destination, not by spelling: Owner/Repo and owner/repo share one directory.
    names: dict[PurePosixPath, str] = {}
    clones: dict[PurePosixPath, list[Path]] = {}
    sources: dict[PurePosixPath, list[Source]] = {}
    stale: dict[PurePosixPath, list[tuple[Path, str]]] = {}
    # Live clones first, so their index lines lead the merged MEMORY.md; then aliases. An alias
    # names the owner of a memory directory explicitly, so it claims that directory from any
    # checkout that maps to it; the checkout itself can still be redirected.
    for key in aliases or {}:
        if not _PROJECT_KEY_RE.match(key):
            raise ValueError(f"invalid project key {key!r}; expected a name from ~/.claude/projects")
    alias_keys = set(aliases or {})
    claimants: dict[Path, list[Path]] = {}  # aliased memory directory -> clones that map to it
    entries = []
    for path, info in checkouts.items():
        key = path_key(path)
        claimed = key in alias_keys
        if claimed:
            claimants.setdefault(projects / key / "memory", []).append(Path(path))
        reused = bool(info.get("reused")) and not claimed
        entries.append(
            (info["identity"], Source(projects / key / "memory", Path(path)), info.get("url"), reused, claimed)
        )
    entries += [
        (identity, Source(projects / key / "memory", None), None, False, False)
        for key, identity in (aliases or {}).items()
    ]
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
        if src.clone is not None and (not url or remote_url(src.clone) != url):
            stale.setdefault(sub, []).append((src.path, "its clone's remote no longer matches the cache; re-run sync"))
            continue
        if claimed:
            clones[sub].append(src.clone)
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
        if src.clone is not None:
            clones[sub].append(src.clone)
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
        g.clone_sources = {s.clone: s.path for s in sources[sub] if s.clone is not None}
        g.target = display(dest, home)
        g.clones = list(clones[sub])
        g.watch = {c for s in sources[sub] for c in claimants.get(s.path, [])}
        for path, reason in stale.get(sub, []):
            g.keep(path, reason)
        # A clone whose memory directory is a symlink is not merged, so it must not be redirected.
        unclean = set()
        for s in sources[sub]:
            if s.path.is_symlink():
                g.keep(s.path, "the memory directory is a symlink")
                if s.clone is not None:
                    unclean.add(s.clone)
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
            if s.clone is not None and s.clone not in redirectable:
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
    for s in g.sources:
        if s.clone == clone:
            g.keep(s.path, reason)


def _redirect(g: GroupPlan, clone: Path, path: Path, target: str) -> None:
    """Point *clone* at the shared directory, re-reading its settings now so changes made since
    planning (permissions, or this very setting) are kept rather than replaced."""
    before = path.read_bytes() if path.is_file() and not path.is_symlink() else None
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
    g.redirected.add(clone)


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
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as out:  # never overwrite a file written since planning
            out.write(content)
    except FileExistsError:
        if not target.is_file() or target.is_symlink() or target.read_bytes() != content:
            failed("a file differs from the shared copy")
        return
    except OSError:  # a file where a directory is needed, or the reverse
        failed("a file and a directory share a name")
        return
    shutil.copystat(f, target)


def _unredirect(g: GroupPlan, clone: Path, path: Path, target: str) -> None:
    """Undo a redirect made in this run, so a clone whose memory changed keeps reading it."""
    data, _note = read_settings(path)
    if data is not None and data.get(SETTING) == target:
        data.pop(SETTING)
        with contextlib.suppress(OSError):
            _atomic_write(path, json.dumps(data, indent=2) + "\n")
            g.redirected.discard(clone)


def live_claude_dirs() -> set[Path] | None:
    """Working directories of running Claude Code processes, or None when lsof cannot tell."""
    try:
        out = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-c", "claude", "-Fn"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode not in (0, 1):  # 1: no such process
        return None
    return {Path(line[1:]).resolve() for line in out.stdout.splitlines() if line.startswith("n")}


def busy_clones(plans: list[GroupPlan], live: set[Path]) -> list[Path]:
    """Clones in *plans* that a running Claude Code session works in (its cwd at or below them).
    Claude Code does not lock its memory files, so ``apply`` needs those sessions closed."""
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
    for g in plans:
        if g.blocked or _dest_problem(g.root, g.dest):
            continue
        g.dest.mkdir(parents=True, exist_ok=True)
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
        redirected_now: dict[Path, tuple[Path, str]] = {}
        for clone, path, data in g.settings_changes:
            own = g.clone_sources.get(clone)
            if own is not None and own not in planned and _has_files(own):
                g.keep(own, "memory appeared since planning; re-run to merge it")
                continue
            if not any(s.clone == clone and s.path in g.kept_paths for s in g.sources):
                _redirect(g, clone, path, data[SETTING])
                if clone in g.redirected:
                    redirected_now[clone] = (path, data[SETTING])
        # Memory a session created while the settings were being written was never planned.
        for clone, (path, target) in list(redirected_now.items()):
            own = g.clone_sources.get(clone)
            if own is not None and own not in planned and _has_files(own):
                g.keep(own, "memory appeared while redirecting; re-run to merge it")
                _unredirect(g, clone, path, target)
                del redirected_now[clone]
        for src in g.archivable():
            if src.clone is not None and (read_settings(src.clone / SETTINGS_FILE)[0] or {}).get(SETTING) != g.target:
                g.keep(src.path, "its clone no longer reads the shared directory")
                continue
            if src.changed():
                g.keep(src.path, "changed since planning; re-run to merge it")
                if src.clone in redirected_now:
                    _unredirect(g, src.clone, *redirected_now[src.clone])
                continue
            target = src.path.with_name(f"memory.merged-{stamp}")
            n = 1
            while target.exists():
                n += 1
                target = src.path.with_name(f"memory.merged-{stamp}.{n}")
            try:
                src.path.rename(target)
            except OSError as exc:
                g.keep(src.path, f"archiving it failed: {exc}")
