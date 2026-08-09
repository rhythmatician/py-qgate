"""Tests for qgate.engine helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from qgate.engine import (
    _TYPE_CONTEXT_LIMIT,
    _custom_guard_errors,
    _enrich_type_diagnostics,
    run_gates,
)


def test_custom_guard_errors_clean(tmp_path: Path) -> None:
    f = tmp_path / "clean.py"
    f.write_text("x = obj.attr\n")
    assert _custom_guard_errors([f], tmp_path) == []


def test_custom_guard_errors_detects_getattr(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    f.write_text('y = getattr(obj, "name", None)\n')
    errors = _custom_guard_errors([f], tmp_path)
    assert len(errors) == 1
    assert "ban-getattr-literals" in errors[0]


def test_run_gates_success_is_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "clean.py"
    source.write_text("x = 1\n")
    commands: list[list[str]] = []

    def run_command(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "success output", "")

    monkeypatch.setattr("qgate.engine._run_command", run_command)
    assert run_gates(files=[source], root=tmp_path) == 0
    ruff_commands = [command for command in commands if "ruff" in command[0]]
    assert ruff_commands
    assert all("--quiet" in command for command in ruff_commands)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_run_gates_failure_prints_concise_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "bad.py"
    source.write_text("x = 1\n")
    commands: list[list[str]] = []

    def run_command(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "--output-format=concise" in command:
            return subprocess.CompletedProcess(command, 1, "", "bad.py:1:1: F401 unused import")
        return subprocess.CompletedProcess(command, 0, "success output", "")

    monkeypatch.setattr("qgate.engine._run_command", run_command)
    assert run_gates(files=[source], root=tmp_path) == 2
    lint_commands = [command for command in commands if "--output-format=concise" in command]
    assert lint_commands and "--quiet" in lint_commands[0]
    captured = capsys.readouterr()
    assert "RUFF LINT" in captured.err
    assert "bad.py:1:1: F401 unused import" in captured.err
    assert "success output" not in captured.err


def test_enrich_type_diagnostic_includes_local_member_contract(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("class User:\n    name: str\n\nuser.name\n")
    diagnostic = (
        f'{source}:4:6 - error: Cannot access attribute "name" for class "User" '
        "(reportAttributeAccessIssue)"
    )

    enriched = _enrich_type_diagnostics(diagnostic, root=tmp_path)

    assert enriched.startswith(diagnostic)
    assert "[TYPE CONTEXT] receiver=User; member=name: str" in enriched


def test_enrich_type_diagnostic_skips_broad_errors(tmp_path: Path) -> None:
    diagnostic = f"{tmp_path / 'example.py'}:1:1 - error: reportGeneralTypeIssues"

    assert _enrich_type_diagnostics(diagnostic, root=tmp_path) == diagnostic


def test_enrich_type_diagnostic_is_bounded(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("class User:\n    " + "x" * 400 + ": str\n")
    diagnostic = f'{source}:2:1 - error: Cannot access attribute "{"x" * 400}" for class "User"'

    enriched = _enrich_type_diagnostics(diagnostic, root=tmp_path)

    assert len(enriched) <= len(diagnostic) + _TYPE_CONTEXT_LIMIT + 1


def test_enrich_type_diagnostic_fails_open(tmp_path: Path) -> None:
    diagnostic = (
        f'{tmp_path / "missing.py"}:1:1 - error: Cannot access attribute "x" for class "User"'
    )

    assert _enrich_type_diagnostics(diagnostic, root=tmp_path) == diagnostic
