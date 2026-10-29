"""Resolve a checkout to its Git remote identity and render the session prompt section."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PLUGIN_NAME = "repo-context"
SKILL_CATEGORY = "repos"
SYNC_COMMAND = "hermes repo-context sync"
_GIT_TIMEOUT_S = 3
_URL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?[:/](?P<path>.+?)(?:\.git)?/*$")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_UNSAFE_TEXT_RE = re.compile(r"[`\x00-\x1f\x7f\u2028\u2029]")


def valid_identity(identity: str | None) -> bool:
    """Whether *identity* is ``owner/repo`` or ``host/group.../repo`` built only from safe name
    characters, so it can be shown in a prompt or used as a directory name."""
    if not identity:
        return False
    parts = identity.split("/")
    if len(parts) < 2 or not all(_SEGMENT_RE.match(p) and p not in (".", "..") for p in parts):
        return False
    return len(parts) == 2 or bool(_HOST_RE.match(parts[0]))


def prompt_path(path: Path | str) -> str:
    """*path* as a code span, or a neutral phrase when it holds characters that could break out
    of one; checkout paths come from the filesystem and are untrusted text."""
    text = str(path)
    return "this checkout" if _UNSAFE_TEXT_RE.search(text) else f"`{text}`"


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def data_dir() -> Path:
    """The plugin's durable data directory, which survives plugin updates."""
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_NAME)
    except ImportError:
        path = hermes_home() / "plugin-data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path


def cache_path() -> Path:
    return data_dir() / "remotes.json"


def parse_remote(url: str) -> str | None:
    """``git@github.com:a2f0/x.git`` -> ``a2f0/x``; other hosts keep a ``host/`` prefix."""
    m = _URL_RE.match((url or "").strip())
    if not m:
        return None
    host, path = m.group("host").lower(), m.group("path").strip("/")
    if "/" not in path:
        return None
    identity = path if host == "github.com" else f"{host}/{path}"
    return identity if valid_identity(identity) else None


def identity_key(identity: str) -> str:
    """Comparison key: GitHub (``owner/repo``) is case-insensitive; other hosts keep case."""
    return identity.lower() if identity.count("/") == 1 else identity


def names_identity(text: str, identity: str) -> bool:
    """Whether *text* names *identity*, case-insensitively only where the host is."""
    return identity.lower() in text.lower() if identity.count("/") == 1 else identity in text


def skill_name(identity: str) -> str | None:
    name = identity.lower().replace("/", "-")
    return name if _SKILL_NAME_RE.match(name) else None


def git_env() -> dict[str, str]:
    """The environment without GIT_*, so a session started from a hook cannot point git at
    another repository."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(cwd: str | Path, *args: str) -> str | None:
    """Run git in *cwd*; stdout on success, else None."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            env=git_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    return text if out.returncode == 0 and text else None


def remote_url(root: str | Path) -> str | None:
    url = git(root, "remote", "get-url", "origin")
    if url:
        return url
    remotes = (git(root, "remote") or "").split()
    return git(root, "remote", "get-url", remotes[0]) if remotes else None


