"""Check focused doctor regressions in independent source copies.

Run only after a separate passing focused baseline. This command does not repeat it.
Linux executable fixtures do not establish native Windows or installed-host support.
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
NO_QUEUE = "test_doctor_builtin_codex_no_queue"
NO_BINARY = "test_doctor_builtin_codex_missing_binary"
NO_EXEC = "test_doctor_builtin_codex_no_execution"
CONFLICT = "test_doctor_builtin_claude_conflict"
EXACT = "test_doctor_builtin_claude_exact_entrypoints"
NO_WRITES = "test_doctor_builtin_no_writes"
GENERIC = "test_doctor_builtin_generic_unchanged"

# All replacements are model changes. Every anchor must occur exactly once.
MUTATIONS = (
    ("codex-omitted", "coura_ux.py", '    if aliases["codex"]:\n',
     '    if False and aliases["codex"]:\n', NO_QUEUE, False),
    ("binary-absence-ignored", "courier.py",
     "    binary = _first_executable(command)\n    if not binary:\n",
     "    binary = _first_executable(command)\n    if False and not binary:\n", NO_BINARY, False),
    ("version-executed", "coura_ux.py", 'aliases["codex"], probe_version=False)',
     'aliases["codex"], probe_version=True)', NO_EXEC, False),
    ("hook-omitted", "coura_ux.py",
     '        courier._doctor_claude_hooks(route_checks, context, aliases["claude"])',
     '        route_checks.extend([])', CONFLICT, False),
    ("archive-launcher-lost", "courier.py",
     "    if archive:\n        return [sys.executable, str(Path(archive).resolve())]\n",
     "    if False and archive:\n        return [sys.executable, str(Path(archive).resolve())]\n", EXACT, False),
    ("inbox-inspected", "coura_ux.py",
     '        courier._doctor_claude_hooks(route_checks, context, aliases["claude"])\n',
     '        courier._doctor_claude_hooks(route_checks, context, aliases["claude"])\n'
     '        for alias in aliases["claude"]:\n'
     '            courier._doctor_inbox(route_checks, alias, state, state / "inbox", instance, instance.resolve(alias))\n',
     NO_WRITES, False),
    ("generic-as-builtin", "coura_ux.py",
     'if target.client == kind] for kind in ("claude", "codex")}',
     'if target.client == kind or (kind == "codex" and target.client == "worker")] for kind in ("claude", "codex")}',
     GENERIC, False),
    ("canonical-archive-unrecognised", "courier.py",
     '    names = {"coura", "courier.py", "coura.pyz"}',
     '    names = {"coura", "courier.py"}', CONFLICT, False),
    ("renamed-archive-unrecognised", "courier.py",
     '        names.add(Path(archive).name.lower())',
     '        names.add("coura.pyz")', CONFLICT, False),
    ("pure-assignment-order", "coura_ux.py", "    problems = []\n    notes = []\n",
     "    notes = []\n    problems = []\n", None, True),
    ("same-site-reach", "coura_ux.py", "    problems = []\n    notes = []\n",
     '    problems = [_problem("FIXTURE", "unexpected problem", "fixture")]\n    notes = []\n',
     NO_EXEC, False),
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, document):
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def first_red(stdout):
    # The subject uses chronological one-line FAIL records, not unittest summaries.
    for line in stdout.splitlines():
        match = re.fullmatch(r"FAIL (test_[A-Za-z0-9_]+)(?: .*)?", line)
        if match:
            return match.group(1)
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args(argv)
    output = args.out.resolve()
    if output.exists() or output.is_relative_to(ROOT) or ROOT.is_relative_to(output):
        parser.error("--out must be a new directory outside the source tree and its parents")
    if not 1 <= args.timeout <= 600:
        parser.error("--timeout must be between 1 and 600 seconds")
    output.mkdir(parents=True)
    pins = {name: digest(ROOT / name) for name in (
        "courier.py", "coura_ux.py", "build_coura_pyz.py", "tests/check_ux.py", "tests/break_doctor.py")}
    summary = {"status": "RUNNING", "inputs": pins, "attempts": [],
               "baseline": "not run here; owner must establish a passing focused baseline separately"}
    write_json(output / "result.json", summary)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name, relative, anchor, replacement, expected, survive in MUTATIONS:
        case = output / name
        case.mkdir()
        item = {"name": name, "status": "UNKNOWN", "expected_first_red": expected,
                "survive": survive, "target": relative}
        try:
            candidate = case / "source"
            shutil.copytree(ROOT, candidate, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
            target = candidate / relative
            text = target.read_text(encoding="utf-8")
            if text.count(anchor) != 1:
                raise ValueError("mutation anchor count is %d, expected 1" % text.count(anchor))
            item["before_sha256"] = digest(target)
            target.write_text(text.replace(anchor, replacement, 1), encoding="utf-8")
            item["after_sha256"] = digest(target)
            # New process and fresh module imports per copy; its test builds its own archive.
            command = [sys.executable, "-B", str(candidate / "tests" / "check_ux.py"), "--doctor-only"]
            item["argv"] = command
            done = subprocess.run(command, cwd=str(candidate), env=env, capture_output=True,
                                  text=True, timeout=args.timeout)
            (case / "stdout.txt").write_text(done.stdout, encoding="utf-8")
            (case / "stderr.txt").write_text(done.stderr, encoding="utf-8")
            red = first_red(done.stdout)
            item.update(returncode=done.returncode, first_red=red)
            records = []
            for line in done.stdout.splitlines():
                match = re.fullmatch(r"(?:PASS|FAIL) (test_[A-Za-z0-9_]+)(?: .*)?", line)
                if match:
                    records.append(match.group(1))
            item["completed_markers"] = records
            if records != [NO_QUEUE, NO_BINARY, NO_EXEC, CONFLICT, EXACT, NO_WRITES, GENERIC]:
                raise ValueError("INCOMPLETE_FOCUS: missing, duplicate or out-of-order marker")
            passed = done.returncode == 0 and red is None if survive else (
                done.returncode != 0 and red == expected)
            item["status"] = "PASS" if passed else "FAIL"
        except subprocess.TimeoutExpired as error:
            for field in ("stdout", "stderr"):
                value = getattr(error, field) or b""
                if isinstance(value, bytes):
                    value = value.decode("utf-8", "replace")
                (case / (field + ".txt")).write_text(value, encoding="utf-8")
            item["error"] = "TIMEOUT"
        except Exception as error:
            item["error"] = type(error).__name__ + ": " + str(error)
        write_json(case / "result.json", item)
        summary["attempts"].append(item)
        print("%s %s first_red=%s expected=%s" % (
            item["status"], name, item.get("first_red"), expected), flush=True)
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