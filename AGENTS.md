## Agent skills

### Coding

Use YAGNI and DRY principals.

### Issue tracker

Issues and specs live in GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Uses the five default labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Uses a single-context layout with root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.

### Phase 2: Python symbol inspection

Before editing against an external Python class or signature, optionally use host-provided Pylance MCP for targeted symbol, hover, or type queries with bounded output. If it is unavailable, use the fallback selected in issue #4 when one is adopted; otherwise continue without enrichment. Do not request full-file or full-module context dumps.
