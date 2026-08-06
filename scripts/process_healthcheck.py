"""Container process healthcheck without shell-specific process tools."""

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    marker = sys.argv[1]
    for command_line in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            command = command_line.read_bytes().replace(b"\0", b" ").decode(errors="ignore")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if marker in command and "process_healthcheck" not in command:
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
