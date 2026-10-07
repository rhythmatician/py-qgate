"""Ruff, Pyright/dmypy execution and output formatting."""

from __future__ import annotations

import ast
import locale
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from qgate.windows_job import WindowsJob

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
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
_COMMAND_TIMEOUT_SECONDS = DEFAULT_COMMAND_TIMEOUT_SECONDS
__all__ = [
    "_TYPE_CONTEXT_LIMIT",
    "_captured_text",
    "_custom_guard_errors",
    "_enrich_type_diagnostics",
    "_run_command",
    "run_gates",
    "validate_command_timeout",
]


def validate_command_timeout(value: object) -> float:
    message = "[tool.qgate] command-timeout-seconds must be a finite positive number"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        timeout = float(value)
    except OverflowError as exc:
        raise ValueError(message) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(message)
    return timeout


def _new_job() -> WindowsJob | None:
    return WindowsJob() if sys.platform == "win32" else None


def _stop_process_tree(process: subprocess.Popen[str], job: WindowsJob | None) -> str:
    """Stop the checker and descendants owned by this gate invocation."""
    error = ""
    if job is not None:
        try:
            job.terminate()
        except OSError as exc:
            error = f"could not terminate checker job: {exc}"
            job.close()  # KILL_ON_JOB_CLOSE is the fallback for this owned tree.
    else:
        # On POSIX, a negative PID targets the session's process group.
        try:
            os.kill(-process.pid, 9)  # SIGKILL
        except ProcessLookupError:
            pass
        except OSError as exc:
            error = f"could not terminate checker group: {exc}"
    if process.poll() is None:
        process.kill()
    return error


def _captured_text(value: bytes | str | None, encoding: str) -> str:
    if isinstance(value, bytes):
        return value.decode(encoding, errors="replace")
    return value or ""


def _run_command(
    command: list[str],
    root: Path,
    *,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    timeout_seconds = validate_command_timeout(timeout_seconds)
    output_encoding = locale.getpreferredencoding(False)
    job: WindowsJob | None = None
    try:
        job = _new_job()
        launch_command = [sys.executable, "-m", "qgate._launcher", *command] if job else command
        process = subprocess.Popen(
            launch_command,
            cwd=root,
            stdin=subprocess.PIPE if job else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=output_encoding,
            start_new_session=sys.platform != "win32",
        )
        try:
            if job is not None:
                job.assign(process.pid)
        except OSError:
            process.kill()
            process.communicate(timeout=5)
            raise

        try:
            stdout, stderr = process.communicate(
                input="1" if job is not None else None, timeout=timeout_seconds
            )
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            termination_error = _stop_process_tree(process, job)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired as exc:
                stdout = _captured_text(exc.stdout, output_encoding)
                stderr = _captured_text(exc.stderr, output_encoding)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                if process.poll() is None:
                    process.kill()
                stderr += "\nchecker output could not be drained after termination"
            if termination_error:
                stderr += f"\n{termination_error}"
            rendered_command = subprocess.list2cmdline(command)
            stderr += f"\n{rendered_command} timed out after {timeout_seconds:g} seconds"
            return subprocess.CompletedProcess(command, 124, stdout, stderr)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))
    finally:
        if job is not None:
            job.close()


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


def _coherent_pyright_targets(files: Sequence[Path], root: Path) -> list[Path] | None:
    """Compact a complete selected tree without splitting Pyright analysis."""
    command = _tool_command("pyright", [], files)
    if len(subprocess.list2cmdline(command)) <= _WINDOWS_SAFE_COMMAND_LENGTH:
        return list(files)

    workspace = root.resolve()
    selected = {path.resolve() for path in files}
    candidates = {workspace}
    for path in selected:
        parent = path.parent
        while parent != workspace and workspace in parent.parents:
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
    compacted = sorted(targets)
    command = _tool_command("pyright", [], compacted)
    if len(subprocess.list2cmdline(command)) > _WINDOWS_SAFE_COMMAND_LENGTH:
        return None
    return compacted


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
    type_checker: str = "pyright",
    command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> int:
    """Run quality gates on the given files and return an exit code."""
    command_timeout_seconds = validate_command_timeout(command_timeout_seconds)
    if not files:
        return 0

    ruff = _tool_path("ruff", root)
    tc = _tool_path(type_checker, root)
    commands: list[tuple[str, list[str]]] = []

    def add_commands(
        label: str,
        tool: str,
        arguments: Sequence[str],
        *,
        targets: Sequence[Path] = files,
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
        add_commands("RUFF SAFE FIXES", ruff, ["check", "--fix", "--quiet"], bounded=True)
        add_commands("RUFF FORMAT", ruff, ["format", "--quiet"], bounded=True)
    format_arguments = ["format"]
    if ci:
        format_arguments.extend(("--exclude", "*.md", "--exclude", "*.ipynb", "--exclude", "*.pyi"))
    if not fix:
        add_commands(
            "RUFF FORMAT CHECK",
            ruff,
            [*format_arguments, "--check", "--quiet"],
            bounded=True,
        )
    add_commands(
        "RUFF LINT",
        ruff,
        ["check", "--quiet", "--output-format=concise"],
        bounded=True,
    )
    type_targets = _ci_command_targets(files, root) if ci else files
    if type_checker == "pyright" and not ci:
        type_targets = _coherent_pyright_targets(files, root)
        if type_targets is None:
            print(
                "[QUALITY GATE FAILED]\n\n--- PYRIGHT ---\n"
                "Selected Gate Targets exceed the Windows command-line limit, and qgate "
                "cannot run one coherent Pyright analysis without including unselected files.",
                file=sys.stderr,
            )
            return 2
    add_commands(
        type_checker.upper(),
        tc,
        ["run", "--"] if type_checker == "dmypy" else [],
        targets=type_targets,
        bounded=not ci and type_checker != "pyright",
    )

    command_results = [
        (label, _run_command(command, root, timeout_seconds=command_timeout_seconds))
        for label, command in commands
    ]
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
