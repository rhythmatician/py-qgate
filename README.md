# py-qgate

Unified, zero-token-drift quality gates for Python projects across **local dev**, **pre-commit hooks**, **CI**, and **AI co-developers (Codex)**.

`py-qgate` wraps **Ruff** (formatting & linting), **Pyright / dmypy** (type checking), and custom AST guards into a single, high-speed CLI binary.

---

## Quickstart

Bootstrap quality gates into any Python repository in one command:

```bash
uvx --from git+[https://github.com/rhythmatician/py-qgate](https://github.com/rhythmatician/py-qgate) qgate init
```

This automatically scaffolds:

* `.codex/hooks.json` (PostToolUse agent feedback hook)
* `.pre-commit-config.yaml` (Sub-second local git commit gate using `dmypy`)
* `.github/workflows/ci.yml` (Authoritative GitHub Actions quality gate)
* `pyproject.toml` (Appends default `[tool.ruff]` and `[tool.pyright]` configs if missing)

---

## How It Works

`qgate` enforces a single source of truth (`pyproject.toml`) across three distinct execution modes:

| Mode | Command | Engine | Goal |
| --- | --- | --- | --- |
| **Pre-Commit** | `qgate --fix --type-checker dmypy <files>` | Ruff + `dmypy` | Sub-second (`<0.5s`) local commit check |
| **Agent Hook** | `qgate --codex-stdin --fix` | Ruff + `pyright` | Instant post-edit feedback & auto-formatting |
| **CI Gate** | `qgate --ci` | Ruff + `pyright` | Authoritative, read-only quality check |

---

## CLI Reference

```text
usage: qgate [command] [--codex-stdin] [--ci] [--fix] [--type-checker {pyright,dmypy}] [paths ...]

```

### Commands

* `qgate init`: Scaffold configuration templates into the current working directory.
* `qgate [paths...]`: Run quality checks against specific files or directories.

### Flags

* `--fix`: Run `ruff check --fix` and `ruff format` before type checking.
* `--ci`: Check all Python files in the workspace in read-only mode (fails on unformatted code).
* `--codex-stdin`: Parse JSON payloads from Codex `PostToolUse` stdin events to target modified files.
* `--type-checker {pyright,dmypy}`: Choose the type-checking engine (default: `pyright`).

---

## Exit Codes & Output Contract

* **`0`**: All checks passed. Outputs nothing to stdout/stderr (zero-token footprint for agents).
* **`2`**: One or more quality gates failed. Prints formatted diagnostic errors to `stderr`.
