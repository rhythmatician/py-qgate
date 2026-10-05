# Quality Gates

The Quality Gates context identifies which Python files are eligible for automated formatting, linting, type checking, and custom guards across development-tool workflows.

## Language

**Workspace**:
The trusted project directory within which Gate Targets may exist. A reported working directory does not change the Workspace.
_Avoid_: Working directory, repository root

**Change Event**:
A best-effort notification from an external development tool about a completed action. A malformed Change Event produces no Candidate Paths.

**Candidate Path**:
An untrusted path reported by a Change Event or supplied directly for possible quality checking.
_Avoid_: File, target

**Gate Target**:
An existing Python file within the Workspace that is eligible for quality gates.
_Avoid_: Candidate Path, input file

**Target Selection**:
The conversion of Candidate Paths into Gate Targets under Workspace containment.
_Avoid_: File discovery, path parsing

**Excluded Folder**:
A project-configured, Workspace-relative folder whose descendants are intentionally ineligible
as Gate Targets. Exclusions defer quality-gate enforcement in that tree without weakening it
elsewhere.
