"""Behavior tests for provider-neutral Gate Target Selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from qgate.targeting import select_all_gate_targets, select_gate_targets


def _configure_excluded_folders(workspace: Path, *folders: str) -> None:
    values = ", ".join(repr(folder) for folder in folders)
    (workspace / "pyproject.toml").write_text(
        f"[tool.qgate]\nexcluded-folders = [{values}]\n",
        encoding="utf-8",
    )


def test_selecting_all_gate_targets_excludes_workspace_caches(tmp_path: Path) -> None:
    target = tmp_path / "main.py"
    target.write_text("", encoding="utf-8")
    cache = tmp_path / ".venv"
    cache.mkdir()
    (cache / "hidden.py").write_text("", encoding="utf-8")

    targets = select_all_gate_targets(tmp_path)

    assert targets == [target.resolve()]


def test_selecting_all_gate_targets_excludes_configured_folder_descendants(
    tmp_path: Path,
) -> None:
    excluded = tmp_path / "scaffolding" / "nested"
    sibling = tmp_path / "scaffolding-next"
    excluded.mkdir(parents=True)
    sibling.mkdir()
    (excluded / "deferred.py").write_text("", encoding="utf-8")
    included = sibling / "active.py"
    included.write_text("", encoding="utf-8")
    _configure_excluded_folders(tmp_path, "scaffolding")

    targets = select_all_gate_targets(tmp_path)

    assert targets == [included.resolve()]


def test_configured_excluded_folders_normalize_windows_separators(tmp_path: Path) -> None:
    excluded = tmp_path / "generated" / "python"
    excluded.mkdir(parents=True)
    (excluded / "client.py").write_text("", encoding="utf-8")
    _configure_excluded_folders(tmp_path, "generated\\python")

    assert select_all_gate_targets(tmp_path) == []


def test_selecting_all_gate_targets_rejects_symlinks_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")
    linked = workspace / "linked.py"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable on this platform")

    targets = select_all_gate_targets(workspace)

    assert targets == []


def test_target_selection_returns_only_python_files_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    working_directory = workspace / "package"
    working_directory.mkdir(parents=True)
    first = working_directory / "a.py"
    second = workspace / "z.py"
    non_python = working_directory / "notes.md"
    for path in (first, second, non_python):
        path.write_text("", encoding="utf-8")

    targets = select_gate_targets(
        ["a.py", "../z.py", "notes.md", "missing.py", "a.py"],
        workspace=workspace,
        reported_working_directory=working_directory,
    )

    assert targets == [first.resolve(), second.resolve()]


def test_target_selection_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")

    targets = select_gate_targets(
        [str(outside)],
        workspace=workspace,
        reported_working_directory=workspace,
    )

    assert targets == []


def test_explicit_target_inside_configured_exclusion_is_not_selected(tmp_path: Path) -> None:
    excluded = tmp_path / "migrations"
    excluded.mkdir()
    candidate = excluded / "legacy.py"
    candidate.write_text("", encoding="utf-8")
    _configure_excluded_folders(tmp_path, "migrations")

    targets = select_gate_targets([str(candidate)], workspace=tmp_path)

    assert targets == []


def test_excluded_folder_outside_workspace_does_not_affect_targets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ignored.py").write_text("", encoding="utf-8")
    target = workspace / "active.py"
    target.write_text("", encoding="utf-8")
    _configure_excluded_folders(workspace, "../outside")

    targets = select_all_gate_targets(workspace)

    assert targets == [target.resolve()]


def test_target_selection_falls_back_to_workspace_for_invalid_reported_directory(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.py"
    target.write_text("", encoding="utf-8")

    targets = select_gate_targets(
        ['"target.py"'],
        workspace=workspace,
        reported_working_directory=tmp_path,
    )

    assert targets == [target.resolve()]
