"""Tests for qgate.engine helpers."""

from __future__ import annotations

import shutil
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


def test_large_partial_target_set_runs_one_pyright_with_exact_selection(
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
        if "pyright" in command[0]:
            import json

            config = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
            assert set(config["include"]) == {p.relative_to(tmp_path).as_posix() for p in sources}
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("qgate.engine._run_command", run_command)

    assert run_gates(files=sources, root=tmp_path, fix=True) == 0
    pyright_commands = [command for command in commands if "pyright" in command[0]]
    assert len(pyright_commands) == 1
    assert pyright_commands[0][-2] == "--project"
    assert not Path(pyright_commands[0][-1]).exists()


def test_long_partial_selection_real_pyright_preserves_config_and_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("qgate.engine._WINDOWS_SAFE_COMMAND_LENGTH", 100)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pyright]\nreportAssignmentType = false\n", encoding="utf-8"
    )
    package = tmp_path / "space é"
    package.mkdir()
    selected = package / "selected.py"
    selected.write_text("value: int = None\n", encoding="utf-8")
    (package / "unselected.py").write_text("value: int = None\n", encoding="utf-8")

    assert run_gates(files=[selected], root=tmp_path) == 0
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pyright]\nreportAssignmentType = true\n", encoding="utf-8"
    )
    assert run_gates(files=[selected], root=tmp_path) == 2
    errors = capsys.readouterr().err
    assert "reportAssignmentType" in errors
    assert "unselected.py" not in errors
    assert not list(tmp_path.glob(".qgate-pyright-*.json"))


def test_long_selection_honors_json_precedence_and_execution_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("qgate.engine._WINDOWS_SAFE_COMMAND_LENGTH", 100)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pyright]\nreportAssignmentType = false\n", encoding="utf-8"
    )
    (tmp_path / "pyrightconfig.json").write_text(
        '{"reportAssignmentType": true, '
        '"executionEnvironments": [{"root": "app", "extraPaths": ["lib"]}]}',
        encoding="utf-8",
    )
    app = tmp_path / "app"
    app.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "helper.py").write_text("def number() -> int:\n    return 1\n", encoding="utf-8")
    source = app / "selected.py"
    source.write_text("from helper import number\n\nvalue: str = number()\n", encoding="utf-8")
    (app / "unselected.py").write_text("value: int = None\n", encoding="utf-8")

    assert run_gates(files=[source], root=tmp_path) == 2
    errors = capsys.readouterr().err
    assert "reportAssignmentType" in errors
    assert "reportMissingImports" not in errors
    assert "unselected.py" not in errors
    assert not list(tmp_path.glob(".qgate-pyright-*.json"))


