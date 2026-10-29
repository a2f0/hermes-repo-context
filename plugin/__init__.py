"""Hermes plugin: key repository learning by Git remote instead of checkout path.

At session start the plugin resolves the working directory's checkout to its
GitHub remote (``owner/repo``; other hosts get nothing) and adds a
short, cache-safe system prompt section naming that identity and its repo skill
(``<owner>-<repo>``), so every clone of a repository points the agent at the same
lessons. ``hermes repo-context`` adds the sync, show, and claude-memory commands.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .identity import render_section

SECTION_ID = "repo-context.identity"


def _section(session_info: Mapping[str, Any]) -> str:
    return render_section(str(session_info.get("cwd") or ""))


def register(ctx) -> None:
    from . import cli

    ctx.register_system_prompt_section(SECTION_ID, _section, position="after_memory", max_chars=2000)
    ctx.register_cli_command(
        name="repo-context",
        help="key repository learning by Git remote instead of checkout path",
        setup_fn=cli.setup,
        handler_fn=cli.main,
    )
