"""Tests for qgate.cli entry point and init subcommand."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qgate.cli import _run_init, main


def test_main_no_files_returns_zero(tmp_path: Path) -> None:
    result = main([], workspace_root=tmp_path)
    assert result == 0


def test_main_passes_configured_command_timeout_to_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.qgate]\ncommand-timeout-seconds = 1200\n")
    (tmp_path / "example.py").write_text("x = 1\n")
    received: dict[str, object] = {}

    def run_gates(**kwargs: object) -> int:
        received.update(kwargs)
        return 0

    monkeypatch.setattr("qgate.cli.run_gates", run_gates)
    assert main(["example.py"], workspace_root=tmp_path) == 0
    assert received["command_timeout_seconds"] == 1200


def test_main_uses_default_timeout_without_project_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "example.py").write_text("x = 1\n")
    received: dict[str, object] = {}

    def run_gates(**kwargs: object) -> int:
        received.update(kwargs)
        return 0

    monkeypatch.setattr("qgate.cli.run_gates", run_gates)
    assert main(["example.py"], workspace_root=tmp_path) == 0
    assert received["command_timeout_seconds"] == 300


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", '"300"', "true"])
def test_main_rejects_invalid_command_timeout_before_running_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(f"[tool.qgate]\ncommand-timeout-seconds = {value}\n")
    (tmp_path / "example.py").write_text("x = 1\n")

    def run_gates(**kwargs: object) -> int:
        pytest.fail("invalid timeout must not launch any checker")

    monkeypatch.setattr("qgate.cli.run_gates", run_gates)
    with pytest.raises(SystemExit) as exc:
        main(["example.py"], workspace_root=tmp_path)
    assert exc.value.code == 2
    assert "command-timeout-seconds" in capsys.readouterr().err


def test_main_version_reports_installed_qgate_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(["--version"])

    assert result == 0
    assert capsys.readouterr().out == "qgate 0.1.0\n"


def test_main_ci_and_fix_errors() -> None:
    import io
    import sys

    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        try:
            main(["--ci", "--fix"])
        except SystemExit as exc:
            assert exc.code != 0
    finally:
        sys.stderr = old_stderr


def test_run_init_creates_files(tmp_path: Path) -> None:
    result = _run_init(tmp_path)
    assert result == 0
    assert (tmp_path / ".codex" / "hooks.json").exists()
    pre_commit = (tmp_path / ".pre-commit-config.yaml").read_text()
    assert "entry: uv run qgate --fix --type-checker dmypy" in pre_commit
    assert "require_serial: true" in pre_commit
    assert "uvx" not in pre_commit
    hooks = (tmp_path / ".codex" / "hooks.json").read_text()
    ci = (tmp_path / ".github" / "workflows" / "ci.yml").read_text()
    assert "uv run qgate --codex-stdin --fix" in hooks
    assert "uv run --locked qgate --ci" in ci
    assert "uvx" not in hooks
    assert "uvx" not in ci
    settings = json.loads((tmp_path / ".vscode" / "settings.json").read_text())
    assert settings["editor.formatOnSave"] is True


def test_run_init_skips_existing(tmp_path: Path) -> None:
    hooks = tmp_path / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text("{}")
    _run_init(tmp_path)
    assert hooks.read_text() == "{}"  # unchanged


def test_run_init_appends_pyproject(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'test'\n")
    _run_init(tmp_path)
    content = pyproject.read_text()
    assert "[tool.ruff]" in content
    assert 'select = ["E4", "E7", "E9", "F"]' in content
    assert 'typeCheckingMode = "standard"' in content


def test_run_init_preserves_existing_ruff_and_adds_missing_pyright(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = "[project]\nname = 'test'\n\n[tool.ruff]\nline-length = 88\n"
    pyproject.write_text(original)

    _run_init(tmp_path)

    content = pyproject.read_text()
    assert original in content
    assert content.count("[tool.ruff]") == 1
    assert "[tool.ruff.lint]" not in content
    assert content.count("[tool.pyright]") == 1
    assert 'typeCheckingMode = "standard"' in content


def test_run_init_preserves_existing_pyright_and_adds_missing_ruff(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = (
        "[project]\nname = 'test'\n\n"
        "[tool.pyright]\npythonVersion = '3.11'\ntypeCheckingMode = 'basic'\n"
    )
    pyproject.write_text(original)

    _run_init(tmp_path)

    content = pyproject.read_text()
    assert original in content
    assert content.count("[tool.pyright]") == 1
    assert content.count("[tool.ruff]") == 1
    assert content.count("[tool.ruff.lint]") == 1
    assert 'select = ["E4", "E7", "E9", "F"]' in content


def test_run_init_leaves_existing_ruff_and_pyright_policy_unchanged(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = (
        "[project]\nname = 'test'\n\n"
        "[tool.ruff.lint]\nselect = ['F']\n\n"
        "[tool.pyright]\ntypeCheckingMode = 'basic'\n"
    )
    pyproject.write_text(original)

    _run_init(tmp_path)

    assert pyproject.read_text() == original


def test_run_init_respects_standalone_ruff_policy(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'test'\n")
    (tmp_path / "ruff.toml").write_text("[lint]\nselect = ['F']\n")

    _run_init(tmp_path)

    content = pyproject.read_text()
    assert "[tool.ruff]" not in content
    assert "[tool.pyright]" in content


def test_run_init_respects_dot_ruff_policy(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'test'\n")
    (tmp_path / ".ruff.toml").write_text("[lint]\nselect = ['F']\n")

    _run_init(tmp_path)

    assert "[tool.ruff]" not in pyproject.read_text()


def test_run_init_respects_standalone_pyright_policy(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'test'\n")
    (tmp_path / "pyrightconfig.json").write_text('{"typeCheckingMode": "basic"}\n')

    _run_init(tmp_path)

    content = pyproject.read_text()
    assert "[tool.pyright]" not in content
    assert "[tool.ruff]" in content


def test_run_init_preserves_existing_vscode_settings(tmp_path: Path) -> None:
    settings = tmp_path / ".vscode" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"python.defaultInterpreterPath": ".venv/bin/python"}\n')

    _run_init(tmp_path)

    content = settings.read_text()
    parsed = json.loads(content)
    assert parsed["python.defaultInterpreterPath"] == ".venv/bin/python"
    assert parsed["editor.formatOnSave"] is True


def test_run_init_updates_jsonc_vscode_settings(tmp_path: Path) -> None:
    settings = tmp_path / ".vscode" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        "{\n  // Keep this workspace setting.\n"
        '  "editor.formatOnSave": false,\n'
        '  "files.trimTrailingWhitespace": true,\n'
        "}\n"
    )

    _run_init(tmp_path)

    content = settings.read_text()
    assert "// Keep this workspace setting." in content
    assert '"editor.formatOnSave": true' in content
    assert '"files.trimTrailingWhitespace": true' in content


def test_run_init_is_idempotent_for_vscode_settings(tmp_path: Path) -> None:
    _run_init(tmp_path)
    settings = tmp_path / ".vscode" / "settings.json"
    first = settings.read_text()

    _run_init(tmp_path)

    assert settings.read_text() == first


def test_run_init_skips_invalid_vscode_settings(tmp_path: Path) -> None:
    settings = tmp_path / ".vscode" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{ invalid")

    _run_init(tmp_path)

    assert settings.read_text() == "{ invalid"


def test_main_init_subcommand(tmp_path: Path) -> None:
    result = main(["init"], workspace_root=tmp_path)
    assert result == 0
    assert (tmp_path / ".codex" / "hooks.json").exists()


def test_run_init_does_not_create_agents_file(tmp_path: Path) -> None:
    _run_init(tmp_path)
    assert not (tmp_path / "AGENTS.md").exists()


def test_run_init_does_not_modify_agents_file(tmp_path: Path) -> None:
    agents = tmp_path / "AGENTS.md"
    original = "# Existing guidance\n"
    agents.write_text(original)
    _run_init(tmp_path)
    assert agents.read_text() == original
