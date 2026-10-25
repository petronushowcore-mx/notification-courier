"""Run focused observer checks in independent disposable source copies.

Each child has a deadline, exact raw streams and its real process exit.
The unchanged baseline must pass before any model change is tried.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ORDER = ("OBSERVER_EVENT_JSON", "PARTICIPANT_LENS_CONTROL", "OBSERVER_CALLER_LENS")
INPUTS = ("courier.py", "observer.py", "examples/agenda_collector.py",
          "tests/check_observer.py", "tests/check_observer_lens.py",
          "tests/break_observer_lens.py")
MUTATIONS = (
    ("legacy-publisher", "observer.py",
     '    publication = copy_context()\n'
     '    publication.run(runtime._DISPATCH.set, {"journal": str(journal), "emit_observation": False})\n'
     '    publication.run(runtime._LENS.set, None)\n'
     '    receipt = publication.run(courier.send_data, destination, key, payload)\n',
     '    token = runtime._DISPATCH.set({"journal": str(journal), "emit_observation": False})\n'
     '    try:\n'
     '        receipt = courier.send_data(destination, key, payload)\n'
     '    finally:\n'
     '        runtime._DISPATCH.reset(token)\n',
     "OBSERVER_EVENT_JSON", "RED"),
    ("participant-headers-lost", "courier.py",
     '    header = lens_header(lens["name"], lens["text"])\n',
     '    return line, carrier\n'
     '    header = lens_header(lens["name"], lens["text"])\n',
     "PARTICIPANT_LENS_CONTROL", "RED"),
    ("outer-lens-cleared", "observer.py",
     '    receipt = publication.run(courier.send_data, destination, key, payload)\n',
     '    receipt = publication.run(courier.send_data, destination, key, payload)\n'
     '    runtime._LENS.set(None)\n',
     "OBSERVER_CALLER_LENS", "RED"),
    ("outer-dispatch-cleared", "observer.py",
     '    receipt = publication.run(courier.send_data, destination, key, payload)\n',
     '    receipt = publication.run(courier.send_data, destination, key, payload)\n'
     '    runtime._DISPATCH.set(None)\n',
     "OBSERVER_CALLER_LENS", "RED"),
    ("literal-assignment-order", "observer.py",
     'SCHEMA = "coura.observer.event.v1"\nPROJECTION_SCHEMA = "coura.observer.projection.v1"\n',
     'PROJECTION_SCHEMA = "coura.observer.projection.v1"\nSCHEMA = "coura.observer.event.v1"\n',
     None, "SURVIVE"),
    ("same-site-schema-reach", "observer.py",
     'SCHEMA = "coura.observer.event.v1"\nPROJECTION_SCHEMA = "coura.observer.projection.v1"\n',
     'SCHEMA = "coura.observer.event.v0"\nPROJECTION_SCHEMA = "coura.observer.projection.v1"\n',
     "OBSERVER_EVENT_JSON", "REACH"),
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verdict(stdout, code, expected):
    lines = stdout.decode("utf-8", "replace").splitlines()
    records = []
    for line in lines:
        match = re.fullmatch(r"(PASS|FAIL|ERROR) ([A-Z][A-Z0-9_]+)(?: .*)?", line)
        if match:
            records.append((match.group(1), match.group(2)))
    errors = [marker for state, marker in records if state == "ERROR"]
    reds = [marker for state, marker in records if state == "FAIL"]
    if errors:
        return False, reds[0] if reds else None, records, "ERROR_IS_NOT_A_NAMED_ASSERTION"
    if expected is None:
        good = code == 0 and records == [("PASS", name) for name in ORDER] and lines.count("teeth: 3/3") == 1
    else:
        position = ORDER.index(expected)
        wanted = [("PASS", name) for name in ORDER[:position]] + [("FAIL", expected)]
        good = code == 1 and records == wanted and reds == [expected] and lines.count("teeth: %d/3" % position) == 1
    return good, reds[0] if reds else None, records, None if good else "UNEXPECTED_EXIT_OR_MARKER_SEQUENCE"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    output = args.out.resolve()
    if output.exists() or output.is_relative_to(ROOT) or ROOT.is_relative_to(output):
        parser.error("--out must be a new directory outside the source tree and its parents")
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600")
    output.mkdir(parents=True)
    summary = {"status": "RUNNING", "inputs": {name: digest(ROOT / name) for name in INPUTS},
               "order": list(ORDER), "attempts": []}
    write_json(output / "result.json", summary)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cases = (("baseline", None, None, None, None, "BASELINE"), *MUTATIONS)
    for name, relative, anchor, replacement, expected, kind in cases:
        case = output / name
        case.mkdir()
        item = {"name": name, "kind": kind, "status": "UNKNOWN",
                "target": relative, "expected_first_red": expected}
        stdout = stderr = b""
        try:
            candidate = case / "source"
            shutil.copytree(ROOT, candidate, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
            if relative is not None:
                target = candidate / relative
                text = target.read_text(encoding="utf-8")
                count = text.count(anchor)
                if count != 1:
                    raise ValueError("mutation anchor count %d, expected1" % count)
                item["before_sha256"] = digest(target)
                target.write_text(text.replace(anchor, replacement, 1), encoding="utf-8")
                item["after_sha256"] = digest(target)
                if item["before_sha256"] == item["after_sha256"]:
                    raise ValueError("mutation did not change the source")
            command = [sys.executable, "-B", str(candidate / "tests/check_observer_lens.py"),
                       "--evidence", str(case / "observations")]
            item["argv"] = command
            done = subprocess.run(command, cwd=str(candidate), env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=args.timeout)
            stdout, stderr = done.stdout, done.stderr
            good, first, records, reason = verdict(stdout, done.returncode, expected)
            item.update(exit=done.returncode, first_red=first, completed_markers=records,
                        status="PASS" if good else "FAIL", reason=reason)
        except subprocess.TimeoutExpired as error:
            stdout, stderr = error.stdout or b"", error.stderr or b""
            item.update(error="TIMEOUT", exit=None)
        except Exception as error:
            item.update(error=type(error).__name__ + ": " + str(error), exit=None)
        (case / "stdout.txt").write_bytes(stdout)
        (case / "stderr.txt").write_bytes(stderr)
        item["stdout_sha256"] = hashlib.sha256(stdout).hexdigest()
        item["stderr_sha256"] = hashlib.sha256(stderr).hexdigest()
        write_json(case / "result.json", item)
        summary["attempts"].append(item)
        print("%s %s first_red=%s expected=%s" % (item["status"], name, item.get("first_red"), expected), flush=True)
        if item["status"] != "PASS":
            summary["status"] = item["status"]
            write_json(output / "result.json", summary)
            return 1
        write_json(output / "result.json", summary)
    summary["status"] = "PASS"
    write_json(output / "result.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
