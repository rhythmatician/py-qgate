"""Start a checker only after its Windows Job Object owns this process."""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    if sys.stdin.buffer.read(1) != b"1":
        return 127
    try:
        return subprocess.call(sys.argv[1:])
    except OSError as exc:
        print(exc, file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
