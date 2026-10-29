"""``hermes repo-context`` subcommands: sync, show, and claude-memory."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from . import claude_memory
from . import identity as ident
from . import sync as sync_mod

USAGE = "usage: hermes repo-context {sync,show,claude-memory} [options]"


def setup(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="repo_context_command")

    p = subs.add_parser("sync", help="map clones to remotes and trust clones of matrix-managed repositories")
    p.add_argument(
        "--root",
        action="append",
        type=Path,
        help="directory whose child clones to scan (default: ~/github); repeatable",
    )
    p.add_argument(
        "--trust",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="also trust clones of this repository; repeatable",
    )
    p.add_argument("--refresh", action="store_true", help="re-resolve remote names already in the cache")
    p.add_argument("--apply", action="store_true", help="make the listed trust changes (default: report them)")

    s = subs.add_parser("show", help="print the session prompt section for a directory")
    s.add_argument("path", nargs="?", default=".", help="directory inside a checkout (default: .)")

    c = subs.add_parser("claude-memory", help="share Claude Code auto memory per remote (dry run unless --apply)")
    c.add_argument("--apply", action="store_true", help="merge memory and write settings")
    c.add_argument(
        "--allow-running",
        action="store_true",
        help="apply even while Claude Code runs in an affected clone (its writes can race the merge)",
    )
    c.add_argument(
        "--alias",
        action="append",
        default=[],
        metavar="KEY=OWNER/REPO",
        help=(
            "merge a memory dir whose checkout is gone; project keys start with '-', so attach the "
            "value with '=': --alias=-Users-me-github-old=me/new; repeatable"
        ),
    )
    c.add_argument(
        "--only", action="append", default=[], metavar="OWNER/REPO", help="limit to these remotes; repeatable"
    )

    parser.set_defaults(func=main)


def main(args: argparse.Namespace) -> int:
    command = getattr(args, "repo_context_command", None)
    if command == "sync":
        return run_sync(args)
    if command == "show":
        return run_show(args)
    if command == "claude-memory":
        return run_claude_memory(args)
    print(USAGE)
    return 2


def run_sync(args: argparse.Namespace) -> int:
    roots = args.root or [Path.home() / "github"]
    result = sync_mod.sync(roots, refresh=args.refresh, apply=args.apply, extra_trusted=args.trust)
    verb = "" if args.apply else "would "
    print("Trust changes:" if args.apply else "Trust changes (review, then re-run with --apply):")
    for path in result.trusted_added:
        print(f"  {verb}trust {path}")
    for path in result.trusted_removed:
        print(f"  {verb}untrust {path} (missing)")
    if not (result.trusted_added or result.trusted_removed):
        print("  none")

    managed = {ident.identity_key(m) for m in result.managed}
    groups: dict[str, list[str]] = {}
    for path, info in result.checkouts.items():
        renamed = "R" if ident.parse_remote(info["url"]) != info["identity"] else " "
        groups.setdefault(info["identity"], []).append(f"{renamed} {path}")
    print("\nClones by remote (* = trusted repository, R = remote URL names an older repository):")
    for identity in sorted(groups, key=str.lower):
        mark = "*" if ident.identity_key(identity) in managed else " "
        _name, found = ident.repo_skill(identity)
        skill = "skill" if found else "     "
        print(f"{mark} {identity:<36} {skill} {len(groups[identity])} clone(s)")
        for line in groups[identity]:
            print(f"      {line}")
    for path in result.unresolved:
        print(f"  skipped (no GitHub remote): {path}")
    print(f"\nCache: {ident.cache_path()}")
    return 0


def run_show(args: argparse.Namespace) -> int:
    section = ident.render_section(os.path.abspath(args.path))
    print(section or "No Git checkout with a remote here; the plugin adds nothing.")
    return 0


def run_claude_memory(args: argparse.Namespace) -> int:
    checkouts = ident.load_cache().get("checkouts") or {}
    aliases = {}
    for alias in args.alias:
        key, sep, identity = alias.partition("=")
        if not sep or not key or not ident.valid_identity(identity):
            print(f"Invalid --alias {alias!r}; expected --alias=KEY=OWNER/REPO.")
            return 2
        aliases[key] = identity
    if not checkouts and not aliases:
        print(f"No clones in {ident.cache_path()}; run `{ident.SYNC_COMMAND}` first, or pass --alias.")
        return 1
    home = Path.home()
    try:
        plans = claude_memory.plan(checkouts, claude_home=home / ".claude", home=home, aliases=aliases, only=args.only)
    except ValueError as exc:
        print(f"Invalid --only: {exc}.")
        return 2
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: shared memory under ~/.claude/repo-memory\n")
    for g in plans:
        warn = ""
        if g.over_limit:
            warn = (
                f"  WARNING: index over Claude's load limit ({claude_memory.INDEX_MAX_LINES} lines / "
                f"{claude_memory.INDEX_MAX_BYTES} bytes); compact it"
            )
        print(f"{g.identity} -> {claude_memory.display(g.dest, home)}")
        print(
            f"  merge {len(g.sources)} memory dir(s): {len(g.copies)} topic file(s), index "
            f"{g.index_before} -> {len(g.index)} lines, {len(g.index_text.encode())} bytes{warn}"
        )
        for src in g.sources:
            print(f"    from {claude_memory.display(src.path, home)}")
        for f in g.conflicts:
            print(f"  CONFLICT (kept the shared copy): {claude_memory.display(f, home)}")
        for f in g.skipped:
            print(f"  SKIPPED (symlink or special file): {claude_memory.display(f, home)}")
        print(f"  settings: {claude_memory.SETTING} in {len(g.settings_changes)} clone(s)")
        for _clone, path, data, note in g.settings:
            if data is None:
                print(f"    {claude_memory.display(path, home)}: {note}")
    if args.apply and not args.allow_running:
        live = claude_memory.live_claude_dirs()
        if live is None:
            print("\nCannot tell whether Claude Code is running (lsof failed). Close its sessions in")
            print("these clones, then re-run with --allow-running.")
            return 1
        busy = claude_memory.busy_clones(plans, live)
        if busy:
            print("\nClaude Code is running in these clones and could write memory during the merge:")
            for clone in busy:
                print(f"  {claude_memory.display(clone, home)}")
            print("Close those sessions and re-run, or pass --allow-running.")
            return 1
    if args.apply:
        claude_memory.apply(plans)
    for g in plans:
        for path, reason in g.kept:
            print(f"KEPT in place ({reason}): {claude_memory.display(path, home)}")
    return 0
