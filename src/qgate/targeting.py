"""Provider-neutral Gate Target Selection."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

_EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".venv-pip-backup",
    "artifacts",
    "tmp",
}


def select_all_gate_targets(workspace: Path) -> list[Path]:
    """Select every Gate Target in a Workspace."""
    trusted_workspace = workspace.resolve()
    targets: list[Path] = []
    for candidate in trusted_workspace.rglob("*"):
        if not candidate.is_file():
            continue
        relative_parts = candidate.relative_to(trusted_workspace).parts
        if any(part in _EXCLUDED_DIRECTORIES for part in relative_parts):
            continue
        resolved = candidate.resolve()
        if not _is_within_workspace(resolved, trusted_workspace):
            continue
        if resolved.suffix.lower() == ".py":
            targets.append(resolved)
    return sorted(targets)


def select_gate_targets(
    candidate_paths: Sequence[str],
    *,
    workspace: Path,
    reported_working_directory: str | Path | None = None,
) -> list[Path]:
    """Resolve untrusted Candidate Paths into safe Gate Targets."""
    trusted_workspace = workspace.resolve()
    base_directory = _working_directory(
        reported_working_directory,
        workspace=trusted_workspace,
    )

    targets: set[Path] = set()
    for raw_candidate in candidate_paths:
        candidate_text = raw_candidate.strip().strip("\"'")
        if not candidate_text:
            continue

        candidate = Path(candidate_text)
        if not candidate.is_absolute():
            candidate = base_directory / candidate
        resolved = candidate.resolve()
        if not _is_within_workspace(resolved, trusted_workspace):
            continue

        if resolved.suffix.lower() == ".py" and resolved.is_file():
            targets.add(resolved)
    return sorted(targets)


def _working_directory(
    reported_working_directory: str | Path | None,
    *,
    workspace: Path,
) -> Path:
    if not reported_working_directory:
        return workspace

    candidate = Path(reported_working_directory)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if not _is_within_workspace(resolved, workspace):
        return workspace
    return resolved if resolved.is_dir() else workspace


def _is_within_workspace(candidate: Path, workspace: Path) -> bool:
    try:
        candidate.relative_to(workspace)
    except ValueError:
        return False
    return True
