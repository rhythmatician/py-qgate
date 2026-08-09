"""Tests for qgate.engine helpers."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from qgate.engine import (
    _custom_guard_errors,
    _discover_python_files,
    _enrich_type_diagnostics,
    _TYPE_CONTEXT_LIMIT,
    _json_value,
    _load_codex_payload,
    _payload_paths,
    _resolve_python_files,
    _working_directory,
)


def test_json_value_primitives() -> None:
    assert _json_value(None) is None
    assert _json_value(True) is True
    assert _json_value(42) == 42
    assert _json_value(3.14) == 3.14
    assert _json_value("hello") == "hello"


def test_json_value_dict() -> None:
    result = _json_value({"a": 1, "b": [2, 3]})
    assert result == {"a": 1, "b": [2, 3]}


def test_json_value_bad_key_raises() -> None:
    with pytest.raises(TypeError):
        _json_value({1: "bad"})  # type: ignore[arg-type]


def test_json_value_unsupported_type_raises() -> None:
    with pytest.raises(TypeError):
        _json_value(object())


def test_load_codex_payload_valid() -> None:
    stream = io.StringIO(json.dumps({"tool_name": "write_file", "tool_input": {"path": "a.py"}}))
    payload = _load_codex_payload(stream)
    assert payload is not None
    assert payload["tool_name"] == "write_file"


def test_load_codex_payload_invalid_json() -> None:
    stream = io.StringIO("not json")
    assert _load_codex_payload(stream) is None


def test_load_codex_payload_non_dict() -> None:
    stream = io.StringIO("[1, 2, 3]")
    assert _load_codex_payload(stream) is None


def test_payload_paths_simple(tmp_path: Path) -> None:
    payload = {"tool_input": {"path": "foo.py"}}
    assert _payload_paths(payload) == ["foo.py"]


def test_payload_paths_apply_patch() -> None:
    command = "*** Add File: src/new.py\nsome content"
    payload = {"tool_name": "apply_patch", "tool_input": {"command": command}}
    paths = _payload_paths(payload)
    assert "src/new.py" in paths


def test_payload_paths_no_tool_input() -> None:
    assert _payload_paths({}) == []


def test_working_directory_default(tmp_path: Path) -> None:
    result = _working_directory({}, tmp_path)
    assert result == tmp_path


def test_working_directory_valid(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    result = _working_directory({"cwd": str(sub)}, tmp_path)
    assert result == sub.resolve()


def test_working_directory_escapes_root(tmp_path: Path) -> None:
    result = _working_directory({"cwd": "/etc"}, tmp_path)
    assert result == tmp_path


def test_resolve_python_files(tmp_path: Path) -> None:
    f = tmp_path / "hello.py"
    f.write_text("x = 1\n")
    result = _resolve_python_files([str(f)], tmp_path, tmp_path)
    assert f.resolve() in result


def test_resolve_python_files_skips_non_py(tmp_path: Path) -> None:
    f = tmp_path / "data.txt"
    f.write_text("hello")
    result = _resolve_python_files([str(f)], tmp_path, tmp_path)
    assert result == []


def test_discover_python_files(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n")
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "hidden.py").write_text("# excluded\n")
    found = _discover_python_files(tmp_path)
    names = [f.name for f in found]
    assert "main.py" in names
    assert "hidden.py" not in names


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


def test_enrich_type_diagnostic_includes_local_member_contract(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("class User:\n    name: str\n\nuser.name\n")
    diagnostic = (
        f"{source}:4:6 - error: Cannot access attribute \"name\" for class \"User\" "
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
    diagnostic = (
        f"{source}:2:1 - error: Cannot access attribute \"{'x' * 400}\" "
        'for class "User"'
    )

    enriched = _enrich_type_diagnostics(diagnostic, root=tmp_path)

    assert len(enriched) <= len(diagnostic) + _TYPE_CONTEXT_LIMIT + 1


def test_enrich_type_diagnostic_fails_open(tmp_path: Path) -> None:
    diagnostic = f"{tmp_path / 'missing.py'}:1:1 - error: Cannot access attribute \"x\" for class \"User\""

    assert _enrich_type_diagnostics(diagnostic, root=tmp_path) == diagnostic
