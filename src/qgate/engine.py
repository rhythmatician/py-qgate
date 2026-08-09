"""Ruff, Pyright/dmypy execution and output formatting."""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TextIO

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


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
_PATCH_FILE_PATTERN = re.compile(
    r"^\*\*\*\s+(?:(?:Add|Delete|Update)\s+File|Move\s+to):\s*(.+?)\s*$",
    re.MULTILINE,
)
_GETATTR_LITERAL_PATTERN = re.compile(
    r"""getattr\(\s*[a-zA-Z_]\w*\s*,\s*(['"])(.*?)\1\s*,\s*None\s*\)""",
    re.DOTALL,
)
_TYPE_DIAGNOSTIC_PATTERN = re.compile(
    r'^(?P<path>.+?):(?P<line>\d+):(?P<column>\d+) - error: '
    r'Cannot access attribute "(?P<member>[^"]+)" for class "(?P<receiver>[^"]+)"',
    re.MULTILINE,
)
_TYPE_CONTEXT_LIMIT = 1200
_TYPE_CONTEXT_ITEM_LIMIT = 300


def _json_value(value: object) -> JsonValue:
    """Convert an external JSON value into the runner's typed boundary value."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        converted: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            converted[key] = _json_value(child)
        return converted
    if isinstance(value, list):
        return [_json_value(child) for child in value]
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _load_codex_payload(stream: TextIO) -> dict[str, JsonValue] | None:
    try:
        raw_payload: object = json.load(stream)
        payload = _json_value(raw_payload)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _string_values(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _string_values(child)


def _payload_paths(payload: dict[str, JsonValue]) -> list[str]:
    tool_input = payload.get("tool_input") or payload.get("toolInput")
    if not isinstance(tool_input, dict):
        return []

    paths: list[str] = []
    for key in ("path", "file_path", "target_file", "paths"):
        value = tool_input.get(key)
        if value is not None:
            paths.extend(_string_values(value))

    tool_name = payload.get("tool_name") or payload.get("toolName")
    command = tool_input.get("command")
    if tool_name == "apply_patch" and isinstance(command, str):
        paths.extend(_PATCH_FILE_PATTERN.findall(command))
    return paths


def _working_directory(payload: dict[str, JsonValue], root: Path) -> Path:
    raw_cwd = payload.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd:
        return root

    candidate = Path(raw_cwd)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except ValueError:
        return root
    return resolved if resolved.is_dir() else root


def _resolve_python_files(candidates: Sequence[str], root: Path, base_dir: Path) -> list[Path]:
    files: set[Path] = set()
    for raw_candidate in candidates:
        candidate_text = raw_candidate.strip().strip("\"'")
        if not candidate_text:
            continue

        candidate = Path(candidate_text)
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except ValueError:
            continue

        if resolved.suffix.lower() == ".py" and resolved.is_file():
            files.add(resolved)
    return sorted(files)


def _discover_python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        relative_parts = candidate.relative_to(root).parts
        if any(part in _EXCLUDED_DIRECTORIES for part in relative_parts):
            continue
        if candidate.suffix.lower() == ".py":
            files.append(candidate.resolve())
    return sorted(files)


def _run_command(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _tool_path(name: str, root: Path) -> str:
    """Resolve a tool from the active environment without starting uv again."""
    executable = f"{name}.exe" if sys.platform == "win32" else name
    for candidate in (
        root / ".venv" / "Scripts" / executable,
        root / ".venv" / "bin" / executable,
    ):
        if candidate.is_file():
            return str(candidate)

    resolved = shutil.which(name)
    if resolved:
        return resolved
    return name


def _tool_command(tool: str, arguments: Sequence[str], files: Sequence[Path]) -> list[str]:
    return [tool, *arguments, *(str(path) for path in files)]


def _ci_command_targets(files: Sequence[Path], root: Path) -> list[Path]:
    """Use compact directory targets in CI to avoid platform command limits."""
    targets: set[Path] = set()
    for path in files:
        relative_parts = path.relative_to(root).parts
        targets.add(Path(relative_parts[0]))
    return sorted(targets)


def _custom_guard_errors(files: Sequence[Path], root: Path) -> list[str]:
    errors: list[str] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{path}: unable to read file: {exc}")
            continue

        for match in _GETATTR_LITERAL_PATTERN.finditer(source):
            line_number = source.count("\n", 0, match.start()) + 1
            relative_path = path.relative_to(root)
            errors.append(
                f"{relative_path}:{line_number}: ban-getattr-literals: "
                "use direct attribute access or explicit type narrowing"
            )
    return errors


def _short_type_context(
    source_path: Path,
    receiver: str,
    member: str,
) -> str | None:
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None

    receiver_name = receiver.split("|", 1)[0].strip()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != receiver_name:
            continue
        for child in node.body:
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                if child.target.id == member:
                    contract = f"{member}: {ast.unparse(child.annotation)}"
                    return _format_type_context(receiver, contract)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == member:
                returns = ast.unparse(child.returns) if child.returns else "Unknown"
                contract = f"{member}(...) -> {returns}"
                return _format_type_context(receiver, contract)
    return None


def _format_type_context(receiver: str, contract: str) -> str:
    hint = ""
    if "None" in receiver:
        hint = "; hint: narrow None before access"
    context = f"[TYPE CONTEXT] receiver={receiver}; member={contract}{hint}"
    return context[:_TYPE_CONTEXT_ITEM_LIMIT]


def _enrich_type_diagnostics(
    output: str,
    *,
    root: Path,
    max_chars: int = _TYPE_CONTEXT_LIMIT,
) -> str:
    """Add bounded local Python context to narrowly supported Pyright errors."""
    additions: list[str] = []
    used = 0
    for match in _TYPE_DIAGNOSTIC_PATTERN.finditer(output):
        raw_path = Path(match.group("path"))
        source_path = raw_path if raw_path.is_absolute() else root / raw_path
        try:
            source_path = source_path.resolve()
            source_path.relative_to(root)
        except ValueError:
            continue
        context = _short_type_context(
            source_path,
            match.group("receiver"),
            match.group("member"),
        )
        if not context or used + len(context) + 1 > max_chars:
            continue
        additions.append(context)
        used += len(context) + 1
    if not additions:
        return output
    return f"{output.rstrip()}\n" + "\n".join(additions)


def run_gates(
    *,
    files: list[Path],
    root: Path,
    ci: bool = False,
    fix: bool = False,
    type_checker: str = "pyright",
) -> int:
    """Run quality gates on the given files and return an exit code."""
    if not files:
        return 0

    command_targets = _ci_command_targets(files, root) if ci else files
    ruff = _tool_path("ruff", root)
    tc = _tool_path(type_checker, root)
    commands: list[tuple[str, list[str]]] = []
    if fix:
        commands.extend(
            (
                (
                    "RUFF SAFE FIXES",
                    _tool_command(ruff, ["check", "--fix", "--quiet"], command_targets),
                ),
                (
                    "RUFF FORMAT",
                    _tool_command(ruff, ["format", "--quiet"], command_targets),
                ),
            )
        )
    format_arguments = ["format"]
    if ci:
        format_arguments.extend(("--exclude", "*.md", "--exclude", "*.ipynb", "--exclude", "*.pyi"))
    if not fix:
        commands.append(
            (
                "RUFF FORMAT CHECK",
                _tool_command(ruff, [*format_arguments, "--check", "--quiet"], command_targets),
            )
        )
    commands.extend(
        (
            (
                "RUFF LINT",
                _tool_command(ruff, ["check", "--output-format=concise"], command_targets),
            ),
            (
                type_checker.upper(),
                _tool_command(
                    tc,
                    ["run", "--", "--strict"] if type_checker == "dmypy" else [],
                    command_targets,
                ),
            ),
        )
    )

    command_results = [(label, _run_command(command, root)) for label, command in commands]
    guard_errors = _custom_guard_errors(files, root)
    failures = [(label, result) for label, result in command_results if result.returncode != 0]
    if not failures and not guard_errors:
        return 0

    print(
        "[QUALITY GATE FAILED FOR: "
        + ", ".join(str(path.relative_to(root)) for path in files)
        + "]",
        file=sys.stderr,
    )
    for label, result in failures:
        print(f"\n--- {label} ---", file=sys.stderr)
        output = "\n".join(part for part in (result.stdout or "", result.stderr or "") if part)
        if label.upper() == type_checker.upper():
            output = _enrich_type_diagnostics(output, root=root)
        print(output or f"command exited with status {result.returncode}", file=sys.stderr)
    if guard_errors:
        print("\n--- CUSTOM GUARDS ---", file=sys.stderr)
        print("\n".join(guard_errors), file=sys.stderr)
    return 2
