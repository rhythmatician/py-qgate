"""Ruff, Pyright/dmypy execution and output formatting."""

from __future__ import annotations

import ast
import contextlib
import os
import re
import shutil
import signal
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
_WINDOWS_CREATE_SUSPENDED = 0x00000004
_COMMAND_TIMEOUT_SECONDS = 300
_OUTPUT_DRAIN_SECONDS = 1


class _WindowsJob:
    """Keep a checker and its descendants in one terminable Windows job."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.INT,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._kernel32 = kernel32
        self._handle = handle
        try:
            self._limits = ExtendedLimitInformation()
            self._limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(self._limits), ctypes.sizeof(self._limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            # Popen holds the suspended process, so its PID cannot be reused here.
            process_handle = kernel32.OpenProcess(0x0101, False, process.pid)
            if not process_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not kernel32.AssignProcessToJobObject(handle, process_handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                kernel32.CloseHandle(process_handle)
            self._resume(process.pid)
        except BaseException:
            self.close()
            raise

    def _resume(self, process_id: int) -> None:
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = self._kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD

        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            thread_ids: list[int] = []
            found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process_id:
                    thread_ids.append(entry.th32ThreadID)
                found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            if len(thread_ids) != 1:
                raise OSError(f"expected one suspended checker thread, found {len(thread_ids)}")
            thread = kernel32.OpenThread(0x0002, False, thread_ids[0])  # THREAD_SUSPEND_RESUME
            if not thread:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if kernel32.ResumeThread(thread) != 1:
                    raise OSError("could not resume suspended checker thread")
            finally:
                kernel32.CloseHandle(thread)
        finally:
            kernel32.CloseHandle(snapshot)

    def terminate(self) -> None:
        import ctypes

        if not self._kernel32.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def release(self) -> None:
        """Let detached children continue after an ordinary checker exit."""
        import ctypes

        self._limits.BasicLimitInformation.LimitFlags = 0
        if not self._kernel32.SetInformationJobObject(
            self._handle, 9, ctypes.byref(self._limits), ctypes.sizeof(self._limits)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        self.close()

    def close(self) -> None:
        self._kernel32.CloseHandle(self._handle)


def _run_command(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    process: subprocess.Popen[str] | None = None
    job: _WindowsJob | None = None
    completed_normally = False
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=_WINDOWS_CREATE_SUSPENDED if sys.platform == "win32" else 0,
            start_new_session=sys.platform != "win32",
        )
        if sys.platform == "win32":
            job = _WindowsJob(process)
        try:
            stdout, stderr = process.communicate(timeout=_COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                assert job is not None
                job.terminate()
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=_OUTPUT_DRAIN_SECONDS)
            rendered_command = subprocess.list2cmdline(command)
            return subprocess.CompletedProcess(
                command,
                124,
                "",
                f"{rendered_command} timed out after {_COMMAND_TIMEOUT_SECONDS} seconds",
            )
        if sys.platform == "win32":
            assert job is not None
            job.release()
            job = None
        completed_normally = True
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))
    finally:
        if job is not None:
            job.close()
        elif process is not None and not completed_normally:
            if sys.platform == "win32":
                with contextlib.suppress(OSError):
                    process.kill()
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)


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
) -> int:
    """Run quality gates on the given files and return an exit code."""
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
