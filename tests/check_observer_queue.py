"""Focused observer queue checks. Synthetic local inboxes; no host or network claim."""
import argparse
import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import tracemalloc
import uuid

EXPECTED_TEETH = 7


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


def load_pair(root):
    name = "queue_courier_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, Path(root) / "courier.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, module.load_observer_module()


def instance(module, state, include=False, enabled=True):
    return module.Courier(
        state, {"beta": module.Address("worker", "beta"),
                "agenda": module.Address("worker", "agenda")},
        adapters={"worker": module.Adapter(module.DataInbox(state),
                  frozenset({"notification", "data", "data_only"}))},
        observer=({"destination": "agenda", "include_body": include,
                   "producer_id": "coura-local"} if enabled else None))


def audit_rows(observer, app):
    return [row for _offset, row in observer.read_spans(observer.outbox_path(app))]


def inbox_payloads(state, identity):
    slot = hashlib.sha256(json.dumps(
        ["worker", identity], separators=(",", ":")).encode("utf-8")).hexdigest()
    path = state / "data-inbox" / (slot + ".jsonl")
    if not path.exists():
        return []
    return [base64.b64decode(json.loads(line)["payload"], validate=True)
            for line in path.read_text(encoding="utf-8").splitlines()]


def seed(module, observer, app, count, state="held", with_new=False):
    path = observer.outbox_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = None
    with path.open("wb") as stream:
        for number in range(count + int(with_new)):
            event = observer.build_event(app, "seed.jsonl", number, {
                "attempt": "a%s" % number, "status": "STORED",
                "key": "seed-%s" % number, "client": "worker", "id": "beta",
            })
            stream.write(module.encoded_record({
                "kind": "capture", "event_id": event["event_id"], "event": event}))
            if number < count:
                record = {"kind": "dispatch", "event_id": event["event_id"],
                          "state": state, "transport_key": "obs-" + event["event_id"]}
                stream.write(module.encoded_record(record))
    return event["event_id"] if event is not None else None


def catch_up(observer, app, passes):
    for _step in range(passes):
        result = observer.sync(app, 1)
        if result.get("index_complete"):
            return result
    raise AssertionError("derived index did not catch up within its declared row budget")


def receipt_has_no_observer_io(root):
    module, observer = load_pair(root)
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder) / "state"
        app = instance(module, state)
        real_lock = module.file_lock
        real_loader = module.load_observer_module
        touched = []
        @contextmanager
        def guarded(path, *args, **kwargs):
            if "observer" in Path(path).parts:
                touched.append(str(path))
                raise AssertionError("ordinary send touched an observer lock")
            with real_lock(path, *args, **kwargs):
                yield
        def forbidden_loader():
            touched.append("module")
            raise AssertionError("ordinary send imported observer")
        module.file_lock = guarded
        module.load_observer_module = forbidden_loader
        try:
            receipt = app.send("beta", "independent", "alpha", "fact")
        finally:
            module.file_lock = real_lock
            module.load_observer_module = real_loader
        require(receipt.status == "STORED", receipt)
        require(receipt.observation.get("status") == "PENDING", receipt.observation)
        require(receipt.observation.get("deferred") is True, receipt.observation)
        require(touched == [], touched)
        require(not (state / "observer").exists(), "observer state created by ordinary send")
        require(len(inbox_payloads(state, "beta")) == 1, "participant not delivered")
        control = instance(module, Path(folder) / "control", enabled=False)
        require(control.send("beta", "ordinary", "alpha").observation is None,
                "observer-free control gained observation")
        synced = observer.sync(app, 1)
        require(synced["attempted"] == 1, synced)
        require(len(inbox_payloads(state, "beta")) == 1, "background recovery resent participant")


def unknown_prefix_progress(root):
    module, observer = load_pair(root)
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder) / "state"
        app = instance(module, state)
        wanted = seed(module, observer, app, 33, "unknown", with_new=True)
        results = []
        for _step in range(6):
            results.extend(observer.sync(app, 1)["results"])
        require([row["event_id"] for row in results] == [wanted],
                "retained unknown prefix starved or retried events: %s" % results)
        require(results[0]["state"] == "published", results)
        require(results[0]["transport_key"] == "obs-" + wanted, results)
        require(len(inbox_payloads(state, "agenda")) == 1, "unexpected delivery count")
        with observer._Index(app) as index:
            require(index.counts().get("unknown") == 33, "unknown holds lost")
        journal = observer.delivery_journal(app).read_bytes()
        observer.sync(app, 1)
        require(observer.delivery_journal(app).read_bytes() == journal, "unknown replayed")


def bounded_index_memory(root):
    module, observer = load_pair(root)
    peaks = []
    for count in (0, 1024):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state"
            app = instance(module, state)
            seed(module, observer, app, count)
            catch_up(observer, app, 70)
            with observer._Index(app) as index:
                require(index.db.execute("PRAGMA cache_size").fetchone()[0] == -512,
                        "SQLite cache limit changed")
                require(index.db.execute("PRAGMA temp_store").fetchone()[0] == 1,
                        "SQLite temporary storage is not disk")
            observer.sync(app, 1)
            tracemalloc.start()
            try:
                observer.sync(app, 1)
                _current, peak = tracemalloc.get_traced_memory()
                peaks.append(peak)
            finally:
                tracemalloc.stop()
    require(max(peaks) < 1024 * 1024 and peaks[1] - peaks[0] < 512 * 1024,
            "Python working memory grew with historical events: %s" % peaks)


