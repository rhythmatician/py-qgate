"""Tests for qgate.cli entry point and init subcommand."""

from __future__ import annotations

import json
from pathlib import Path

from qgate.cli import _run_init, main


def test_main_no_files_returns_zero(tmp_path: Path) -> None:
    result = main([], workspace_root=tmp_path)
    assert result == 0


def test_main_version_reports_installed_qgate_version(capsys) -> None:
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
    assert (tmp_path / ".pre-commit-config.yaml").exists()
    assert (tmp_path / ".github" / "workflows" / "ci.yml").exists()
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


def test_run_init_skips_existing_ruff(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    original = "[project]\nname = 'test'\n\n[tool.ruff]\nline-length = 88\n"
    pyproject.write_text(original)
    _run_init(tmp_path)
    assert pyproject.read_text() == original


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
