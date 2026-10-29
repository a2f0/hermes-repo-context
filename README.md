# hermes-repo-context

A [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) plugin that keys
what the agent learns about a repository by its Git remote instead of the
checkout path. Every clone of `owner/repo`, wherever it lives, points the agent
at the same repo skill, and the same idea extends to Claude Code's auto memory.

## Quick start

```sh
hermes plugins install a2f0/hermes-repo-context/plugin --enable
hermes skills trust ~/github/matrix
hermes repo-context sync          # review the paths it would trust
hermes repo-context sync --apply
hermes repo-context show ~/github/<any-clone>
```

The `/plugin` suffix installs only the plugin folder. Trusting one matrix
workspace clone yourself is what lets `sync` trust the repositories it manages.
New sessions started in a clone then carry a short `Repository identity`
section in their system prompt.

## What it does

- **Session prompt.** At session start the plugin resolves the working
  directory's checkout to its GitHub remote (`owner/repo`) and names the repo
  skill `<owner>-<repo>`: load it if it exists,
  create it in the `repos` category when a repository-specific lesson appears,
  and keep memory for facts that hold in every repository. A skill counts only
  when it names its `owner/repo`; if another repository already holds the flat
  name (`a-b/c` and `a/b-c`), the plugin uses `<owner>--<repo>`. Names are
  lowercase and turn dots into hyphens (`a2f0/a2f0.net` is `a2f0-a2f0-net`), as
  Hermes's skill linter requires; a skill created before 0.2.0 under the dotted
  name still counts, and renaming it (directory and frontmatter `name`) to the
  new name is safe. The section is
  rendered once per session, so it never breaks prompt caching, and is empty
  outside a checkout with a remote.
- **`hermes repo-context sync`** scans the clones under `~/github` (repeat
  `--root` for other directories) and the `checkouts/` of every matrix
  workspace among them. It resolves renamed repositories through `gh` into a
  cache, so session start stays offline, and lists every clone of a
  matrix-managed repository (and of the workspace itself) that is missing from
  `skills.trusted_project_dirs`. With `--apply` it adds them, so Hermes loads
  that repository's own skills in each clone, and removes trusted paths that no
  longer exist. A remote URL says nothing about what a directory contains, so
  review the list before applying. Only a `matrix.json` in a clone you already
  trust counts, so a cloned repository cannot trust itself by shipping one.
  `--trust owner/repo` covers another repository's clones too.
- **`hermes repo-context claude-memory`** shares Claude Code auto memory per
  remote. It merges each clone's `~/.claude/projects/<path>/memory` into
  `~/.claude/repo-memory/<owner>/<repo>` (lowercased, as GitHub compares
  names), sets `autoMemoryDirectory` in each
  clone's `.claude/settings.local.json` (only where Git ignores that file), and
  then renames each merged source to `memory.merged-<date>`. A source stays in
  place when a file in it differs from the shared copy, it holds a symlink, or
  its clone could not be redirected. Claude Code does not lock its memory, so
  `--apply` refuses while any Claude Code session runs; close them first (or
  pass `--allow-running`, which can lose a racing write). It is a dry run until `--apply`;
  `--alias=<project-key>=owner/repo` (with `=`, since project keys start with
  `-`) merges memory whose checkout is gone. A
  symlinked memory directory does not work, because Claude Code resolves the
  real path and refuses the write.
- **`hermes repo-context show [path]`** prints the section a session there
  would get.

Re-run `sync --apply` (and `claude-memory --apply`) after cloning. The cache lives in
`<hermes home>/plugin-data/repo-context/remotes.json`.

## Development

```sh
mise install
bun install --frozen-lockfile
uv sync --frozen
bun run hooks:install
bun run check
```

`bun run check` runs Ruff (lint and format), mypy (strict), the pytest suite,
ShellCheck, and the managed-tooling checks; the pre-push hook runs it too.
With Hermes installed, `hermes plugins doctor plugin --ci` and
`hermes plugins validate plugin` run Hermes's own loader and catalog checks.

| Path | Contents |
| --- | --- |
| `plugin/` | The installable plugin: manifest, prompt section, CLI, sync, and Claude memory merge |
| `tests/` | pytest suite, run against temporary Git repositories |
| `scripts/testing/verify.json` | The `bun run check` step list |
| `scripts/git/hooks/` | Git hooks, installed by `bun run hooks:install` |
| `scripts/checks/ci-gate.json` | The jobs `CI gate` requires |

The plugin's modules form layers, each importing only those below it:
`identity` (remote parsing, prompt section, cache) at the bottom, then
`claude_memory`, `sync`, `cli`, and the package `__init__` on top. The plugin
imports only the standard library, plus Hermes's own modules inside functions.
`tests/test_module_graph.py` enforces the layers, the absence of import cycles,
and the import rules; add a new module to its `LAYERS`.