def test_long_selection_nested_config_matches_explicit_pyright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from qgate.engine import _run_command

    monkeypatch.setattr("qgate.engine._WINDOWS_SAFE_COMMAND_LENGTH", 100)
    settings = tmp_path / "settings"
    settings.mkdir()
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "helper.py").write_text("def number() -> int:\n    return 1\n", encoding="utf-8")
    (settings / "base.json").write_text(
        '{"extraPaths": ["../lib"], "reportAssignmentType": true}', encoding="utf-8"
    )
    (tmp_path / "pyrightconfig.json").write_text(
        '{"extends": "settings/base.json"}', encoding="utf-8"
    )
    source = tmp_path / "selected.py"
    source.write_text("from helper import number\nvalue: str = number()\n", encoding="utf-8")
    (tmp_path / "unselected.py").write_text("value: int = None\n", encoding="utf-8")
    pyright = shutil.which("pyright")
    assert pyright is not None
    baseline = _run_command([pyright, str(source)], tmp_path)
    calls: list[list[str]] = []

    def record_command(
        command: list[str], root: Path, *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return _run_command(command, root, timeout_seconds=timeout_seconds)

    monkeypatch.setattr("qgate.engine._run_command", record_command)
    assert baseline.returncode == 1
    assert "reportAssignmentType" in baseline.stdout
    assert "reportMissingImports" not in baseline.stdout
    assert run_gates(files=[source], root=tmp_path) == 2
    errors = capsys.readouterr().err
    assert "reportAssignmentType" in errors
    assert "reportMissingImports" not in errors
    assert "unselected.py" not in errors
    assert len([command for command in calls if "pyright" in command[0]]) == 1
    assert not list(tmp_path.glob(".qgate-pyright-*.json"))


def test_long_selection_without_project_config_handles_literal_brackets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("qgate.engine._WINDOWS_SAFE_COMMAND_LENGTH", 100)
    source = tmp_path / "selected[1].py"
    source.write_text("value: int = None\n", encoding="utf-8")
    (tmp_path / "selected1.py").write_text("value: int = None\n", encoding="utf-8")
    assert run_gates(files=[source], root=tmp_path) == 2
    errors = capsys.readouterr().err
    assert "reportAssignmentType" in errors
    assert "selected1.py" not in errors
    assert not list(tmp_path.glob(".qgate-pyright-*.json"))


def test_long_selection_preserves_project_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from qgate.engine import _run_command

    monkeypatch.setattr("qgate.engine._WINDOWS_SAFE_COMMAND_LENGTH", 100)
    (tmp_path / "pyrightconfig.json").write_text('{"exclude": ["selected.py"]}', encoding="utf-8")
    source = tmp_path / "selected.py"
    source.write_text("value: int = None\n", encoding="utf-8")
    (tmp_path / "unselected.py").write_text("value: int = None\n", encoding="utf-8")
    pyright = shutil.which("pyright")
    assert pyright is not None
    baseline = _run_command([pyright, str(source)], tmp_path)
    assert baseline.returncode == 0
    assert run_gates(files=[source], root=tmp_path) == 0
    assert not capsys.readouterr().err
    assert not list(tmp_path.glob(".qgate-pyright-*.json"))


def test_long_selection_without_project_config_runs_pyright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("qgate.engine._WINDOWS_SAFE_COMMAND_LENGTH", 100)
    source = tmp_path / "selected.py"
    source.write_text("value: int = None\n", encoding="utf-8")
    (tmp_path / "unselected.py").write_text("value: int = None\n", encoding="utf-8")
    assert run_gates(files=[source], root=tmp_path) == 2
    errors = capsys.readouterr().err
    assert "reportAssignmentType" in errors
    assert "unselected.py" not in errors


def test_selection_configs_are_unique_and_clean_up_after_exception(tmp_path: Path) -> None:
    from qgate.engine import _pyright_selection_config

    source = tmp_path / "selected.py"
    source.touch()
    with (
        pytest.raises(RuntimeError, match="checker failure"),
        _pyright_selection_config([source], tmp_path) as first,
        _pyright_selection_config([source], tmp_path) as second,
    ):
        assert first != second
        assert first.exists() and second.exists()
        raise RuntimeError("checker failure")
    assert not list(tmp_path.glob(".qgate-pyright-*.json"))


def test_ancestor_project_discovery_matches_explicit_pyright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from qgate.engine import _run_command

    monkeypatch.setattr("qgate.engine._WINDOWS_SAFE_COMMAND_LENGTH", 100)
    (tmp_path / "pyrightconfig.json").write_text(
        '{"reportAssignmentType": false}', encoding="utf-8"
    )
    (tmp_path / "helper.py").write_text("def number() -> int:\n    return 1\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    (child / "pyproject.toml").write_text(
        "[tool.pyright]\nreportAssignmentType = true\n", encoding="utf-8"
    )
    source = child / "selected.py"
    source.write_text("from helper import number\n\nvalue: str = number()\n", encoding="utf-8")
    (child / "unselected.py").write_text("value: int = None\n", encoding="utf-8")
    pyright = shutil.which("pyright")
    assert pyright is not None
    baseline = _run_command([pyright, str(source)], child)
    assert baseline.returncode == 0
    assert run_gates(files=[source], root=child) == 0
    assert not capsys.readouterr().err
    assert not list(tmp_path.glob(".qgate-pyright-*.json"))


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
