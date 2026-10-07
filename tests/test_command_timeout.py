"""Real-process coverage for checker timeout cleanup through the gate."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from qgate import engine


def python_as_checker(_name: str, _root: Path) -> str:
    return sys.executable


def use_python_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "format").write_text("raise SystemExit(0)\n")
    (tmp_path / "check").write_text("raise SystemExit(0)\n")
    monkeypatch.setattr("qgate.engine._tool_path", python_as_checker)


def test_timeout_reaps_descendant_holding_output_pipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port_file = tmp_path / "child-port"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import socket, sys, time\n"
        "from pathlib import Path\n"
        "listener = socket.socket()\n"
        "listener.bind(('127.0.0.1', 0))\n"
        "listener.listen()\n"
        "Path(sys.argv[1]).write_text(str(listener.getsockname()[1]))\n"
        "time.sleep(4)\n"
    )
    launcher_script = tmp_path / "launcher.py"
    launcher_script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "port = Path(__file__).with_name('child-port')\n"
        "child = Path(__file__).with_name('child.py')\n"
        "subprocess.Popen([sys.executable, str(child), str(port)], "
        "stdin=subprocess.DEVNULL)\n"
        "deadline = time.monotonic() + 8\n"
        "while not port.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
    )
    checker_command = [sys.executable, str(launcher_script)]
    original_communicate = subprocess.Popen[str].communicate
    ready_at: float | None = None

    def communicate_after_child_starts(
        process: subprocess.Popen[str],
        input: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        nonlocal ready_at
        if process.args == checker_command and ready_at is None:
            deadline = time.monotonic() + 8
            while not port_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert port_file.exists()
            ready_at = time.monotonic()
        return original_communicate(
            process, input=input, timeout=timeout if process.args == checker_command else 8
        )

    monkeypatch.setattr(subprocess.Popen, "communicate", communicate_after_child_starts)
    use_python_tools(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "_COMMAND_TIMEOUT_SECONDS", 0.5)

    assert engine.run_gates(files=[launcher_script], root=tmp_path) == 2

    assert ready_at is not None
    assert time.monotonic() - ready_at < 2
    output = capsys.readouterr().err
    assert "--- PYRIGHT ---" in output
    assert "timed out after 0.5 seconds" in output
    assert "--- RUFF" not in output
    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex(("127.0.0.1", int(port_file.read_text()))) != 0


def test_checker_exit_and_output_reach_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    checker = tmp_path / "checker.py"
    checker.write_text(
        "import sys\nprint('checker-out')\nprint('checker-error', file=sys.stderr)\n"
        "raise SystemExit(7)\n"
    )
    use_python_tools(tmp_path, monkeypatch)

    assert engine.run_gates(files=[checker], root=tmp_path) == 2

    output = capsys.readouterr().err
    assert "--- PYRIGHT ---" in output
    assert "checker-out" in output
    assert "checker-error" in output


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job lifecycle")
def test_success_keeps_detached_checker_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port_file = tmp_path / "detached-port"
    child_script = tmp_path / "detached.py"
    child_script.write_text(
        "import socket, sys, time\n"
        "from pathlib import Path\n"
        "listener = socket.socket()\n"
        "listener.bind(('127.0.0.1', 0))\n"
        "listener.listen()\n"
        "Path(sys.argv[1]).write_text(str(listener.getsockname()[1]))\n"
        "time.sleep(4)\n"
    )
    launcher_script = tmp_path / "detach.py"
    launcher_script.write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "port = Path(__file__).with_name('detached-port')\n"
        "child = Path(__file__).with_name('detached.py')\n"
        "subprocess.Popen([sys.executable, str(child), str(port)], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "deadline = time.monotonic() + 8\n"
        "while not port.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "print('done')\n"
    )
    use_python_tools(tmp_path, monkeypatch)

    assert engine.run_gates(files=[launcher_script], root=tmp_path) == 0

    assert capsys.readouterr().err == ""
    assert port_file.exists()
    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex(("127.0.0.1", int(port_file.read_text()))) == 0
