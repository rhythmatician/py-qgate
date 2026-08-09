"""Run the repository's shared Python quality gates."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TextIO

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


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


def _resolve_python_files(
    candidates: Sequence[str], root: Path, base_dir: Path
) -> list[Path]:
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


def _tool_command(
    tool: str, arguments: Sequence[str], files: Sequence[Path]
) -> list[str]:
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-stdin",
        action="store_true",
        help="read the Codex PostToolUse JSON payload from stdin",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="check every Python file without modifying files",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="format and apply safe Ruff fixes before checking",
    )
    parser.add_argument(
        "--type-checker",
        choices=("pyright", "dmypy"),
        default="pyright",
        help="type checker to execute (default: pyright)",
    )
    parser.add_argument("paths", nargs="*", help="Python files to check")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    workspace_root: Path | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.ci and args.fix:
        parser.error("--ci and --fix cannot be used together")
    if args.codex_stdin and args.paths:
        parser.error("--codex-stdin cannot be combined with file paths")
    if args.type_checker == "dmypy" and (args.ci or args.codex_stdin):
        parser.error("dmypy is only available for local file checks")

    root = (workspace_root or Path(__file__).resolve().parents[1]).resolve()
    if args.ci:
        files = _discover_python_files(root)
    elif args.codex_stdin:
        payload = _load_codex_payload(stdin or sys.stdin)
        if payload is None:
            return 0
        base_dir = _working_directory(payload, root)
        files = _resolve_python_files(_payload_paths(payload), root, base_dir)
    elif not args.paths:
        files = _discover_python_files(root)
    else:
        files = _resolve_python_files(args.paths, root, root)

    if not files:
        return 0

    command_targets = _ci_command_targets(files, root) if args.ci else files
    ruff = _tool_path("ruff", root)
    type_checker = _tool_path(args.type_checker, root)
    commands: list[tuple[str, list[str]]] = []
    if args.fix:
        commands.extend(
            (
                (
                    "RUFF SAFE FIXES",
                    _tool_command(ruff, ["check", "--fix", "--quiet"], command_targets),
                ),
                (
                    "RUFF FORMAT",
                    _tool_command(ruff, ["format"], command_targets),
                ),
            )
        )
    format_arguments = ["format"]
    if args.ci:
        format_arguments.extend(
            ("--exclude", "*.md", "--exclude", "*.ipynb", "--exclude", "*.pyi")
        )
    if not args.fix:
        commands.append(
            (
                "RUFF FORMAT CHECK",
                _tool_command(ruff, [*format_arguments, "--check"], command_targets),
            )
        )
    commands.extend(
        (
            (
                "RUFF LINT",
                _tool_command(
                    ruff, ["check", "--output-format=concise"], command_targets
                ),
            ),
            (
                args.type_checker.upper(),
                _tool_command(
                    type_checker,
                    ["run", "--", "--strict"] if args.type_checker == "dmypy" else [],
                    command_targets,
                ),
            ),
        )
    )

    command_results = [
        (label, _run_command(command, root)) for label, command in commands
    ]
    guard_errors = _custom_guard_errors(files, root)
    failures = [
        (label, result) for label, result in command_results if result.returncode != 0
    ]
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
        output = "\n".join(
            part for part in (result.stdout or "", result.stderr or "") if part
        )
        print(
            output or f"command exited with status {result.returncode}", file=sys.stderr
        )
    if guard_errors:
        print("\n--- CUSTOM GUARDS ---", file=sys.stderr)
        print("\n".join(guard_errors), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