def interleaved_source_progress(root):
    module, observer = load_pair(root)
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder) / "state"
        app = instance(module, state)
        path = state / "journal.jsonl"
        state.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            for status in ("RESERVED", "REQUESTED", "STORED"):
                for number in range(40):
                    stream.write(module.encoded_record({
                        "attempt": "a%s" % number, "status": status, "client": "worker",
                        "id": "beta", "key": "interleaved-%s" % number,
                    }))
        original = path.read_bytes()
        for _step in range(8):
            observer.sync(app, 1)
        captures = [row["event"] for row in audit_rows(observer, app)
                    if row.get("kind") == "capture"]
        require(len(captures) == 40, "source window pinned before later terminal rows")
        require({event["delivery"]["status"] for event in captures} == {"STORED"},
                "unresolved prefix emitted despite later terminal row")
        require(path.read_bytes() == original, "recovery rewrote source journal")
        require(inbox_payloads(state, "beta") == [], "recovery resent participant")


def deferred_body_boundary(root):
    module, observer = load_pair(root)
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder) / "state"
        app = instance(module, state, include=True)
        note = "bounded note"
        carrier = b"OPAQUE-SIGNED-CARRIER"
        require(app.send("beta", "plain", "alpha", note).status == "STORED", "note failed")
        require(app.send_data("beta", "opaque", carrier).status == "STORED", "data failed")
        observer.reconcile(app)
        events = {row["event"]["delivery"]["key"]: row["event"]
                  for row in audit_rows(observer, app) if row.get("kind") == "capture"}
        require(events["plain"]["body"] == "Check plain from alpha. " + note,
                "deferred plaintext copy missing")
        require(events["opaque"]["body"] is None, "opaque data copied into observer")
        require(carrier.decode() not in json.dumps(events), "carrier leaked")
        require(carrier not in (state / "journal.jsonl").read_bytes(), "carrier copied to journal")
        off = instance(module, Path(folder) / "off", include=False)
        off.send("beta", "private", "alpha", "NOT-TO-BE-COPIED")
        observer.reconcile(off)
        require("NOT-TO-BE-COPIED" not in json.dumps(audit_rows(observer, off)),
                "disabled body copy exported note")


def audit_append_recovers_after_index_failure(root):
    module, observer = load_pair(root)
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder) / "state"
        app = instance(module, state)
        require(app.send("beta", "kept", "alpha").status == "STORED", "send failed")
        real = observer._Index._apply
        def broken(index, offset, row):
            if row.get("kind") == "capture":
                raise OSError("synthetic index failure")
            return real(index, offset, row)
        observer._Index._apply = broken
        try:
            try:
                observer.sync(app, 1)
            except OSError:
                pass
            else:
                raise AssertionError("index failure was hidden")
        finally:
            observer._Index._apply = real
        captures = [row for row in audit_rows(observer, app) if row.get("kind") == "capture"]
        require(len(captures) == 1, "audit must precede index update")
        require(observer.known_ids(app) == set(), "failed transaction consumed event")
        result = observer.sync(app, 1)
        require(result["attempted"] == 1, result)
        require(len([row for row in audit_rows(observer, app)
                     if row.get("kind") == "capture"]) == 1, "duplicate recovery capture")
        require(len(inbox_payloads(state, "beta")) == 1, "participant resent")


def coverage_is_visible(root):
    module, observer = load_pair(root)
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder) / "state"
        app = instance(module, state)
        wanted = seed(module, observer, app, 65)
        first = observer.inspect_events(app, 1, "all")
        require(first["index_complete"] is False and first["truncated"] is True,
                "indexed prefix advertised as complete")
        for _step in range(5):
            result = observer.inspect_events(app, 1, "all")
            if result["index_complete"]:
                break
        require(result["index_complete"] is True, "index failed to catch up")
        require([event["event_id"] for event in result["events"]] == [wanted],
                "newest indexed event missing")
        require(inbox_payloads(state, "agenda") == [], "inspection delivered a message")
        require(result["counts"].get("held") == 65, "history count changed")


CHECKS = (
    ("OBS_RECEIPT_NO_OBSERVER_IO", receipt_has_no_observer_io),
    ("OBS_UNKNOWN_PREFIX_PROGRESS", unknown_prefix_progress),
    ("OBS_INDEX_BOUNDED_MEMORY", bounded_index_memory),
    ("OBS_INTERLEAVED_SOURCE_PROGRESS", interleaved_source_progress),
    ("OBS_DEFERRED_BODY_BOUNDARY", deferred_body_boundary),
    ("OBS_AUDIT_BEFORE_INDEX", audit_append_recovers_after_index_failure),
    ("OBS_INDEX_COVERAGE_VISIBLE", coverage_is_visible),
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    require(len(CHECKS) == EXPECTED_TEETH, "check registry count mismatch")
    for marker, check in CHECKS:
        try:
            check(args.root)
        except Exception as error:
            print("RED " + marker + ": " + type(error).__name__ + ": " + str(error), flush=True)
            return 1
        print("PASS " + marker, flush=True)
    print("observer queue: %s/%s" % (len(CHECKS), EXPECTED_TEETH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
