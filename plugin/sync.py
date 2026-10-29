"""Map local clones to canonical remotes and trust clones of matrix-managed repositories.

Two things still depend on a checkout's path: the canonical name of a renamed
repository (a clone of a2f0/stealth is a2f0/balebase), which needs ``gh``, and
``skills.trusted_project_dirs``, which trusts repository skills per path. ``sync``
resolves the first into the plugin's cache, so session start stays offline, and
keeps the second in step with every clone of a managed repository.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from . import identity as ident


class TrustStore(Protocol):
    def load(self) -> list[str]: ...

    def save(self, dirs: list[str]) -> None: ...


class HermesConfigTrust:
    """``skills.trusted_project_dirs`` through Hermes's own config API, as `hermes skills trust` uses."""

    def load(self) -> list[str]:
        from hermes_cli.config import load_config

        value = (load_config().get("skills") or {}).get("trusted_project_dirs") or []
        return [str(v) for v in (value if isinstance(value, list) else [value])]

    def save(self, dirs: list[str]) -> None:
        from hermes_cli.config import load_config, save_config

        config = load_config()
        config.setdefault("skills", {})["trusted_project_dirs"] = dirs
        save_config(config)


@dataclass
class SyncResult:
    checkouts: dict[str, dict[str, str]] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)
    managed: set[str] = field(default_factory=set)
    trusted_added: list[str] = field(default_factory=list)
    trusted_removed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def is_checkout(path: Path) -> bool:
    return (path / ".git").exists()


def find_clones(roots: Iterable[Path]) -> list[Path]:
    """Child clones of each root, plus the ``checkouts/`` of any matrix workspace among them."""
    clones: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
            if is_checkout(child):
                clones.append(child.resolve())
            checkouts = child / "checkouts"
            if (child / "matrix.json").is_file() and checkouts.is_dir():
                clones.extend(sorted(p.resolve() for p in checkouts.iterdir() if p.is_dir() and is_checkout(p)))
    return clones


def gh_canonical(name: str) -> str | None:
    """GitHub's current ``owner/repo`` for *name*, following renames; None when gh cannot tell."""
    try:
        out = subprocess.run(
            ["gh", "repo", "view", name, "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def managed_identities(checkouts: dict[str, dict[str, str]], trusted_paths: set[str]) -> set[str]:
    """The identity of every matrix workspace the user already trusts, and the repositories its
    ``matrix.json`` lists. A manifest in an untrusted clone grants nothing, so a cloned repository
    cannot trust itself by shipping a ``matrix.json``."""
    managed: set[str] = set()
    for path, info in checkouts.items():
        manifest = Path(path) / "matrix.json"
        if path in trusted_paths and manifest.is_file():
            managed.add(info["identity"])
            try:
                managed.update(json.loads(manifest.read_text()).get("repos") or [])
            except ValueError:
                continue
    return managed


def sync(
    roots: Iterable[Path],
    *,
    refresh: bool = False,
    apply: bool = False,
    extra_trusted: Iterable[str] = (),
    trust_store: TrustStore | None = None,
    canonical: Callable[[str], str | None] = gh_canonical,
) -> SyncResult:
    roots = [Path(r).expanduser().resolve() for r in roots]
    cache = {} if refresh else ident.load_cache()
    old_urls: dict[str, str] = dict(cache.get("urls") or {})
    resolved: dict[str, str] = {
        k: v
        for k, v in (cache.get("names") or {}).items()
        if isinstance(k, str) and ident.valid_identity(k) and isinstance(v, str) and ident.valid_identity(v)
    }
    result = SyncResult()

    def current_name(name: str) -> str:
        """GitHub's current name for *name* (renames followed), cached; other hosts unchanged."""
        if not ident.valid_identity(name):
            return name
        if name not in resolved:
            found = canonical(name)
            resolved[name] = found if ident.valid_identity(found) else name
        return resolved[name]

    for clone in find_clones(roots):
        url = ident.remote_url(clone)
        parsed = ident.parse_remote(url) if url else None
        if not url or not parsed:
            result.unresolved.append(str(clone))
            continue
        identity = old_urls.get(url)
        if not ident.valid_identity(identity):
            identity = current_name(parsed)
        result.urls[url] = identity
        result.checkouts[str(clone)] = {"url": url, "identity": identity}

    store = trust_store or HermesConfigTrust()
    trusted = store.load()
    trusted_resolved = {str(Path(t).expanduser().resolve()) for t in trusted}
    # Clone identities follow renames, so the names they are compared with must too.
    result.managed = {
        current_name(m) for m in managed_identities(result.checkouts, trusted_resolved) | set(extra_trusted)
    }
    managed_keys = {ident.identity_key(m) for m in result.managed}

    kept = [t for t in trusted if Path(t).expanduser().exists()]
    result.trusted_removed = [t for t in trusted if t not in kept]
    result.trusted_added = [
        path
        for path, info in result.checkouts.items()
        if ident.identity_key(info["identity"]) in managed_keys and path not in trusted_resolved
    ]

    # A remote URL is local metadata and proves nothing about a checkout's contents, so trust
    # changes only when the user approves the listed paths with apply; the cache is always safe.
    if apply and (result.trusted_added or result.trusted_removed):
        store.save(kept + result.trusted_added)
    dest = ident.cache_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    ident.atomic_write(
        dest,
        json.dumps(
            {
                "updated": datetime.now(UTC).isoformat(timespec="seconds"),
                "roots": [str(r) for r in roots],
                "urls": dict(sorted(result.urls.items())),
                "names": dict(sorted(resolved.items())),
                "checkouts": dict(sorted(result.checkouts.items())),
            },
            indent=2,
        )
        + "\n",
    )
    return result
