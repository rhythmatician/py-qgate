"""Tests for qgate.engine helpers."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from qgate.engine import (
    _TYPE_CONTEXT_LIMIT,
    _captured_text,
    _custom_guard_errors,
    _enrich_type_diagnostics,
    _run_command,
    run_gates,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("already decoded", "already decoded"), ("caf\xe9".encode("cp1252"), "caf\xe9"), (None, "")],
)
def test_timeout_output_keeps_text_and_decodes_bytes(
    value: bytes | str | None, expected: str
) -> None:
    assert _captured_text(value, "cp1252") == expected


def test_custom_guard_errors_clean(tmp_path: Path) -> None:
    f = tmp_path / "clean.py"
    f.write_text("x = obj.attr\n")
    assert _custom_guard_errors([f], tmp_path) == []


def test_custom_guard_errors_detects_getattr(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    # Build the fixture at runtime so qgate's own source scan can check this test.
    f.write_text("y = get" + 'attr(obj, "name", None)\n')
    errors = _custom_guard_errors([f], tmp_path)
    assert len(errors) == 1
    assert "ban-getattr-literals" in errors[0]


def test_run_command_reports_checker_timeout(
    tmp_path: Path,
) -> None:
    command = [sys.executable, "-c", "import time; time.sleep(2)"]
    result = _run_command(command, tmp_path, timeout_seconds=0.1)

    assert result.returncode == 124
    assert result.stdout == ""
    assert "timed out after 0.1 seconds" in result.stderr


def test_run_command_preserves_normal_status_and_output(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; print('stdout text'); print('stderr text', file=sys.stderr); sys.exit(7)",
    ]
    result = _run_command(command, tmp_path, timeout_seconds=2)

    assert result.returncode == 7
    assert result.stdout.strip() == "stdout text"
    assert result.stderr.strip() == "stderr text"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object only")
def test_failed_job_assignment_cannot_launch_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "checker-started"

    def fail_assignment(_self: object, _pid: int) -> None:
        raise OSError("job assignment failed")

    monkeypatch.setattr("qgate.windows_job.WindowsJob.assign", fail_assignment)
    command = [
        sys.executable,
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()",
        str(marker),
    ]
    result = _run_command(command, tmp_path, timeout_seconds=2)

    assert result.returncode == 127
    assert "job assignment failed" in result.stderr
    assert not marker.exists()


def test_job_creation_error_becomes_gate_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_job_creation() -> None:
        raise OSError("job creation failed")

    def fail_process_launch(*args: object, **kwargs: object) -> None:
        pytest.fail("checker must not launch after job creation fails")

    monkeypatch.setattr("qgate.engine._new_job", fail_job_creation)
    monkeypatch.setattr(subprocess, "Popen", fail_process_launch)
    result = _run_command(["checker"], tmp_path)

    assert result.returncode == 127
    assert "job creation failed" in result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object only")
def test_job_termination_error_still_kills_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "orphan-marker"
    command = [
        sys.executable,
        "-c",
        "import pathlib,sys,time; print('started',flush=True); "
        "time.sleep(1); pathlib.Path(sys.argv[1]).touch()",
        str(marker),
    ]

    def fail_termination(_self: object) -> None:
        raise OSError("job termination failed")

    monkeypatch.setattr("qgate.windows_job.WindowsJob.terminate", fail_termination)
    result = _run_command(command, tmp_path, timeout_seconds=0.2)

    assert result.returncode == 124
    assert "could not terminate checker job" in result.stderr
    time.sleep(1.1)
    assert not marker.exists()


def test_timeout_stops_child_after_launcher_exits_and_keeps_output(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "orphan-marker"
    ready = tmp_path / "ready"
    child_script = (
        "import pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text('ready'); "
        "print('child started', flush=True); "
        "time.sleep(1.5); pathlib.Path(sys.argv[2]).write_text('orphan')"
    )
    launcher_script = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}, sys.argv[1], sys.argv[2]]); "
        "ready = pathlib.Path(sys.argv[1]); "
        "\nwhile not ready.exists(): time.sleep(0.01)"
    )

    started = time.monotonic()
    result = _run_command(
        [sys.executable, "-c", launcher_script, str(ready), str(marker)],
        tmp_path,
        timeout_seconds=0.5,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 124
    assert "child started" in result.stdout
    assert "timed out after 0.5 seconds" in result.stderr
    assert elapsed < 1.3
    time.sleep(1.2)
    assert not marker.exists(), "the checker child survived after its launcher exited"


def test_run_gates_rejects_invalid_direct_timeout_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "example.py"
    source.write_text("x = 1\n")

    def run_command(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        pytest.fail("invalid timeout must not launch any checker")

    monkeypatch.setattr("qgate.engine._run_command", run_command)
    with pytest.raises(ValueError, match="finite positive"):
        run_gates(files=[source], root=tmp_path, command_timeout_seconds=0)


def test_run_gates_success_is_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "clean.py"
    source.write_text("x = 1\n")
    commands: list[list[str]] = []

    def run_command(
        command: list[str], root: Path, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
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


def test_fix_batches_command_targets_below_windows_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = [
        tmp_path / f"package_{index:04d}" / ("long_module_name_" + "x" * 48 + ".py")
        for index in range(700)
    ]
    commands: list[list[str]] = []

    def run_command(
        command: list[str], root: Path, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    def no_guard_errors(_files: Sequence[Path], _root: Path) -> list[str]:
        return []

    monkeypatch.setattr("qgate.engine._run_command", run_command)
    monkeypatch.setattr("qgate.engine._custom_guard_errors", no_guard_errors)

    assert run_gates(files=sources, root=tmp_path, fix=True, type_checker="dmypy") == 0
    assert commands
    assert all(len(subprocess.list2cmdline(command)) <= 16_000 for command in commands)
    assert {
        Path(argument) for command in commands for argument in command if argument.endswith(".py")
    } == set(sources)


def test_explicit_fix_keeps_selected_gate_targets_in_each_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = [tmp_path / "first.py", tmp_path / "second.py"]
    commands: list[list[str]] = []

    def run_command(
        command: list[str], root: Path, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    def no_guard_errors(_files: Sequence[Path], _root: Path) -> list[str]:
        return []

    monkeypatch.setattr("qgate.engine._run_command", run_command)
    monkeypatch.setattr("qgate.engine._custom_guard_errors", no_guard_errors)

    assert run_gates(files=sources, root=tmp_path, fix=True) == 0
    assert commands
    assert all(command[-2:] == [str(path) for path in sources] for command in commands)


def test_ci_ruff_keeps_exact_gate_targets_while_pyright_uses_compact_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "example.py"
    source.write_text("x = 1\n")
    (docs / "example.md").write_text("```python\ninvalid python\n```\n")
    commands: list[list[str]] = []

    def run_command(
        command: list[str], root: Path, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("qgate.engine._run_command", run_command)

    assert run_gates(files=[source], root=tmp_path, ci=True) == 0

    ruff_commands = [command for command in commands if "ruff" in command[0]]
    pyright_commands = [command for command in commands if "pyright" in command[0]]
    assert ruff_commands
    assert all(str(source) in command for command in ruff_commands)
    assert all(str(Path("docs")) not in command for command in ruff_commands)
    assert pyright_commands == [[pyright_commands[0][0], str(Path("docs"))]]


def test_ci_batches_long_exact_ruff_target_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = [
        tmp_path / "docs" / f"package_{index:04d}" / ("long_module_name_" + "x" * 48 + ".py")
        for index in range(700)
    ]
    commands: list[list[str]] = []

    def run_command(
        command: list[str], root: Path, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    def no_guard_errors(_files: Sequence[Path], _root: Path) -> list[str]:
        return []

    monkeypatch.setattr("qgate.engine._run_command", run_command)
    monkeypatch.setattr("qgate.engine._custom_guard_errors", no_guard_errors)

    assert run_gates(files=sources, root=tmp_path, ci=True) == 0

    ruff_commands = [command for command in commands if "ruff" in command[0]]
    assert len(ruff_commands) > 1
    assert all(len(subprocess.list2cmdline(command)) <= 16_000 for command in ruff_commands)
    assert {
        Path(argument)
        for command in ruff_commands
        for argument in command
        if argument.endswith(".py")
    } == set(sources)


def test_large_explicit_target_set_batches_ruff_but_keeps_one_coherent_pyright_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    sources = [package / (f"long_module_{index:04d}_" + "x" * 48 + ".py") for index in range(700)]
    for source in sources:
        source.touch()
    commands: list[list[str]] = []

    def run_command(
        command: list[str], root: Path, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("qgate.engine._run_command", run_command)

    assert run_gates(files=sources, root=tmp_path, fix=True) == 0

    ruff_commands = [command for command in commands if "ruff" in command[0]]
    pyright_commands = [command for command in commands if "pyright" in command[0]]
    assert len(ruff_commands) > 1
    assert all(len(subprocess.list2cmdline(command)) <= 16_000 for command in ruff_commands)
    assert pyright_commands == [[pyright_commands[0][0], str(tmp_path)]]


def test_large_partial_target_set_fails_instead_of_splitting_pyright(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    sources = [package / (f"long_module_{index:04d}_" + "x" * 48 + ".py") for index in range(700)]
    for source in sources:
        source.touch()
    (package / "unselected.py").touch()
    commands: list[list[str]] = []

    def run_command(
        command: list[str], root: Path, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("qgate.engine._run_command", run_command)

    assert run_gates(files=sources, root=tmp_path, fix=True) == 2
    assert not [command for command in commands if "pyright" in command[0]]
    assert "cannot run one coherent Pyright analysis" in capsys.readouterr().err


def test_dmypy_respects_project_owned_mypy_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "clean.py"
    source.write_text("x = 1\n")
    commands: list[list[str]] = []

    def run_command(
        command: list[str], root: Path, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("qgate.engine._run_command", run_command)

    assert run_gates(files=[source], root=tmp_path, type_checker="dmypy") == 0
    dmypy_commands = [command for command in commands if "dmypy" in command[0]]
    assert dmypy_commands == [[dmypy_commands[0][0], "run", "--", str(source)]]
    assert "--strict" not in dmypy_commands[0]


def test_run_gates_failure_prints_concise_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "bad.py"
    source.write_text("x = 1\n")
    commands: list[list[str]] = []

    def run_command(
        command: list[str], root: Path, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
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
