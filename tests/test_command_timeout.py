"""Real-process coverage for checker timeout cleanup through the gate."""

from __future__ import annotations

import contextlib
import hashlib
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


def test_utf8_stdin_checker_timeout_keeps_bounded_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = tmp_path / "read_then_wait.py"
    receipt = tmp_path / "stdin-receipt"
    checker.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())\n"
        "time.sleep(4)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(engine, "_COMMAND_TIMEOUT_SECONDS", 0.5)

    started = time.monotonic()
    result = engine._run_command(  # pyright: ignore[reportPrivateUsage]
        [sys.executable, str(checker), str(receipt)],
        tmp_path,
        input_data="package/résumé.py\n",
    )

    assert result.returncode == 124
    assert receipt.read_bytes().decode("utf-8").splitlines() == ["package/résumé.py"]
    assert time.monotonic() - started < 2


def test_large_utf8_stdin_is_a_complete_regular_file(
    tmp_path: Path,
) -> None:
    checker = tmp_path / "inspect_stdin.py"
    checker.write_text(
        "import hashlib, os, stat, sys\n"
        "content = sys.stdin.buffer.read()\n"
        "print(stat.S_ISREG(os.fstat(0).st_mode))\n"
        "print(len(content))\n"
        "print(hashlib.sha256(content).hexdigest())\n",
        encoding="utf-8",
    )
    input_data = "package/résumé.py\n" * 50_000
    expected_bytes = input_data.encode("utf-8")

    result = engine._run_command(  # pyright: ignore[reportPrivateUsage]
        [sys.executable, str(checker)], tmp_path, input_data=input_data
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "True",
        str(len(expected_bytes)),
        hashlib.sha256(expected_bytes).hexdigest(),
    ]


def test_unencodable_stdin_fails_before_starting_checker(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    checker = tmp_path / "mark_start.py"
    checker.write_text(
        "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('started')\n",
        encoding="utf-8",
    )

    result = engine._run_command(  # pyright: ignore[reportPrivateUsage]
        [sys.executable, str(checker), str(marker)], tmp_path, input_data="bad/\ud800.py\n"
    )

    assert result.returncode == 127
    assert "surrogates not allowed" in result.stderr
    assert not marker.exists()


def test_temp_stdin_creation_error_fails_before_starting_checker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "started"
    checker = tmp_path / "mark_start.py"
    checker.write_text(
        "import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('started')\n",
        encoding="utf-8",
    )

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("temporary stdin unavailable")

    monkeypatch.setattr(engine.tempfile, "TemporaryFile", unavailable)
    result = engine._run_command(  # pyright: ignore[reportPrivateUsage]
        [sys.executable, str(checker), str(marker)], tmp_path, input_data="selected.py\n"
    )

    assert result.returncode == 127
    assert "temporary stdin unavailable" in result.stderr
    assert not marker.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows stdin and job lifecycle")
def test_never_reading_checker_with_large_stdin_times_out_under_outer_watchdog(
    tmp_path: Path,
) -> None:
    port_file = tmp_path / "checker-port"
    result_file = tmp_path / "checker-result"
    checker = tmp_path / "never_read.py"
    checker.write_text(
        "import socket, sys, time\n"
        "from pathlib import Path\n"
        "listener = socket.socket()\n"
        "listener.bind(('127.0.0.1', 0))\n"
        "listener.listen()\n"
        "Path(sys.argv[1]).write_text(str(listener.getsockname()[1]))\n"
        "time.sleep(8)\n",
        encoding="utf-8",
    )
    driver = tmp_path / "run_checker.py"
    driver.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from qgate import engine\n"
        "engine._COMMAND_TIMEOUT_SECONDS = 0.5\n"
        "result = engine._run_command(\n"
        "    [sys.executable, sys.argv[1], sys.argv[2]],\n"
        "    Path(sys.argv[3]), input_data='package/résumé.py\\n' * 100_000,\n"
        ")\n"
        "Path(sys.argv[4]).write_text(str(result.returncode))\n",
        encoding="utf-8",
    )
    outer = subprocess.Popen(
        [
            sys.executable,
            str(driver),
            str(checker),
            str(port_file),
            str(tmp_path),
            str(result_file),
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=engine._WINDOWS_CREATE_SUSPENDED,  # pyright: ignore[reportPrivateUsage]
    )
    started = time.monotonic()
    outer_job: engine._WindowsJob | None = None  # pyright: ignore[reportPrivateUsage]
    try:
        outer_job = engine._WindowsJob(outer)  # pyright: ignore[reportPrivateUsage]
        try:
            _stdout, stderr = outer.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            outer_job.terminate()
            try:
                outer.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pytest.fail("owned checker tree did not drain after termination")
            pytest.fail("owned checker driver exceeded the outer watchdog")
    finally:
        if outer_job is not None:
            outer_job.close()
        else:
            outer.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                outer.communicate(timeout=2)
        if outer.stdout is not None:
            outer.stdout.close()
        if outer.stderr is not None:
            outer.stderr.close()

    assert outer.returncode == 0, stderr
    assert result_file.read_text() == "124"
    assert time.monotonic() - started < 5
    assert port_file.is_file()
    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex(("127.0.0.1", int(port_file.read_text()))) != 0


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
