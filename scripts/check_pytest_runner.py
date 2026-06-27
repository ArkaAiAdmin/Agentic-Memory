"""Pre-commit guard: reject `pytest eval/` unless invoked by run_full_suite.py.

Prevents the MPS/OMP crash that happens when pytest runs multiple eval
files in a single process on Apple Silicon.  The full suite runner
(eval/run_full_suite.py) spawns one subprocess per file, which is safe.
"""

import os
import subprocess
import sys


def main() -> int:
    ppid = os.getppid()
    try:
        cmd = subprocess.check_output(
            ["ps", "-p", str(ppid), "-o", "command="],
            timeout=5,
            text=True,
        ).strip()
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ):
        return 0

    if "pytest" not in cmd.split():
        return 0

    parent_script = os.path.basename(cmd.split()[-1]) if cmd.split() else ""
    if parent_script == "run_full_suite.py":
        return 0

    print(
        f"ERROR: Direct `pytest eval/` detected!\n"
        f"  Parent process: {cmd}\n"
        f"  Use `make test` instead (runs eval/run_full_suite.py which is MPS-safe).\n"
        f"  Single-file: `make test-file FILE=eval/test_foo.py`\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
