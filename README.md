# py-qgate

`py-qgate` packages one consistent Python quality-gate convention for reuse across projects.
It keeps checks aligned across two axes: human and LLM-agent changes, and upstream file edits
through downstream CI verification.

It coordinates the same tools and configuration across editor formatting, coding-agent hooks,
pre-commit, and CI. For coding agents, qgate safely converts an untrusted Change Event into
eligible Gate Targets and returns one of two outcomes:

- success with no output and no agent-context cost;
- concise, actionable diagnostics for the files that changed.

The aim is to keep human developers and coding agents on the same page: both should receive the
same formatting and quality policy close to the change that triggered it. A later developer
should not discover and inherit formatting drift left by an earlier agent change.

```text
Change Event
    -> Target Selection
    -> repository-selected checks
    -> silent success | bounded diagnostics
```

## Why qgate exists

Python already has excellent formatters, linters, and type checkers. Qgate does not replace
them. It packages an opinionated way to apply them consistently across the projects where it is
installed, while supplying the missing integration seam between file-changing agents and those
deterministic tools.

The core is host-agnostic:

- treat paths reported by external tools as untrusted;
- select only existing Python files contained by the Workspace;
- check only the affected Gate Targets when possible;
- keep successful runs silent;
- keep failure feedback bounded and useful.

Codex PostToolUse events are the first supported Change Event adapter, not the limit of the
design.

## Four enforcement layers

Each layer has a distinct responsibility:

- **Editor formatting** normalizes human edits when the developer saves them.
- **Agent feedback** normalizes agent edits immediately after the file-changing action and
  reports actionable failures to the originating agent.
- **Pre-commit** catches drift before history is written, but remains a backstop: if it rewrites
  a file, the developer must review, stage, and retry the commit.
- **CI** applies the same policy without modification as the authoritative final gate.

The intended invariant is not merely identical configuration. Whether the change producer is a
human or an LLM agent, it should settle its own formatting before the change is considered
complete, rather than leaving an unrelated future editor save, commit, branch switch, or
worktree to surface the drift. Downstream gates then verify the same convention without
introducing a second interpretation of quality.

## Current scope

Qgate currently coordinates Ruff with Pyright or dmypy and includes a narrow custom AST guard.
It can also scaffold an initial Codex hook, pre-commit hook, and CI workflow. Those scaffolds are
convenience templates: repositories retain ownership of their tool configuration, editor
settings, and CI policy.

Qgate is a personal, reusable convention rather than a general-purpose quality platform. It is
deliberately not:

- a replacement for Ruff, Pyright, dmypy, pre-commit, or CI;
- a universal Python quality platform;
- a provider abstraction for agent hosts;
- a source of broad semantic or repository context for agents.

## Quickstart

Bootstrap the current templates into a Python repository:

```bash
uvx --from git+https://github.com/rhythmatician/py-qgate qgate init
```

Review generated files before committing them. Existing repositories may prefer to call qgate
from their current hooks or workflows instead of adopting every scaffold.

## Usage

```text
usage: qgate [command] [--codex-stdin] [--ci] [--fix]
             [--type-checker {pyright,dmypy}] [paths ...]
```

- `qgate init` scaffolds the current integration templates.
- `qgate <paths...>` checks selected Python files.
- `qgate --codex-stdin --fix` reads one Codex PostToolUse Change Event, applies safe Ruff fixes
  and formatting, then checks the selected Gate Targets with Pyright.
- `qgate --ci` checks all Gate Targets in the Workspace without modifying them.
- `qgate --type-checker dmypy <paths...>` uses dmypy for local file checks.

## Excluded folders

Projects can make every Python file below selected Workspace-relative folders ineligible as a
Gate Target:

```toml
[tool.qgate]
excluded-folders = ["migrations", "generated/python"]
```

Both `/` and `\\` separators are accepted. Absolute paths and paths that resolve outside the
Workspace are ignored. These exclusions apply consistently to full-Workspace checks, explicit
Candidate Paths, and Codex Change Events. Qgate's built-in exclusions for caches, environments,
artifacts, and temporary directories remain active.

Excluded folders are a clean-as-you-touch boundary: existing deferred code stays outside the
gates, but move a file out of an excluded folder (or remove the exclusion) when bringing that code
under active maintenance so Ruff, the configured type checker, and custom guards all evaluate it.

## Feedback contract

- Exit `0`: every gate passed; qgate writes nothing to stdout or stderr.
- Exit `2`: one or more gates failed; qgate writes concise diagnostics to stderr.
- Malformed or irrelevant Change Events select no Gate Targets and succeed silently.

## Design direction

Near-term work should preserve parity across the four enforcement layers while deepening the
Change Event -> Target Selection -> feedback path. Agent-specific behavior should be validated
by whether it enables self-correction with less time and context than full-repository checks.
Additional host adapters should be added only when a second real integration proves the seam,
not in anticipation of one.
