"""Provider-neutral Gate Target Selection."""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

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
    excluded_roots = _configured_excluded_roots(trusted_workspace)
    targets: list[Path] = []
    for candidate in trusted_workspace.rglob("*"):
        if not candidate.is_file():
            continue
        if _is_excluded(candidate, trusted_workspace, excluded_roots):
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
    excluded_roots = _configured_excluded_roots(trusted_workspace)
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
        if _is_excluded(candidate, trusted_workspace, excluded_roots):
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


def _configured_excluded_roots(workspace: Path) -> tuple[Path, ...]:
    pyproject = workspace / "pyproject.toml"
    try:
        configuration = cast(
            dict[str, object],
            tomllib.loads(pyproject.read_text(encoding="utf-8")),
        )
    except (OSError, tomllib.TOMLDecodeError):
        return ()

    tool = configuration.get("tool")
    if not isinstance(tool, dict):
        return ()
    qgate = cast(dict[str, object], tool).get("qgate")
    if not isinstance(qgate, dict):
        return ()
    qgate = cast(dict[str, object], qgate)
    raw_folders = qgate.get("excluded-folders", [])
    if not isinstance(raw_folders, list):
        return ()

    roots: set[Path] = set()
    for raw_folder in cast(list[object], raw_folders):
        if not isinstance(raw_folder, str) or not raw_folder.strip():
            continue
        normalized = raw_folder.strip().replace("\\", "/")
        if PurePosixPath(normalized).is_absolute() or PureWindowsPath(normalized).is_absolute():
            continue
        root = (workspace / Path(*PurePosixPath(normalized).parts)).resolve()
        if root != workspace and _is_within_workspace(root, workspace):
            roots.add(root)
    return tuple(sorted(roots))


def _is_excluded(candidate: Path, workspace: Path, excluded_roots: tuple[Path, ...]) -> bool:
    try:
        relative_parts = candidate.absolute().relative_to(workspace).parts
    except ValueError:
        return False
    if any(part in _EXCLUDED_DIRECTORIES for part in relative_parts):
        return True

    resolved = candidate.resolve()
    return any(resolved == root or _is_within_workspace(resolved, root) for root in excluded_roots)
