"""Argument parsing and CLI entry point for qgate."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from qgate.engine import (
    _discover_python_files,
    _load_codex_payload,
    _payload_paths,
    _resolve_python_files,
    _working_directory,
    run_gates,
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


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
        print(
            "  skip  pyproject.toml (not found — create one and re-run `qgate init`)"
        )

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
        files = _discover_python_files(root)
    elif args.codex_stdin:
        payload = _load_codex_payload(stdin or sys.stdin)
        if payload is None:
            return 0
        base_dir = _working_directory(payload, root)
        files = _resolve_python_files(_payload_paths(payload), root, base_dir)
    elif not all_paths:
        files = _discover_python_files(root)
    else:
        files = _resolve_python_files(all_paths, root, root)

    return run_gates(
        files=files,
        root=root,
        ci=args.ci,
        fix=args.fix,
        type_checker=args.type_checker,
    )


if __name__ == "__main__":
    raise SystemExit(main())
