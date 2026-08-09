"""Behavior tests for Codex Change Event targeting."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from qgate.codex import select_codex_gate_targets


def test_codex_change_event_selects_safe_gate_targets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    working_directory = workspace / "src"
    working_directory.mkdir(parents=True)
    target = working_directory / "changed.py"
    target.write_text("", encoding="utf-8")
    payload = {
        "cwd": str(working_directory),
        "tool_name": "write_file",
        "tool_input": {"path": "changed.py"},
    }

    targets = select_codex_gate_targets(io.StringIO(json.dumps(payload)), workspace)

    assert targets == [target.resolve()]


def test_codex_apply_patch_event_extracts_each_changed_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = workspace / "first.py"
    second = workspace / "second.py"
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")
    patch = "*** Update File: first.py\n*** Add File: second.py\n"
    payload = {"toolName": "apply_patch", "toolInput": {"command": patch}}

    targets = select_codex_gate_targets(io.StringIO(json.dumps(payload)), workspace)

    assert targets == [first.resolve(), second.resolve()]


@pytest.mark.parametrize("payload", ["not json", "[1, 2, 3]", "{}"])
def test_unusable_codex_change_event_selects_no_targets(
    payload: str,
    tmp_path: Path,
) -> None:
    targets = select_codex_gate_targets(io.StringIO(payload), tmp_path)

    assert targets == []


def test_codex_change_event_accepts_path_aliases(tmp_path: Path) -> None:
    targets_by_name = {
        name: tmp_path / f"{name}.py" for name in ("file_path", "target_file", "paths")
    }
    for target in targets_by_name.values():
        target.write_text("", encoding="utf-8")
    payload = {
        "tool_input": {
            "file_path": "file_path.py",
            "target_file": "target_file.py",
            "paths": ["paths.py"],
        }
    }

    targets = select_codex_gate_targets(io.StringIO(json.dumps(payload)), tmp_path)

    assert targets == sorted(path.resolve() for path in targets_by_name.values())


def test_codex_change_event_does_not_select_target_in_configured_excluded_folder(
    tmp_path: Path,
) -> None:
    excluded = tmp_path / "generated"
    excluded.mkdir()
    target = excluded / "changed.py"
    target.write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.qgate]\nexcluded-folders = ["generated"]\n',
        encoding="utf-8",
    )
    payload = {
        "cwd": str(tmp_path),
        "tool_name": "write_file",
        "tool_input": {"path": str(target)},
    }

    targets = select_codex_gate_targets(io.StringIO(json.dumps(payload)), tmp_path)

    assert targets == []
