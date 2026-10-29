# Repository guidance

Read README.md before changing the plugin. Resolve GitHub identity with
`gh repo view --json nameWithOwner`. Work on a feature branch created with
`agent-tool branch create <type>/<topic> --base main --remote origin`, preserve
unrelated edits, and use signed Conventional Commits without co-author
trailers. Do not force-push or add attribution footers.

## Setup and validation

Bun, uv, and ShellCheck are pinned in `.mise.toml`; uv selects Python from
`.python-version`. In agent shells, put the matching directories under
`~/.local/share/mise/installs` first on `PATH` rather than running
`mise trust`. Install with `bun install --frozen-lockfile` and
`uv sync --frozen`, then run `bun run hooks:install`; run it again after
returning to `main` from a merge.

Run `bun run check` for Ruff lint and format checks, the pytest suite,
ShellCheck over the hooks, the binary-file check, and the managed skill and
permission checks. When Hermes is installed, also run
`hermes plugins doctor plugin --ci` and `hermes plugins validate plugin`; they
import the plugin through Hermes's own loader. Validate expects one advisory
warning: `plugin/identity.py` calls the private
`agent.skill_utils.is_project_root_trusted`, and `plugin/sync.py` uses
`hermes_cli.config`. Both are imported lazily and fail soft; keep any new use of
Hermes internals the same way, and prefer documented `ctx` APIs.

The hooks in `scripts/git/hooks` are installed by agent-tool from
`agent-tool.json`: `commit-msg` checks subject and attribution policy,
`pre-commit` checks the branch name and staged binaries, and `pre-push` checks
hook freshness, destination branch names, commit signatures and subjects, and
binaries before running `bun run check`.

Tests must not depend on the developer's machine: `tests/conftest.py` points
`HOME`, `XDG_CONFIG_HOME`, and `HERMES_HOME` at temporary directories. Never
run `hermes repo-context sync --apply` or `claude-memory --apply` to validate a
change; they change real Hermes configuration and Claude Code memory. Use
`hermes repo-context show <path>`, and `sync` and `claude-memory` without
`--apply`.

## Output capture

Run finite, noisy commands, such as installs, builds, tests, linters, and Git
commands that run hooks, through
`bun run agent:run --label <name> -- <command> [args...]`. It keeps complete
output in a private log, preserves the exit status, and prints a compact
receipt, with a bounded excerpt of the log on failure. Read more with
`bun run agent:logs <log-path>`, raising `--tail-lines` or `--max-bytes` as
needed, or with a targeted search of the log. A zero exit does not rule out
warnings: inspect required warnings, timings, and artifacts before accepting a
check. Run reviews, version preparation, audits, infrastructure previews, and
run-group checks directly so their results stay visible, and run interactive
tools and servers directly too. Never narrow a suite or silence warnings to
reduce output.

For example, capture the tests with
`bun run agent:run --label tests -- uv run pytest`, and a push with
`bun run agent:run --label push -- bun run agent-tool branch push <head> --remote origin`;
the push log holds the pre-push hook's `check` receipt. `bun run check` is
already an `agent-tool run-group` that records each step, so run it directly.

## Shared skills and shipping

`@a2f0/agent-tool` is pinned exactly in `package.json` and `bun.lock`. After
updating it, run `bun run agents:sync` and commit the lockfile,
`.agent-tool-skills.json`, `.agent-tool-permissions.json`, the generated skills
under `.agents/skills` and `.claude/skills`, and the permission rules in
`.claude/settings.json` and `.codex/rules/` together. Do not edit managed
skills. Keep repository guidance here; do not add CLAUDE.md.

Use the installed `ship-pr` skill for authorized shipping, including an
independent review of the committed HEAD. `main` is protected by the
`protect-main` ruleset: changes land as squash-merged pull requests with signed
commits, and the required check is `CI gate` from workflow `CI`, which must be
current with `main`. `CI gate` passes only when the `lint` and `test` jobs
succeed (`scripts/checks/ci-gate.json`). Do not change protection settings as
part of routine shipping. Bump `version` in `plugin/plugin.yaml` for a change
users should pick up with `hermes plugins update repo-context`. There is no
package release or deployment workflow.

## Layout

The installable plugin is `plugin/` alone; Hermes installs it with
`hermes plugins install a2f0/hermes-repo-context/plugin`, so nothing outside it
ships. Keep repository tooling (`package.json`, `pyproject.toml`, `scripts/`)
at the root, where Hermes does not read it as plugin dependencies. The plugin
imports only the standard library; adding a runtime dependency means declaring
it in `plugin/plugin.yaml`.
