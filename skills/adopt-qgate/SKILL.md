---
name: adopt-qgate
description: Migrate an existing Python project to the qgate convention.
disable-model-invocation: true
---

# Adopt qgate

Replace a project's prototype quality-check flows with one qgate convention across editor
formatting, LLM-agent changes, pre-commit, and CI. Preserve distinct checks and project-specific
settings; remove only behavior qgate supersedes.

## 1. Establish the migration scope

Read the repository instructions and inspect the worktree before editing. Preserve unrelated
changes. Treat the whole trusted repository as the Workspace and Python files within it as
potential Gate Targets.

Inventory every place that installs, configures, invokes, or documents formatting, linting,
type checking, and custom source guards. Inspect at least:

- Python configuration and dependency manifests;
- editor and Workspace settings;
- agent hooks and agent instructions;
- pre-commit configuration;
- CI workflows;
- tox, nox, Make, task-runner, and package scripts;
- contributor documentation that names superseded commands.

Search for both tool names and behaviors. Prototype flows may call Ruff, Black, isort, Flake8,
Pyright, mypy, dmypy, or custom scripts indirectly.

Build a migration matrix with one row per discovered path:

| Layer | Existing behavior | qgate replacement | Action |
| --- | --- | --- | --- |
| Editor | | | replace / retain / reconcile |
| Agent | | | replace / retain / reconcile |
| Pre-commit | | | replace / retain / reconcile |
| CI | | | replace / retain / reconcile |

Also list quality paths outside these four layers. Finish the inventory only when every
discovered path has an action and a reason.

## 2. Classify existing behavior

Classify each path before changing it:

- **Replace** behavior covered by qgate formatting, linting, type checking, Target Selection,
  or custom guards.
- **Retain** distinct behavior such as tests, security scans, packaging checks, deployment, or
  project-specific validation outside qgate's contract.
- **Reconcile** mixed configuration or workflows by editing only the superseded portion.

Preserve project-specific Ruff and Pyright policy when it is intentional. Surface genuine
conflicts between that policy and the qgate convention before choosing one. Prefer one effective
invocation per enforcement layer over fallback copies of a prototype flow.

## 3. Install the convention

Install qgate and its checker executables into the project's locked development environment:

```bash
uv add --dev "py-qgate @ git+https://github.com/rhythmatician/py-qgate" ruff pyright mypy
uv run qgate init
```

Confirm `uv.lock` records an exact qgate Git commit. Review every write and skip reported by
initialization. Existing files are inputs to the migration, not obstacles to overwrite. Replace
generated or retained floating `uvx --from git+...` qgate commands with `uv run qgate`, and make CI
sync the committed lockfile before invoking qgate. Reconcile the four layers so they have these
roles:

- editor formatting normalizes human edits on save;
- the PostToolUse hook normalizes LLM-agent edits and returns bounded failure feedback;
- pre-commit applies the same convention to changed Gate Targets as a late backstop;
- CI checks the entire Workspace without modification.

Remove superseded commands, hooks, dependencies, and documentation only after their qgate path
is present. Edit mixed-purpose files in place; keep their unrelated responsibilities.

## 4. Bootstrap the baseline

Run `uv run qgate --fix` across the Workspace. Review the diff, including a large mechanical
formatting diff when required, then resolve the remaining diagnostics. The steady-state policy
is clean-as-you-touch: an upstream change owns every failure in its selected Gate Targets, while
CI protects the full Workspace.

Reach one successful full-Workspace CI-mode run. That green run establishes the baseline; do
not add diagnostic snapshots or changed-line suppression to carry old debt forward.

Keep the bootstrap changes uncommitted unless the user explicitly requests a commit. If a
mechanical reformat dominates the migration, make its separation from semantic fixes easy for
the user to review.

## 5. Verify parity

Verify the installed configuration rather than assuming scaffolding succeeded:

- exercise or inspect each of the four enforcement-layer commands;
- confirm agent, pre-commit, CI, and manual commands execute qgate through `uv run` from the same
  committed lockfile;
- confirm no active qgate invocation independently resolves a Git branch, tag, or package version;
- confirm upstream layers fix/check selected Gate Targets rather than sweeping unrelated files;
- confirm CI performs a read-only full-Workspace check;
- confirm successful agent checks emit no stdout or stderr;
- search again for obsolete prototype invocations and dependencies;
- run the repository's relevant tests for any configuration or code changed during migration.

Follow every yielded or live validation session through terminal completion. Record its final
exit code; count only exit code 0 as success. Treat silence as the expected success output only
after observing that exit code.

Finish only when every inventory row is accounted for, retained checks still run, superseded
paths are gone, `uv run qgate --fix` and `uv run --locked qgate --ci` both exit 0, and every other
validation has a recorded terminal exit code. Report the resulting four-layer matrix, the removed
and retained behavior, validation performed, and any intentionally deferred work.
