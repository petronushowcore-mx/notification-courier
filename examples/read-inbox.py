#!/usr/bin/env python3
"""Print unread text for one file-inbox recipient.

Usage: python3 examples/read-inbox.py CONFIG RECIPIENT

Point a CLI that can read a file or a pipe at this script, or at
`coura show RECIPIENT --text`.
"""
import subprocess
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 3:
        print("usage: read-inbox.py CONFIG RECIPIENT", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parent.parent
    command = [sys.executable, str(root / "courier.py"), "--config", sys.argv[1],
               "show", sys.argv[2], "--text"]
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
