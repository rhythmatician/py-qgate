"""Argument parsing and CLI entry point for qgate."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from qgate.codex import select_codex_gate_targets
from qgate.engine import run_gates
from qgate.targeting import select_all_gate_targets, select_gate_targets

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_VSCODE_SETTINGS = "editor.formatOnSave"


def _without_jsonc_comments(text: str) -> str:
    """Return JSONC text with comments and trailing commas removed."""
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
        elif char == '"':
            in_string = True
            result.append(char)
            index += 1
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise ValueError("unterminated comment")
            index = end + 2
        else:
            result.append(char)
            index += 1

    cleaned = "".join(result)
    return _remove_trailing_commas(cleaned)


def _remove_trailing_commas(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            result.append(char)
        elif char in "]}":
            while result and result[-1].isspace():
                result.pop()
            if result and result[-1] == ",":
                result.pop()
            result.append(char)
        else:
            result.append(char)
    return "".join(result)


def _settings_with_format_on_save(text: str) -> str:
    try:
        settings = json.loads(_without_jsonc_comments(text))
    except ValueError:
        raise ValueError("invalid VS Code settings") from None
    if not isinstance(settings, dict):
        raise ValueError("VS Code settings must be an object")

    key = json.dumps(_VSCODE_SETTINGS)
    key_start = -1
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        if not in_string and text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
            continue
        if not in_string and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end == -1 else end + 2
            continue
        if not in_string and text.startswith(key, index):
            key_start = index
            break
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        index += 1
    if key_start != -1:
        colon = text.find(":", key_start + len(key))
        if colon != -1:
            value_start = colon + 1
            while value_start < len(text) and text[value_start].isspace():
                value_start += 1
            value_end = value_start
            depth = 0
            in_string = False
            escaped = False
            while value_end < len(text):
                char = text[value_end]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                elif char == '"':
                    in_string = True
                elif char in "[{":
                    depth += 1
                elif char in "]}":
                    if depth == 0:
                        break
                    depth -= 1
                elif char == "," and depth == 0:
                    break
                value_end += 1
            while value_end > value_start and text[value_end - 1].isspace():
                value_end -= 1
            return text[:value_start] + "true" + text[value_end:]

    closing_brace = text.rfind("}")
    if closing_brace == -1:
        raise ValueError("VS Code settings must be an object")
    prefix = text[:closing_brace].rstrip()
    separator = "\n" if prefix.rstrip().endswith(("{", ",")) else ",\n"
    return prefix + separator + f'    "{_VSCODE_SETTINGS}": true\n' + text[closing_brace:]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quality gates for CI, pre-commit, and human/agent co-development."
    )
    parser.add_argument(
        "command",
        nargs="?",
        help="subcommand to run (e.g. 'init' to scaffold config files)",
    )
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


def _run_init(target_dir: Path) -> int:
    """Scaffold quality-gate config files into *target_dir*."""
    scaffolds = [
        (_TEMPLATES_DIR / "hooks.json", target_dir / ".codex" / "hooks.json"),
        (
            _TEMPLATES_DIR / "pre-commit-config.yaml",
            target_dir / ".pre-commit-config.yaml",
        ),
        (
            _TEMPLATES_DIR / "ci.yml",
            target_dir / ".github" / "workflows" / "ci.yml",
        ),
    ]

    for src, dst in scaffolds:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            print(f"  skip  {dst.relative_to(target_dir)} (already exists)")
        else:
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"  write {dst.relative_to(target_dir)}")

    vscode_settings = target_dir / ".vscode" / "settings.json"
    vscode_settings.parent.mkdir(parents=True, exist_ok=True)
    if vscode_settings.exists():
        existing = vscode_settings.read_text(encoding="utf-8")
        try:
            updated = _settings_with_format_on_save(existing)
        except ValueError:
            print("  skip  .vscode/settings.json (invalid JSON or JSONC; please fix it and re-run)")
        else:
            if updated != existing:
                vscode_settings.write_text(updated, encoding="utf-8")
                print("  write .vscode/settings.json (enabled format-on-save)")
            else:
                print("  skip  .vscode/settings.json (format-on-save already enabled)")
    else:
        vscode_settings.write_text('{\n    "editor.formatOnSave": true\n}\n', encoding="utf-8")
        print("  write .vscode/settings.json")

    # Append ruff/pyright settings to pyproject.toml if missing
    pyproject = target_dir / "pyproject.toml"
    snippet = (_TEMPLATES_DIR / "pyproject-snippet.toml").read_text(encoding="utf-8")
    if pyproject.exists():
        existing = pyproject.read_text(encoding="utf-8")
        if "[tool.ruff]" in existing:
            print("  skip  pyproject.toml [tool.ruff] (already present)")
        else:
            with pyproject.open("a", encoding="utf-8") as fh:
                fh.write(snippet)
            print("  write pyproject.toml (appended [tool.ruff] settings)")
    else:
        print("  skip  pyproject.toml (not found — create one and re-run `qgate init`)")

    print("\nDone. Review the generated files and commit them.")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    workspace_root: Path | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return _run_init(workspace_root or Path.cwd())

    # Gate-mode validation
    if args.ci and args.fix:
        parser.error("--ci and --fix cannot be used together")
    all_paths = ([args.command] if args.command else []) + list(args.paths)
    if args.codex_stdin and all_paths:
        parser.error("--codex-stdin cannot be combined with file paths")
    if args.type_checker == "dmypy" and (args.ci or args.codex_stdin):
        parser.error("dmypy is only available for local file checks")

    root = (workspace_root or Path.cwd()).resolve()

    if args.ci:
        files = select_all_gate_targets(root)
    elif args.codex_stdin:
        files = select_codex_gate_targets(stdin or sys.stdin, root)
    elif not all_paths:
        files = select_all_gate_targets(root)
    else:
        files = select_gate_targets(
            all_paths,
            workspace=root,
            reported_working_directory=root,
        )

    return run_gates(
        files=files,
        root=root,
        ci=args.ci,
        fix=args.fix,
        type_checker=args.type_checker,
    )


if __name__ == "__main__":
    raise SystemExit(main())
