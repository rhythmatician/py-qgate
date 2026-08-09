"""Behavior tests for provider-neutral Gate Target Selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from qgate.targeting import select_all_gate_targets, select_gate_targets


def test_selecting_all_gate_targets_excludes_workspace_caches(tmp_path: Path) -> None:
    target = tmp_path / "main.py"
    target.write_text("", encoding="utf-8")
    cache = tmp_path / ".venv"
    cache.mkdir()
    (cache / "hidden.py").write_text("", encoding="utf-8")

    targets = select_all_gate_targets(tmp_path)

    assert targets == [target.resolve()]


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