def atomic_write(path: Path, text: str) -> None:
    """Write *path* through an exclusively created temporary file in its directory, then rename
    it into place: readers never see a partial file, and a planted temporary name or symlink is
    never written through."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_cache() -> dict[str, Any]:
    try:
        data = json.loads(cache_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def identity_for_url(url: str, cache: Mapping[str, Any] | None = None) -> str | None:
    urls = (cache if cache is not None else load_cache()).get("urls") or {}
    cached = urls.get(url)
    return cached if isinstance(cached, str) and valid_identity(cached) else parse_remote(url)


def resolve_checkout(cwd: str | Path) -> tuple[Path, str, str] | None:
    """``(root, remote_url, identity)`` for the checkout containing *cwd*, else None."""
    top = git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return None
    root = Path(top).resolve()
    if root == Path.home().resolve():
        return None
    url = remote_url(root)
    identity = identity_for_url(url) if url else None
    return (root, url, identity) if url and identity else None


def find_skill(name: str) -> Path | None:
    skills = hermes_home() / "skills"
    for pattern in (f"{name}/SKILL.md", f"*/{name}/SKILL.md"):
        for hit in skills.glob(pattern):
            if ".archive" not in hit.parts:
                return hit
    return None


def repo_skill(identity: str) -> tuple[str | None, Path | None]:
    """``(name, SKILL.md path or None)`` of the repo skill for *identity*.

    The flat ``owner-repo`` name can collide (``a-b/c`` and ``a/b-c``), so an existing skill
    counts only when its SKILL.md names *identity*; otherwise the ``owner--repo`` name is used.
    """
    name = skill_name(identity)
    if name is None:
        return None, None
    found = find_skill(name)
    if found is not None and names_identity(found.read_text(errors="replace"), identity):
        return name, found
    alternate = identity.lower().replace("/", "--")
    alt_found = find_skill(alternate) if _SKILL_NAME_RE.match(alternate) else None
    if alt_found is not None and names_identity(alt_found.read_text(errors="replace"), identity):
        return alternate, alt_found  # created while the flat name belonged to another repository
    if found is None:
        return name, None
    if alt_found is not None or not _SKILL_NAME_RE.match(alternate):
        return None, None
    return alternate, None


def matrix_workspace(root: Path) -> tuple[Path, str | None] | None:
    """``(workspace, identity)`` when *root* is ``<workspace>/checkouts/<name>`` of a matrix workspace."""
    parent = root.parent
    if parent.name != "checkouts" or not (parent.parent / "matrix.json").is_file():
        return None
    workspace = parent.parent
    url = remote_url(workspace)
    return workspace, (identity_for_url(url) if url else None)


def untrusted_project_skills(root: Path) -> bool:
    if not any((root / sub).is_dir() for sub in (".agents/skills", ".hermes/skills")):
        return False
    try:
        from agent.skill_utils import is_project_root_trusted
    except ImportError:
        return False
    return not is_project_root_trusted(root)


def render_section(cwd: str) -> str:
    """The session prompt section for *cwd*, or "" outside a checkout with a remote."""
    resolved = resolve_checkout(cwd) if cwd else None
    if resolved is None:
        return ""
    root, _url, identity = resolved
    name, skill = repo_skill(identity)
    lines = [
        "## Repository identity",
        f"- The checkout {prompt_path(root)} is a clone of `{identity}`. Hermes keys repository "
        "learning by remote, "
        f"not by path: every clone of `{identity}` shares it, so never record this path as the "
        "repository's identity.",
    ]
    if name and skill:
        lines.append(
            f"- Repo skill `{name}` holds Hermes's lessons for `{identity}`: load it with skill_view "
            "before working here, and patch repository-specific procedures, pitfalls, and the user's "
            "preferences for this repository into it."
        )
    elif name:
        lines.append(
            f"- No `{name}` skill exists yet. When you learn something specific to `{identity}` (a "
            f"procedure, pitfall, or the user's preference for this repository), create it with "
            f'skill_manage (name `{name}`, category `{SKILL_CATEGORY}`, description "Use when working '
            f'in a clone of {identity}.") rather than saving it to memory; keep `{identity}` in the '
            "skill, since that is how this plugin matches it."
        )
    lines.append(
        "- Keep memory (MEMORY.md, USER.md) for facts that hold in every repository. Lessons Claude "
        "Code and Codex also need belong in the repository itself (AGENTS.md or a repository-owned "
        "skill), shipped by PR; never edit skills agent-tool manages."
    )
    workspace = matrix_workspace(root)
    if workspace:
        ws_path, ws_identity = workspace
        ws_skill, ws_path_found = repo_skill(ws_identity) if ws_identity else (None, None)
        hint = f"; load `{ws_skill}` for its sweep rules" if ws_skill and ws_path_found else ""
        lines.append(f"- This is a managed checkout of the matrix workspace {prompt_path(ws_path)}{hint}.")
    if untrusted_project_skills(root):
        lines.append(
            "- This clone's repository skills are not trusted, so Hermes did not load them. Run "
            f"`{SYNC_COMMAND}` to review trust for clones of matrix-managed repositories (`--apply` "
            "grants it) and start a new session, "
            "or read `.agents/skills/<name>/SKILL.md` directly."
        )
    return "\n".join(lines)
