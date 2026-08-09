# Python Object Introspection Options

_Research snapshot: 2026-08-09. Primary sources only._

## Environment verification

This Codex session does **not** expose a Pylance or Pyright semantic MCP tool. The
available tool catalog was queried for Pylance, Pyright, hover, and symbol tools;
there was no match. Therefore the host-provided Pylance path cannot be consumed
in this environment, even though Pylance documents its MCP tools as usable by
other MCP clients.

The repository environment does contain Pyright 1.1.411. `uv run pyright
--version` succeeds, and invoking `uv run pyright-langserver --version` reaches
the language server and fails only because it requires an LSP transport such as
`--stdio`. This verifies that the first-party CLI/LSP engine is locally
available, but Codex has no direct semantic-query client for it here.

## Findings

| Option | MCP / transport | Granularity and token cost | YAGNI fit |
|---|---|---|---|
| **Pyright CLI** | No MCP. Install the first-party npm package; `pyright` is the checker. | Diagnostics and JSON output, not targeted object lookup. Useful for Phase 3, not Phase 2. | Already used by qgate; no new dependency. |
| **`pyright-langserver` / LSP** | The first-party npm package exposes `pyright-langserver`; standard launch is `pyright-langserver --stdio`. | Cursor-local `textDocument/hover`, `signatureHelp`, definition/type-definition, symbols, and diagnostics. Strong semantic answer, but raw LSP needs a client and responses need truncation. | Best underlying engine; add a wrapper only if needed. ([package metadata](https://raw.githubusercontent.com/microsoft/pyright/main/packages/pyright/package.json), [language-server implementation](https://github.com/microsoft/pyright/blob/main/packages/pyright-internal/src/languageServerBase.ts)) |
| **Pylance MCP** | First-party Pylance MCP tools are documented for Copilot and “any other MCP client”; the VS Code extension supplies the host integration rather than a documented standalone PyPI/npm server. | `pylanceLSP` provides exact-position hover/diagnostics; `pylanceSemanticContext` is richer and potentially more expensive. Tools also cover imports, environments, symbols, and refactoring. | Best Phase 2 answer when the host exposes it; do not make qgate depend on it. ([tool guide](https://raw.githubusercontent.com/microsoft/pylance-release/main/docs/howto/copilot-pylance-workflow.md), [VS Code dynamic MCP API](https://code.visualstudio.com/api/references/vscode-api)) |
| **`jons-mcp-pyright`** | Community FastMCP server over **STDIO**; `uvx jons-mcp-pyright /project`. Its README includes Codex CLI and TOML setup. | Root-bound `symbol_info`, `type_info`, hover-backed navigation, diagnostics, definitions, and references. Results are structured/paginated; `type_info` is aimed at value references. | Closest direct Codex fit, but young and small. Evaluate before adopting. ([upstream README](https://github.com/jonmmease/jons-pyright-mcp)) |

The fidelity comparison is based on the documented query surfaces: Pylance and
Pyright LSP both answer at a cursor position, while the CLI produces diagnostics
rather than an interactive symbol answer. The narrow wrapper exposes that LSP
information as bounded MCP results. Only CLI and language-server startup were
tested locally; wrapper lookup fidelity was not tested because installing and
registering a third-party server is the adoption decision being deferred.

## Decision

**Defer adoption in this environment.** No Pylance semantic tool is exposed, and
writing an LSP client or registering a third-party MCP server is not justified by
a demonstrated lookup failure. Keep qgate Phase 3-only and provider-independent.

When a host exposes Pylance MCP, prefer exact-position `pylanceLSP` hover or
signature requests over rich semantic context. If a future concrete lookup need
shows that Codex requires a standalone bridge, trial `jons-mcp-pyright` first on
one representative external-symbol query and compare its bounded result with an
editor Pylance hover before adopting it.

No first-party standalone Pyright MCP server is documented in the reviewed Pyright repository/package metadata; Pyright’s first-party integration point is the CLI plus LSP. ([Pyright README](https://github.com/microsoft/pyright), [installation](https://github.com/microsoft/pyright/blob/main/docs/installation.md))
