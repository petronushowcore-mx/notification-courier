"""Regressions for observer misconfiguration, producer identity, receipt latency, and bounded recovery.

Run against the tree that contains this file. Pass --expect-fail ROOT to load
courier.py and observer.py from ROOT and require each test to fail there.
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


HERE = Path(__file__).resolve()


def load_pair(root):
    """Load courier.py and observer.py from root and bind them together."""
    import importlib.util
    root = Path(root)
    token = uuid.uuid4().hex
    cname = "coura_pair_c_" + token
    oname = "coura_pair_o_" + token
    cspec = importlib.util.spec_from_file_location(cname, root / "courier.py")
    courier = importlib.util.module_from_spec(cspec)
    sys.modules[cname] = courier
    cspec.loader.exec_module(courier)
    ospec = importlib.util.spec_from_file_location(oname, root / "observer.py")
    observer = importlib.util.module_from_spec(ospec)
    sys.modules[oname] = observer
    ospec.loader.exec_module(observer)
    observer.bind(courier)
    return courier, observer


def write_config(directory, state, observer):
    body = {
        "state_dir": str(state),
        "transports": {"worker": {"type": "inbox"}},
        "recipients": {
            "alpha": {"client": "worker", "id": "alpha"},
            "beta": {"client": "worker", "id": "beta"},
            "agenda": {"client": "worker", "id": "agenda"},
        },
    }
    if observer is not None:
        body["observer"] = observer
    path = Path(directory) / "coura.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def invoke(root, args, timeout=30):
    return subprocess.run(
        [sys.executable, "-B", str(Path(root) / "courier.py"), *args],
        capture_output=True, text=True, timeout=timeout)


def inbox_lines(state, client, identity):
    key = hashlib.sha256(json.dumps(
        [client, identity], ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    path = Path(state) / "data-inbox" / (key + ".jsonl")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def payloads(state, client, identity):
    found = []
    for row in inbox_lines(state, client, identity):
        found.append(base64.b64decode(row["payload"], validate=True))
    return found


def journal_rows(state):
    path = Path(state) / "journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def child_producer(root, config, slot, coord):
    coord = Path(coord)
    try:
        courier, observer = load_pair(root)
        real_replace = observer.os.replace

        def paused_replace(src, dst):
            if Path(dst).name == "producer-id":
                (coord / ("enter-" + slot)).write_text("1", encoding="utf-8")
                deadline = time.monotonic() + 20
                while not (coord / "release").exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("release was not signaled")
                    time.sleep(0.01)
            return real_replace(src, dst)

        observer.os.replace = paused_replace
        real_lock = courier.file_lock

        @contextmanager
        def wrapped_lock(path):
            marker = "producer-id.gate" in str(path)
            if marker:
                (coord / ("waiting-" + slot)).write_text("1", encoding="utf-8")
            with real_lock(path):
                if marker:
                    (coord / ("holding-" + slot)).write_text("1", encoding="utf-8")
                yield

        courier.file_lock = wrapped_lock
        instance = courier.Courier.from_config(config)
        (coord / ("ready-" + slot)).write_text("1", encoding="utf-8")
        deadline = time.monotonic() + 20
        while not (coord / "go").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("go was not signaled")
            time.sleep(0.01)
        value = observer.producer_id(instance)
        (coord / ("result-" + slot)).write_text(value, encoding="utf-8")
    except Exception as error:
        (coord / ("error-" + slot)).write_text(repr(error), encoding="utf-8")
        raise


def child_hold(root, state, ready, release):
    courier, _observer = load_pair(root)
    path = Path(state) / "observer" / "outbox.jsonl"
    with courier.file_lock(path):
        Path(ready).write_text("1", encoding="utf-8")
        while not Path(release).exists():
            time.sleep(0.02)


def test_f1_misconfigured_observer_does_not_block_delivery(root):
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        state = directory / "state"
        config = write_config(directory, state, {
            "destination": "gone",
            "include_body": False,
            "producer_id": "coura-local",
        })
        note = "exact-bytes-token"
        expected = b"Check f1-key from alpha. " + note.encode("ascii")
        done = invoke(root, [
            "--config", str(config), "send", "beta", "--key", "f1-key",
            "--sender", "alpha", "--note", note,
        ])
        found = payloads(state, "worker", "beta")
        if done.returncode != 0 or not done.stdout.strip():
            raise AssertionError(
                "misconfigured observer refused delivery rc=%s stdout=%s stderr=%s payloads=%s"
                % (done.returncode, done.stdout.strip(), done.stderr.strip(), len(found)))
        receipt = json.loads(done.stdout)
        observation = receipt.get("observation") or {}
        if receipt.get("status") != "STORED" or observation.get("status") != "CAPTURE_FAILED":
            raise AssertionError("delivery or observation mismatch %s" % done.stdout.strip())
        detail = observation.get("detail") or ""
        if "Observer destination must be a configured recipient" not in detail:
            raise AssertionError("observation detail missing destination error: %s" % detail)
        if found != [expected]:
            raise AssertionError("payload mismatch %s" % found)
        missing = invoke(root, [
            "--config", str(config), "send", "nobody", "--key", "f1-missing",
            "--sender", "alpha", "--note", note,
        ])
        if missing.returncode == 0 or "Unknown address" not in missing.stderr:
            raise AssertionError("unknown recipient was not refused: %s" % missing.stderr.strip())
        if payloads(state, "worker", "beta") != [expected]:
            raise AssertionError("unknown recipient changed the inbox")
        sync = invoke(root, ["--config", str(config), "observer-sync", "--batch", "1"])
        if sync.returncode == 0 or "Observer destination must be a configured recipient" not in sync.stderr:
            raise AssertionError("observer-sync did not refuse: %s" % sync.stderr.strip())
        if payloads(state, "worker", "beta") != [expected]:
            raise AssertionError("observer-sync changed the inbox")


def test_f2_producer_id_is_fixed_once_when_two_processes_race(root):
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        state = directory / "explicit-state"
        config = write_config(directory, state, {
            "destination": "agenda",
            "include_body": False,
            "producer_id": "coura-explicit",
        })
        courier, observer = load_pair(root)
        instance = courier.Courier.from_config(config)
        assert observer.producer_id(instance) == "coura-explicit"
        assert not (state / "observer" / "producer-id").exists()

    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        state = directory / "state"
        coord = directory / "coord"
        coord.mkdir()
        config = write_config(directory, state, {
            "destination": "agenda",
            "include_body": False,
        })
        procs = []
        try:
            for slot in ("a", "b"):
                procs.append(subprocess.Popen(
                    [sys.executable, "-B", str(HERE), "--child-producer",
                     str(root), str(config), slot, str(coord)]))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if (coord / "ready-a").exists() and (coord / "ready-b").exists():
                    break
                time.sleep(0.01)
            else:
                errors = []
                for slot in ("a", "b"):
                    err = coord / ("error-" + slot)
                    if err.exists():
                        errors.append(err.read_text(encoding="utf-8"))
                raise AssertionError("producer children did not start %s" % errors)
            (coord / "go").write_text("1", encoding="utf-8")
            deadline = time.monotonic() + 10
            released = False
            while time.monotonic() < deadline:
                enters = sum((coord / ("enter-" + slot)).exists() for slot in ("a", "b"))
                waiting = sum((coord / ("waiting-" + slot)).exists() for slot in ("a", "b"))
                holding = sum((coord / ("holding-" + slot)).exists() for slot in ("a", "b"))
                if enters >= 2 or (holding >= 1 and enters >= 1 and waiting >= 2):
                    (coord / "release").write_text("1", encoding="utf-8")
                    released = True
                    break
                time.sleep(0.01)
            if not released:
                (coord / "release").write_text("1", encoding="utf-8")
                raise AssertionError(
                    "producer race did not reach a controlled interleaving enters=%s waiting=%s holding=%s"
                    % (enters, waiting, holding))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if (coord / "result-a").exists() and (coord / "result-b").exists():
                    break
                time.sleep(0.01)
            else:
                errors = []
                for slot in ("a", "b"):
                    err = coord / ("error-" + slot)
                    if err.exists():
                        errors.append(err.read_text(encoding="utf-8"))
                raise AssertionError("producer results missing %s" % errors)
            first = (coord / "result-a").read_text(encoding="utf-8").strip()
            second = (coord / "result-b").read_text(encoding="utf-8").strip()
            stored = (state / "observer" / "producer-id").read_text(encoding="utf-8").strip()
            if not (first == second == stored):
                raise AssertionError(
                    "producer ids diverged a=%s b=%s stored=%s" % (first, second, stored))
        finally:
            (coord / "go").write_text("1", encoding="utf-8")
            (coord / "release").write_text("1", encoding="utf-8")
            for proc in procs:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def test_f3_outbox_lock_does_not_hold_the_receipt(root):
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        state = directory / "state"
        ready = directory / "ready"
        release = directory / "release"
        config = write_config(directory, state, {
            "destination": "agenda",
            "include_body": False,
            "producer_id": "coura-local",
        })
        note = "f3-token"
        expected = b"Check f3-key from alpha. " + note.encode("ascii")
        holder = subprocess.Popen(
            [sys.executable, "-B", str(HERE), "--child-hold",
             str(root), str(state), str(ready), str(release)])
        try:
            deadline = time.monotonic() + 10
            while not ready.exists():
                if holder.poll() is not None:
                    raise AssertionError("lock holder exited early rc=%s" % holder.returncode)
                if time.monotonic() >= deadline:
                    raise AssertionError("lock holder did not acquire the outbox lock")
                time.sleep(0.01)
            try:
                done = invoke(root, [
                    "--config", str(config), "send", "beta", "--key", "f3-key",
                    "--sender", "alpha", "--note", note,
                ], timeout=5)
            except subprocess.TimeoutExpired:
                raise AssertionError("send blocked on observer lock")
            if done.returncode != 0:
                raise AssertionError("send failed rc=%s stderr=%s" % (done.returncode, done.stderr.strip()))
            receipt = json.loads(done.stdout)
            observation = receipt.get("observation") or {}
            if receipt.get("status") != "STORED":
                raise AssertionError("receipt was not stored: %s" % done.stdout.strip())
            if observation.get("status") != "PENDING" or observation.get("deferred") is not True:
                raise AssertionError("receipt waited on observer capture: %s" % done.stdout.strip())
            if payloads(state, "worker", "beta") != [expected]:
                raise AssertionError("payload mismatch %s" % payloads(state, "worker", "beta"))
            stored = [row for row in journal_rows(state) if row.get("status") == "STORED"]
            if len(stored) != 1:
                raise AssertionError("participant journal STORED count %s" % len(stored))
        finally:
            release.write_text("1", encoding="utf-8")
            try:
                holder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=5)
        synced = invoke(root, ["--config", str(config), "observer-sync", "--batch", "1"])
        if synced.returncode != 0:
            raise AssertionError("observer-sync failed %s" % synced.stderr.strip())
        body = json.loads(synced.stdout)
        if body.get("recovered", 0) < 1:
            raise AssertionError("deferred fact was not recovered %s" % synced.stdout.strip())
        if payloads(state, "worker", "beta") != [expected]:
            raise AssertionError("recovery resent the participant payload")


def test_f4_recovery_read_volume_stays_bounded(root):
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        state = directory / "state"
        config = write_config(directory, state, {
            "destination": "agenda",
            "include_body": False,
            "producer_id": "coura-local",
        })
        courier, observer = load_pair(root)
        instance = courier.Courier.from_config(config)
        for index in range(128):
            receipt = instance.send("beta", "m-%s" % index, "alpha", "n")
            if receipt.status != "STORED":
                raise AssertionError("setup send %s status %s" % (index, receipt.status))
        observer_dir = state / "observer"
        for name in ("outbox.jsonl", "ids.txt", "pending.jsonl", "dispatch.jsonl",
                     "cursor.json", "counts.jsonl", "delivery.jsonl"):
            path = observer_dir / name
            if path.exists():
                path.unlink()
        journal = state / "journal.jsonl"
        journal_bytes = journal.read_bytes()
        real = observer.read_spans
        stats = {"passes": 0, "rows": 0}

        def counting(path):
            target = str(path).endswith("outbox.jsonl")
            if target:
                stats["passes"] += 1
            for item in real(path):
                if target:
                    stats["rows"] += 1
                yield item

        observer.read_spans = counting
        try:
            result = observer.sync(instance, 1)
        finally:
            observer.read_spans = real
        recovered = result.get("recovered")
        attempted = result.get("attempted")
        if not (recovered <= 32 and stats["passes"] <= 4 and stats["rows"] <= 400 and attempted == 1):
            raise AssertionError(
                "recovery unbounded recovered=%s passes=%s rows=%s attempted=%s"
                % (recovered, stats["passes"], stats["rows"], attempted))
        print("F4 recovered=%s passes=%s rows=%s attempted=%s" % (
            recovered, stats["passes"], stats["rows"], attempted))
        outbox = observer_dir / "outbox.jsonl"
        found = observer.known_ids(instance)
        for _step in range(80):
            if len(found) >= 128:
                break
            before = outbox.stat().st_size if outbox.exists() else 0
            observer.sync(instance, 1)
            after = outbox.stat().st_size if outbox.exists() else 0
            if after < before:
                raise AssertionError("outbox shrank from %s to %s" % (before, after))
            if journal.read_bytes() != journal_bytes:
                raise AssertionError("recovery rewrote the participant journal")
            found = observer.known_ids(instance)
        if len(found) != 128:
            raise AssertionError("captured %s event ids, want 128" % len(found))
        if journal.read_bytes() != journal_bytes:
            raise AssertionError("recovery rewrote the participant journal")
        chosen = None
        for line in journal_bytes.splitlines():
            row = json.loads(line)
            if row.get("status") == "STORED":
                chosen = line
                break
        if not chosen or len(chosen) < 8:
            raise AssertionError("no stored journal line to truncate")
        prefix = chosen[:8]
        with journal.open("ab") as stream:
            stream.write(prefix)
            stream.flush()
            os.fsync(stream.fileno())
        snapshot = journal.read_bytes()
        observer.sync(instance, 1)
        if journal.read_bytes() != snapshot or not snapshot.endswith(prefix):
            raise AssertionError("partial tail was consumed or rejected")
        with journal.open("ab") as stream:
            stream.write(chosen[len(prefix):] + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        before_ids = observer.known_ids(instance)
        observer.sync(instance, 1)
        added = observer.known_ids(instance) - before_ids
        if not added:
            raise AssertionError("completed tail was not captured")


def test_failed_index_update_does_not_consume_the_event_id(root):
    from check_observer_queue import audit_append_recovers_after_index_failure
    audit_append_recovers_after_index_failure(root)


def test_inspect_returns_the_newest_event_outside_the_old_window(root):
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        state = directory / "state"
        config = write_config(directory, state, {
            "destination": "agenda",
            "include_body": False,
            "producer_id": "coura-local",
        })
        courier, observer = load_pair(root)
        instance = courier.Courier.from_config(config)
        pending = state / "observer" / "outbox.jsonl"
        pending.parent.mkdir(parents=True)
        lines = []
        for index in range(4096):
            lines.append(json.dumps({
                "kind": "capture", "event_id": "old-%04d" % index,
                "event": {"delivery": {"key": "old-%04d" % index}},
            }, separators=(",", ":")))
        lines.append(json.dumps({
            "kind": "capture", "event_id": "newest-event",
            "event": {"delivery": {"key": "newest-event"}},
        }, separators=(",", ":")))
        pending.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for _step in range(132):
            inspected = observer.inspect_events(instance, 1, "all")
            if inspected["index_complete"]:
                break
        if not inspected["index_complete"]:
            raise AssertionError("observer index did not catch up")
        found = [item.get("event_id") for item in inspected.get("events", [])]
        if found != ["newest-event"]:
            raise AssertionError(
                "inspect missed the newest event: %s truncated=%s"
                % (found[:4], inspected.get("truncated")))


TESTS = (
    test_f1_misconfigured_observer_does_not_block_delivery,
    test_f2_producer_id_is_fixed_once_when_two_processes_race,
    test_f3_outbox_lock_does_not_hold_the_receipt,
    test_f4_recovery_read_volume_stays_bounded,
    test_failed_index_update_does_not_consume_the_event_id,
    test_inspect_returns_the_newest_event_outside_the_old_window,
)


def main(argv):
    if len(argv) >= 2 and argv[1] == "--child-producer":
        child_producer(*argv[2:4 + 2])
        return 0
    if len(argv) >= 2 and argv[1] == "--child-hold":
        child_hold(*argv[2:6])
        return 0
    if len(argv) >= 2 and argv[1] == "--expect-fail":
        if len(argv) != 3:
            print("usage: check_v5_regressions.py --expect-fail ROOT", file=sys.stderr)
            return 2
        root = Path(argv[2]).resolve()
        failed = []
        for test in TESTS:
            try:
                test(root)
            except (AssertionError, subprocess.TimeoutExpired) as error:
                print("FAIL %s: %s" % (test.__name__, error))
                failed.append(test.__name__)
            except Exception as error:
                print("FAIL %s: %s: %s" % (test.__name__, type(error).__name__, error))
                failed.append(test.__name__)
            else:
                print("UNEXPECTED PASS %s" % test.__name__)
        if len(failed) != len(TESTS):
            return 1
        print("expect-fail: %s/%s failed" % (len(failed), len(TESTS)))
        return 0
    root = HERE.parent.parent
    for test in TESTS:
        test(root)
        print("PASS %s" % test.__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
