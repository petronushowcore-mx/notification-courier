#!/usr/bin/env python3
"""Print a status projection from an Agenda data inbox.

This collector does not call a model and does not treat STORED as READ.
Body text is ignored for verified state. Verified task, read, acceptance,
and execution stay unknown. It only parses observer event JSON.
"""
import base64
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import courier
import observer


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: agenda_collector.py CONFIG RECIPIENT\n")
        return 2
    app = courier.Courier.from_config(argv[1])
    target = app.resolve(argv[2])
    buffer = io.StringIO()
    app.reader(target).show(target, out=buffer, text=False)
    raw = buffer.getvalue().strip()
    document = json.loads(raw) if raw else {"messages": []}
    events = []
    for row in document.get("messages", []):
        try:
            payload = base64.b64decode(row["payload"], validate=True)
            event = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeError, KeyError, TypeError):
            continue
        if type(event) is dict and event.get("schema") == observer.SCHEMA:
            events.append(event)
    projection = observer.project(events)
    json.dump({
        "collector": "agenda",
        "model": False,
        "read_means": "UNMEASURED",
        "event_count": len(events),
        "projection": projection,
    }, sys.stdout, ensure_ascii=True, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except courier.Refused as error:
        sys.stderr.write(json.dumps({"status": "REFUSED", "detail": str(error.args[0])[:300]}) + "\n")
        raise SystemExit(1)
