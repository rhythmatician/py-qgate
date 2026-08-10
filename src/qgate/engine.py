"""Ruff, Pyright/dmypy execution and output formatting."""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_GETATTR_LITERAL_PATTERN = re.compile(
    r"""getattr\(\s*[a-zA-Z_]\w*\s*,\s*(['"])(.*?)\1\s*,\s*None\s*\)""",
    re.DOTALL,
)
_TYPE_DIAGNOSTIC_PATTERN = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<column>\d+) - error: "
    r'Cannot access attribute "(?P<member>[^"]+)" for class "(?P<receiver>[^"]+)"',
    re.MULTILINE,
)
_TYPE_CONTEXT_LIMIT = 1200
_TYPE_CONTEXT_ITEM_LIMIT = 300
_WINDOWS_SAFE_COMMAND_LENGTH = 16_000


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


def _tool_commands(
    tool: str,
    arguments: Sequence[str],
    files: Sequence[Path],
    *,
    bounded: bool,
) -> list[list[str]]:
    """Build exact-target commands, batching long runs for Windows safety."""
    if not bounded:
        return [_tool_command(tool, arguments, files)]

    prefix = [tool, *arguments]
    batches: list[list[str]] = []
    batch = prefix.copy()
    for path in files:
        candidate = [*batch, str(path)]
        if len(subprocess.list2cmdline(candidate)) > _WINDOWS_SAFE_COMMAND_LENGTH and len(
            batch
        ) > len(prefix):
            batches.append(batch)
            batch = [*prefix, str(path)]
        else:
            batch = candidate
    if len(batch) > len(prefix):
        batches.append(batch)
    return batches


def _coherent_pyright_targets(files: Sequence[Path], root: Path) -> list[Path]:
    """Compact a complete selected tree without splitting Pyright analysis."""
    command = _tool_command("pyright", [], files)
    if len(subprocess.list2cmdline(command)) <= _WINDOWS_SAFE_COMMAND_LENGTH:
        return list(files)

    selected = {path.resolve() for path in files}
    candidates = {root.resolve()}
    for path in selected:
        parent = path.parent
        while parent != root.resolve() and root.resolve() in parent.parents:
            candidates.add(parent)
            parent = parent.parent

    remaining = selected.copy()
    targets: list[Path] = []
    for directory in sorted(candidates, key=lambda path: len(path.parts)):
        descendants = {path.resolve() for path in directory.rglob("*.py")}
        if descendants and descendants <= remaining:
            targets.append(directory)
            remaining -= descendants

    targets.extend(sorted(remaining))
    return sorted(targets)


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
            if (
                isinstance(child, ast.AnnAssign)
                and isinstance(child.target, ast.Name)
                and child.target.id == member
            ):
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
    root = root.resolve()
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
    full_workspace: bool = False,
    type_checker: str = "pyright",
) -> int:
    """Run quality gates on the given files and return an exit code."""
    if not files:
        return 0

    command_targets = _ci_command_targets(files, root) if ci else files
    ruff = _tool_path("ruff", root)
    tc = _tool_path(type_checker, root)
    commands: list[tuple[str, list[str]]] = []

    def add_commands(
        label: str,
        tool: str,
        arguments: Sequence[str],
        *,
        targets: Sequence[Path] = command_targets,
        bounded: bool = False,
    ) -> None:
        commands.extend(
            (label, command)
            for command in _tool_commands(
                tool,
                arguments,
                targets,
                bounded=bounded,
            )
        )

    if fix:
        add_commands("RUFF SAFE FIXES", ruff, ["check", "--fix", "--quiet"], bounded=not ci)
        add_commands("RUFF FORMAT", ruff, ["format", "--quiet"], bounded=not ci)
    format_arguments = ["format"]
    if ci:
        format_arguments.extend(("--exclude", "*.md", "--exclude", "*.ipynb", "--exclude", "*.pyi"))
    if not fix:
        add_commands(
            "RUFF FORMAT CHECK",
            ruff,
            [*format_arguments, "--check", "--quiet"],
            bounded=not ci,
        )
    add_commands(
        "RUFF LINT",
        ruff,
        ["check", "--quiet", "--output-format=concise"],
        bounded=not ci,
    )
    type_targets = command_targets
    if type_checker == "pyright" and not ci:
        type_targets = _coherent_pyright_targets(command_targets, root)
    add_commands(
        type_checker.upper(),
        tc,
        ["run", "--"] if type_checker == "dmypy" else [],
        targets=type_targets,
        bounded=not ci,
    )

    command_results = [(label, _run_command(command, root)) for label, command in commands]
    guard_errors = _custom_guard_errors(files, root)
    failures = [(label, result) for label, result in command_results if result.returncode != 0]
    if not failures and not guard_errors:
        return 0

    type_checker_label = type_checker.casefold()
    print(
        "[QUALITY GATE FAILED FOR: "
        + ", ".join(str(path.relative_to(root)) for path in files)
        + "]",
        file=sys.stderr,
    )
    for label, result in failures:
        print(f"\n--- {label} ---", file=sys.stderr)
        output = "\n".join(part for part in (result.stdout or "", result.stderr or "") if part)
        if label.casefold() == type_checker_label:
            output = _enrich_type_diagnostics(output, root=root)
        print(output or f"command exited with status {result.returncode}", file=sys.stderr)
    if guard_errors:
        print("\n--- CUSTOM GUARDS ---", file=sys.stderr)
        print("\n".join(guard_errors), file=sys.stderr)
    return 2
