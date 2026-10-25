"""Causal mutations for observer queue checks; only disposable copies are changed."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REGRESSIONS = [
    ("receipt imports observer", "courier.py",
     "            hook = _AFTER_PARTICIPANT_COMMIT.get()",
     "            load_observer_module()\n            hook = _AFTER_PARTICIPANT_COMMIT.get()",
     "OBS_RECEIPT_NO_OBSERVER_IO"),
    ("unknown prefix pins selection", "observer.py",
     '        previous = self.position("round", -1)',
     '        if self.db.execute("SELECT 1 FROM events WHERE state=\'unknown\' LIMIT 1").fetchone():\n            return []\n        previous = self.position("round", -1)',
     "OBS_UNKNOWN_PREFIX_PROGRESS"),
    ("history materialised on automatic pass", "observer.py",
     "    def counts(self):\n        return dict(self.db.execute(",
     "    def counts(self):\n        history = list(read_spans(outbox_path(self.courier)))\n        return dict(self.db.execute(",
     "OBS_INDEX_BOUNDED_MEMORY"),
    ("source cursor never advances", "observer.py",
     '        index.set_position("journal", consumed)',
     '        index.set_position("journal", 0)',
     "OBS_INTERLEAVED_SOURCE_PROGRESS"),
    ("deferred plaintext lost", "courier.py",
     "copied = dict(final_row, observer_body=line)",
     'copied = dict(final_row, observer_body="")',
     "OBS_DEFERRED_BODY_BOUNDARY"),
    ("index precedes durable audit", "observer.py",
     "    def append(self, row):\n        offset = _append_outbox(self.courier, row)",
     "    def append(self, row):\n        self._apply(0, row)\n        offset = _append_outbox(self.courier, row)",
     "OBS_AUDIT_BEFORE_INDEX"),
    ("indexed prefix called complete", "observer.py",
     '"index_complete": ready, "source_recovery_pending": pending_sources',
     '"index_complete": True, "source_recovery_pending": pending_sources',
     "OBS_INDEX_COVERAGE_VISIBLE"),
    ("SURVIVE independent fixture setup order", "tests/check_observer_queue.py",
     "        real_lock = module.file_lock\n        real_loader = module.load_observer_module",
     "        real_loader = module.load_observer_module\n        real_lock = module.file_lock",
     None),
    ("REACH ordinary lock falsely accused", "tests/check_observer_queue.py",
     "        real_lock = module.file_lock\n        real_loader = module.load_observer_module",
     "        real_lock = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('ordinary lock falsely accused'))\n        real_loader = module.load_observer_module",
     "OBS_RECEIPT_NO_OBSERVER_IO"),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    files = ("courier.py", "observer.py", "tests/check_observer_queue.py")
    before = {name: digest(root / name) for name in files}
    args.out.mkdir(parents=True, exist_ok=True)
    outcomes = []
    for number, (name, relative, old, new, expected) in enumerate(REGRESSIONS):
        with tempfile.TemporaryDirectory(prefix="coura-queue-mutant-") as folder:
            target = Path(folder)
            for path in files:
                (target / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / path, target / path)
            file = target / relative
            text = file.read_text(encoding="utf-8")
            if text.count(old) != 1:
                raise AssertionError("mutation anchor count: %s/%s" % (name, text.count(old)))
            file.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
            result = subprocess.run(
                [sys.executable, "-B", str(target / "tests/check_observer_queue.py"),
                 "--root", str(target)], cwd=target, capture_output=True, text=True, timeout=180)
            (args.out / ("%02d.log" % number)).write_text(
                result.stdout + result.stderr, encoding="utf-8")
            red = [line[4:].split(":", 1)[0] for line in result.stdout.splitlines()
                   if line.startswith("RED ")]
            first = red[0] if red else None
            passed = (result.returncode == 0 and not red) if expected is None else (
                result.returncode == 1 and first == expected)
            outcomes.append({"name": name, "expected_first": expected, "actual_first": first,
                             "returncode": result.returncode, "pass": passed})
            print(("PASS " if passed else "FAIL ") + name + " first=" + str(first), flush=True)
            if not passed:
                break
    after = {name: digest(root / name) for name in files}
    receipt = {"status": "PASS" if len(outcomes) == len(REGRESSIONS) and
               all(item["pass"] for item in outcomes) and before == after else "FAIL",
               "source_before": before, "source_after": after,
               "mutations": outcomes, "declared": len(REGRESSIONS)}
    (args.out / "result.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print("observer queue mutations: " + receipt["status"], flush=True)
    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
