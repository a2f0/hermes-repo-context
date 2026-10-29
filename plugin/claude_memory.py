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
import re
import shutil
import subprocess
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
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


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


_SECTION_RE = re.compile(r"^#{2,6}\s")
_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def _sections(lines: list[str]) -> list[tuple[str | None, list[str]]]:
    """An index split at its ``##``-and-deeper headings; ``None`` holds what precedes the first.
    A ``#`` title is ordinary text, so every index's title section lines up."""
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in lines:
        if _SECTION_RE.match(line):
            sections.append((line.rstrip(), []))
        else:
            sections[-1][1].append(line.rstrip("\n"))
    return sections


def merge_index(current: list[str], incoming: list[str]) -> tuple[list[str], int]:
    """*current* with the lines of *incoming* it lacks, each under the heading it had in
    *incoming* (a heading *current* lacks is appended with its lines), and how many lines that
    added. Lines are compared as multisets within a section, so repeated lines such as code fences
    and an entry filed under two headings survive. Existing lines keep their order and form."""
    sections = _sections(current)
    added = 0
    for heading, lines in _sections(incoming):
        body = next((b for h, b in sections if h == heading), None)
        have = Counter(line.strip() for line in body or [] if line.strip())
        seen: Counter[str] = Counter()
        new = []
        for line in lines:
            text = line.strip()
            if not text:
                continue
            seen[text] += 1
            if seen[text] > have[text]:
                new.append(line)
        if not new:
            continue
        added += len(new)
        if body is None:
            last = sections[-1][1]
            if last and last[-1].strip():
                last.append("")
            sections.append((heading, new))
            added += 1
            continue
        end = len(body)
        while end and not body[end - 1].strip():
            end -= 1
        body[end:end] = new
    out: list[str] = []
    for heading, body in sections:
        if heading is not None:
            out.append(heading)
        out.extend(body)
    return out, added


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
            lines = [line for line in index_file.read_text().splitlines() if not (link_targets(line) & unmerged)]
            g.index, _ = merge_index(g.index, lines)


def _plan_settings(g: GroupPlan, clones: list[Path], target: str, unclean: set[Path]) -> None:
    unclean = unclean | {s.clone for s in g.sources if s.path in g.kept_paths}
    for clone in clones:
        path = clone / SETTINGS_FILE
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
    # Group by destination, not by spelling: Owner/Repo and owner/repo share one directory.
    names: dict[PurePosixPath, str] = {}
    clones: dict[PurePosixPath, list[Path]] = {}
    sources: dict[PurePosixPath, list[Source]] = {}
    stale: dict[PurePosixPath, list[tuple[Path, str]]] = {}
    # Live clones first, so their index lines lead the merged MEMORY.md; then aliases.
    entries = [
        (info["identity"], Source(projects / path_key(path) / "memory", Path(path)), info.get("url"))
        for path, info in checkouts.items()
    ]
    entries += [(identity, Source(projects / key / "memory", None), None) for key, identity in (aliases or {}).items()]
    # Claude Code's project key folds every non-alphanumeric character to "-", so foo-bar and
    # foo_bar share one memory directory; such a directory belongs to no single clone.
    users: dict[Path, int] = {}
    for _identity, src, _url in entries:
        users[src.path] = users.get(src.path, 0) + 1
    for identity, src, url in entries:
        sub = memory_subpath(identity)
        if sub is None:
            continue
        names.setdefault(sub, identity)
        clones.setdefault(sub, [])
        sources.setdefault(sub, [])
        if users[src.path] > 1:
            stale.setdefault(sub, []).append((src.path, "Claude Code shares this directory with another path"))
            continue
        # The cache can be older than the checkout: a path reused for another repository must
        # not pour its memory into the cached one.
        if src.clone is not None and (not url or remote_url(src.clone) != url):
            stale.setdefault(sub, []).append((src.path, "its clone's remote no longer matches the cache; re-run sync"))
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
        for path, reason in stale.get(sub, []):
            g.keep(path, reason)
        # A clone whose memory directory is a symlink is not merged, so it must not be redirected.
        unclean = set()
        for s in sources[sub]:
            if s.path.is_symlink():
                g.keep(s.path, "the memory directory is a symlink")
                if s.clone is not None:
                    unclean.add(s.clone)
        g.blocked = _dest_problem(canonical_root, dest)
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


def _write_index(g: GroupPlan) -> None:
    """Append the planned index lines to the shared index as it is now, so lines a running
    session added since planning survive."""
    index = g.dest / INDEX
    current = index.read_text().splitlines() if index.is_file() else []
    # Entries for copies that failed at apply time stay behind with their source.
    planned = [line for line in g.index if not (link_targets(line) & g.failed_rels)]
    merged, added = merge_index(current, planned)
    if added:
        _atomic_write(index, "\n".join(merged) + "\n")


def _keep_clone(g: GroupPlan, clone: Path, reason: str) -> None:
    for s in g.sources:
        if s.clone == clone:
            g.keep(s.path, reason)


def _redirect(g: GroupPlan, clone: Path, path: Path, target: str) -> None:
    """Point *clone* at the shared directory, re-reading its settings now so changes made since
    planning (permissions, or this very setting) are kept rather than replaced."""
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
        _atomic_write(path, json.dumps({**data, SETTING: target}, indent=2) + "\n")
    except OSError as exc:
        _keep_clone(g, clone, f"writing its settings failed: {exc}")
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
