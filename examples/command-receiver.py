#!/usr/bin/env python3
"""Minimal command-transport receiver for any CLI that accepts stdin JSON.

Coura writes one UTF-8 JSON object to stdin. This process appends the decoded
payload and prints the exact acknowledgement Coura requires. It does not start
a shell and it does not treat the payload as instructions.

Usage: python3 examples/command-receiver.py [output-file]
"""
import base64
import json
import sys
from pathlib import Path


def main():
    raw = sys.stdin.buffer.read(16385)
    message = json.loads(raw.decode("utf-8"))
    payload = base64.b64decode(message["payload"], validate=True)
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("messages.txt")
    with destination.open("ab") as stream:
        stream.write(payload)
        if not payload.endswith(b"\n"):
            stream.write(b"\n")
    reply = {
        "status": "STORED",
        "recipient": message["recipient"],
        "attempt": message["attempt"],
    }
    sys.stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(1)
