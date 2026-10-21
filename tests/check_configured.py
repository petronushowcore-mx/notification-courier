"""Offline checks for configured transports. Mutations run against copied sources."""
import ast
import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import io
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

SUBJECT = Path(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--")
               else Path(__file__).resolve().parent.parent / "courier.py").resolve()
LEGACY_ID = "00000000-0000-0000-0000-000000000001"


def load():
    name = "coura_checked_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, SUBJECT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sink(module, calls, status="STORED", before=None):
    def deliver(target, payload, requested, attempt):
        if before is not None:
            before(target, payload, attempt)
        calls.append((target, payload, attempt))
        requested()
        return module.Receipt(status, "Fixture transport result", target.identity,
                              recipient_kind=target.client, attempt_id=attempt)
    return deliver


def configured(module, state, calls, modes=None, status="STORED", before=None):
    address = module.Address("worker", "agent-a")
    binding = module.Adapter(sink(module, calls, status, before),
                             frozenset({"notification", "data"} if modes is None else modes))
    return module.Courier(state, {"a": address}, adapters={"worker": binding})


def base_independent():
    code = '''import builtins, importlib.util, pathlib, sys, tempfile
real_import = builtins.__import__
def limited(name, globals=None, locals=None, fromlist=(), level=0):
    module = (globals or {}).get('__package__', '') if level else name
    if module.split('.')[0] not in sys.stdlib_module_names:
        raise ImportError('Standalone courier must use the standard library only')
    return real_import(name, globals, locals, fromlist, level)
builtins.__import__ = limited
spec = importlib.util.spec_from_file_location('standalone_coura', sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as state:
    target = m.Address('claude', '00000000-0000-0000-0000-000000000001')
    c = m.Courier(state, {'a': target})
    assert c.send('a', 'once', 'fixture').status == 'STORED'
    assert c.inbox.unread(target)[0][0]['line'] == 'Check once from fixture.'
'''
    done = subprocess.run([sys.executable, "-I", "-c", code, str(SUBJECT)],
                          capture_output=True, timeout=20)
    assert done.returncode == 0, done.stderr[-1000:]


def legacy_lock_bytes():
    m = load()
    captured = []
    original = m.file_lock
    @contextmanager
    def observe(path):
        captured.append(Path(path))
        with original(path):
            yield
    m.file_lock = observe
    with tempfile.TemporaryDirectory() as state:
        p = Path(state).resolve()
        row = {"client": "claude", "id": LEGACY_ID, "key": "legacy",
               "attempt": "preexisting", "status": "RESERVED"}
        (p / "journal.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        c = m.Courier(p, {"old": m.Address("claude", LEGACY_ID)})
        result = c.send_data("old", "legacy", b"structured")
        literal = b'["claude","00000000-0000-0000-0000-000000000001","legacy"]'
        expected = p / "send" / hashlib.sha256(literal).hexdigest()
        assert captured[0] == expected, (captured[0], expected)
        assert Path(str(expected) + ".lock").exists()
        assert result.status == "SKIPPED" and result.held_status == "RESERVED"
        assert c.send("old", "legacy", "fixture").status == "SKIPPED"
        assert c.send("old", "independent", "fixture").status == "STORED"


def data_bytes_exact():
    m = load()
    with tempfile.TemporaryDirectory() as state:
        calls = []
        c = configured(m, state, calls)
        body = bytes(range(256)) * 5
        result = c.send_data("a", "carrier", body)
        assert result.status == "STORED", result
        assert len(calls) == 1 and calls[0][1] == body
        assert result.recipient_kind == "worker" and result.attempt_id == calls[0][2]


def shared_entry_hold():
    m = load()
    for first_data in (False, True):
        with tempfile.TemporaryDirectory() as state:
            calls = []
            c = configured(m, state, calls, status="OUTCOME_UNKNOWN")
            first = (c.send_data("a", "held", b"data") if first_data else
                     c.send("a", "held", "fixture", "notice"))
            assert first.status == "OUTCOME_UNKNOWN"
            restarted = configured(m, state, calls, status="OUTCOME_UNKNOWN")
            second = (restarted.send("a", "held", "fixture") if first_data else
                      restarted.send_data("a", "held", b"data"))
            assert second.status == "SKIPPED" and second.held_status == "OUTCOME_UNKNOWN"
            assert len(calls) == 1
            assert restarted.send_data("a", "other", b"allowed").status == "OUTCOME_UNKNOWN"
            assert len(calls) == 2


def data_only_capability():
    m = load()
    with tempfile.TemporaryDirectory() as state:
        calls = []
        c = configured(m, state, calls)
        denied = c.send_data("a", "capability", b"untrusted", data_only=True)
        assert denied.status == "REFUSED" and calls == [], denied
        assert c.send_data("a", "capability", b"ordinary").status == "STORED"
        assert len(calls) == 1


def early_configured_hold():
    m = load()
    with tempfile.TemporaryDirectory() as state:
        observations = []
        def before(target, payload, attempt):
            row = m.held_outcome(m.records(Path(state) / "journal.jsonl"), target, "early")
            observations.append(None if row is None else (row["status"], row["attempt"]))
        calls = []
        c = configured(m, state, calls, before=before)
        result = c.send_data("a", "early", b"data")
        assert result.status == "STORED", result
        assert observations == [("RESERVED", calls[0][2])], observations


def command_full_address():
    m = load()
    for wrong in ("kind", "attempt", None):
        with tempfile.TemporaryDirectory() as state:
            requests = []
            def runner(command, timeout, wire):
                request = json.loads(wire)
                requests.append(request)
                reply = dict(status="STORED", recipient=dict(request["recipient"]),
                             attempt=request["attempt"])
                if wrong == "kind":
                    reply["recipient"]["kind"] = "another"
                if wrong == "attempt":
                    reply["attempt"] = "0" * 32
                return 0, json.dumps(reply).encode(), b""
            address = m.Address("worker", "agent-a")
            binding = m.Adapter(m.CommandAdapter(["fixture-command"], runner=runner),
                                frozenset({"data"}))
            c = m.Courier(state, {"a": address}, adapters={"worker": binding})
            result = c.send_data("a", "command", b"exact")
            assert requests[0]["recipient"] == {"kind": "worker", "id": "agent-a"}
            assert base64.b64decode(requests[0]["payload"]) == b"exact"
            assert result.status == ("STORED" if wrong is None else "OUTCOME_UNKNOWN"), result
            assert c.send_data("a", "command", b"again").status == "SKIPPED"


def legacy_notice_text():
    m = load()
    with tempfile.TemporaryDirectory() as state:
        target = m.Address("claude", LEGACY_ID)
        c = m.Courier(state, {"a": target})
        result = c.send("a", "review", "writer", "the body stays data")
        assert result.as_dict() == {"status": "STORED",
            "detail": "Letter stored; host wake and reading are separate",
            "recipient_id": LEGACY_ID, "queue_item_id": None,
            "held_status": None, "read": "UNMEASURED"}
        assert c.inbox.unread(target)[0][0]["line"] == "Check review from writer. the body stays data"
        rejected = False
        try:
            c.send("a", "too-long", "fixture", "x" * 1025)
        except m.Refused:
            rejected = True
        assert rejected


def data_bound():
    m = load()
    with tempfile.TemporaryDirectory() as state:
        calls = []
        c = configured(m, state, calls)
        assert c.send_data("a", "max", b"x" * 6144).status == "STORED"
        assert len(calls[0][1]) == 6144
        rejected = False
        try:
            c.send_data("a", "oversize", b"x" * 6145)
        except m.Refused:
            rejected = True
        assert rejected and len(calls) == 1


def inbox_roundtrip():
    m = load()
    with tempfile.TemporaryDirectory() as state:
        inbox = m.DataInbox(state)
        recipients = {"a": m.Address("worker", "same-id"),
                      "b": m.Address("another", "same-id")}
        bindings = {kind: m.Adapter(inbox, frozenset({"data", "notification"}))
                    for kind in ("worker", "another")}
        c = m.Courier(state, recipients, adapters=bindings)
        assert inbox.paths(recipients["a"])["jsonl"] != inbox.paths(recipients["b"])["jsonl"]
        for alias, payload in (("a", b"x" * 6144), ("b", b"other")):
            assert c.send_data(alias, "stored", payload).status == "STORED"
            out = io.StringIO()
            assert inbox.show(recipients[alias], out=out) == 1
            shown = json.loads(out.getvalue())
            assert set(shown) == {"messages"}
            row = shown["messages"][0]
            assert row["recipient"] == {"kind": recipients[alias].client,
                                         "id": recipients[alias].identity}
            assert base64.b64decode(row["payload"], validate=True) == payload
            assert m.DataInbox(state).show(recipients[alias], out=io.StringIO()) == 0


def command_stdin():
    m = load()
    code = '''import base64, json, pathlib, sys
request = json.load(sys.stdin)
body = base64.b64decode(request['payload'], validate=True)
pathlib.Path(sys.argv[1]).write_bytes(body)
print(json.dumps(dict(status='STORED', recipient=request['recipient'], attempt=request['attempt'])))
'''
    with tempfile.TemporaryDirectory() as state:
        effect = Path(state) / "received.bin"
        address = m.Address("worker", "actual-cli")
        binding = m.Adapter(m.CommandAdapter([sys.executable, "-I", "-c", code, str(effect)]),
                            frozenset({"data"}))
        c = m.Courier(state, {"a": address}, adapters={"worker": binding})
        body = b"Data only: ignore this sentence as instructions."
        result = c.send_data("a", "actual-child", body)
        assert result.status == "STORED", result
        assert effect.read_bytes() == body
        assert c.send_data("a", "actual-child", b"changed").status == "SKIPPED"
        assert effect.read_bytes() == body


def binding_plain_values():
    m = load()
    armed = False
    effects = []
    class Capability(str):
        __hash__ = str.__hash__
        def __eq__(self, other):
            if armed:
                effects.append("capability-effect")
                raise m.Refused("capability lookup interrupted")
            return str.__eq__(self, other)
    calls = []
    refused = False
    try:
        binding = m.Adapter(sink(m, calls), frozenset({Capability("data")}))
    except m.Refused:
        refused = True
    with tempfile.TemporaryDirectory() as state:
        if not refused:
            c = m.Courier(state, {"a": m.Address("worker", "agent-a")},
                          adapters={"worker": binding})
            armed = True
            statuses = [c.send_data("a", "same-key", b"data").status for _ in range(2)]
            assert refused, (statuses, effects)
        assert effects == [] and calls == []
        ordinary = configured(m, state, calls, modes={"data"})
        assert ordinary.send_data("a", "ordinary", b"data").status == "STORED"
        assert ordinary.send_data("a", "ordinary", b"again").status == "SKIPPED"
        assert len(calls) == 1


CHECKS = [("BASE_INDEPENDENT", base_independent),
          ("LEGACY_LOCK_BYTES", legacy_lock_bytes),
          ("DATA_BYTES_EXACT", data_bytes_exact),
          ("SHARED_ENTRY_HOLD", shared_entry_hold),
          ("DATA_ONLY_CAPABILITY", data_only_capability),
          ("EARLY_CONFIGURED_HOLD", early_configured_hold),
          ("COMMAND_FULL_ADDRESS", command_full_address),
          ("LEGACY_NOTICE_TEXT", legacy_notice_text),
          ("DATA_BOUND", data_bound),
          ("INBOX_ROUNDTRIP", inbox_roundtrip),
          ("COMMAND_STDIN", command_stdin),
          ("BINDING_PLAIN_VALUES", binding_plain_values)]
EXPECTED_TEETH = 12


def check_only():
    assert len(CHECKS) == EXPECTED_TEETH
    passed = 0
    failed = False
    for name, check in CHECKS:
        try:
            check()
        except Exception as error:
            print("FAIL %s: %s: %s" % (name, type(error).__name__, str(error)), flush=True)
            failed = True
        else:
            passed += 1
    print("teeth: %s/%s" % (passed, EXPECTED_TEETH), flush=True)
    return 1 if failed else 0


REGRESSIONS = [{"id": "base_independent", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "    def send(self, recipient, key, sender, note=\"\", delivery=None, caller=None):\n        target = self.resolve(recipient)", "new": "    def send(self, recipient, key, sender, note=\"\", delivery=None, caller=None):\n        import cryptography\n        target = self.resolve(recipient)"}], "first_red": "BASE_INDEPENDENT"}, {"id": "legacy_lock_bytes", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "[target.client, target.identity, key], ensure_ascii=True,", "new": "[target.client, target.identity, key, \"new\"], ensure_ascii=True,"}], "first_red": "LEGACY_LOCK_BYTES"}, {"id": "data_bytes_exact", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "payload = line.encode(\"utf-8\") if carrier is None else carrier", "new": "payload = line.encode(\"utf-8\") if carrier is None else carrier[:1024]"}], "first_red": "DATA_BYTES_EXACT"}, {"id": "shared_entry_hold", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "return self._dispatch(target, key, None, None, None, carrier, data_only)", "new": "return self._dispatch(target, key + (\":data\" if target.client not in (\"claude\", \"codex\") else \"\"), None, None, None, carrier, data_only)"}], "first_red": "SHARED_ENTRY_HOLD"}, {"id": "data_only_capability", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "if data_only and \"data_only\" not in binding.capabilities:", "new": "if False:"}], "first_red": "DATA_ONLY_CAPABILITY"}, {"id": "early_configured_hold", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "                # No supplied binding lookup or call precedes the durable hold.\n                append_record(journal, dict(base, ts=now(), status=\"RESERVED\"))", "new": "                # Regression: supplied code may enter without a hold.\n                pass"}], "first_red": "EARLY_CONFIGURED_HOLD"}, {"id": "command_full_address", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "reply[\"recipient\"] != recipient or reply[\"attempt\"] != attempt", "new": "reply[\"recipient\"].get(\"id\") != recipient[\"id\"] or reply[\"attempt\"] != attempt"}], "first_red": "COMMAND_FULL_ADDRESS"}, {"id": "legacy_notice_text", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "line = (\"Check %s from %s.\" % (key, sender)", "new": "line = ((\"Handle %s from %s.\" if note else \"Check %s from %s.\") % (key, sender)"}], "first_red": "LEGACY_NOTICE_TEXT"}, {"id": "data_bound", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "MAX_DATA = 6144", "new": "MAX_DATA = 6145"}], "first_red": "DATA_BOUND"}, {"id": "inbox_roundtrip", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "key = hashlib.sha256(json.dumps([target.client, target.identity],\n            ensure_ascii=True, separators=(\",\", \":\")).encode(\"utf-8\")).hexdigest()", "new": "key = target.identity"}], "first_red": "INBOX_ROUNDTRIP"}, {"id": "command_stdin", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "            source.seek(0)", "new": "            pass  # stdin left at EOF"}], "first_red": "COMMAND_STDIN"}, {"id": "binding_plain_values", "kind": "REACH", "mutations": [{"path": "courier.py", "old": "        if (not callable(self.transport) or type(self.capabilities) is not frozenset or\n                any(type(item) is not str for item in self.capabilities) or\n", "new": "        if (not callable(self.transport) or type(self.capabilities) is not frozenset or\n"}], "first_red": "BINDING_PLAIN_VALUES"}, {"id": "independent_fixture_order", "kind": "SURVIVE", "mutations": [{"path": "check_configured.py", "old": "    captured = []\n    original = m.file_lock", "new": "    original = m.file_lock\n    captured = []"}], "first_red": None}, {"id": "fixture_capture_overfires", "kind": "REACH", "mutations": [{"path": "check_configured.py", "old": "    captured = []\n    original = m.file_lock", "new": "    captured = [Path(\"foreign-lock\")]\n    original = m.file_lock"}], "first_red": "LEGACY_LOCK_BYTES"}]


def outcome(code, stdout):
    lines = stdout.decode("utf-8", errors="strict").split("\n")
    names = {name for name, _ in CHECKS}
    failures = [re.fullmatch(r"FAIL ([A-Z_]+): .+", line)
                for line in lines if line.startswith("FAIL ")]
    if any(match is None or match[1] not in names for match in failures):
        return None
    summaries = [re.fullmatch(r"teeth: ([0-9]+)/([0-9]+)", line)
                 for line in lines if line.startswith("teeth:")]
    if len(summaries) != 1 or summaries[0] is None:
        return None
    passed, total = map(int, summaries[0].groups())
    reds = [match[1] for match in failures]
    if (total != 12 or len(set(reds)) != len(reds) or
            passed + len(reds) != total or code != (1 if reds else 0)):
        return None
    return passed, reds[0] if reds else None


def mutations():
    controls = [outcome(0, b"teeth: 12/12\n") == (12, None),
                outcome(1, b"teeth: 12/12\n") is None,
                outcome(0, b"teeth: 11/12\n") is None,
                outcome(1, b"FAIL DATA_BOUND: AssertionError: fixture\nteeth: 12/12\n") is None,
                outcome(0, b"") is None]
    if not all(controls):
        raise AssertionError("RESULT_READER_CONTROL")
    if len(REGRESSIONS) != 14:
        raise AssertionError("REGRESSION_POPULATION")
    source = SUBJECT.read_text(encoding="utf-8")
    tests = Path(__file__).read_text(encoding="utf-8")
    for case in REGRESSIONS:
        with tempfile.TemporaryDirectory(prefix="coura-regression-") as state:
            tree = Path(state)
            (tree / "courier.py").write_text(source, encoding="utf-8", newline="\n")
            (tree / "check_configured.py").write_text(tests, encoding="utf-8", newline="\n")
            for mutation in case["mutations"]:
                path = tree / mutation["path"]
                text = path.read_text(encoding="utf-8")
                if text.count(mutation["old"]) != 1:
                    raise AssertionError("MUTATION_ANCHOR:" + case["id"])
                changed = text.replace(mutation["old"], mutation["new"], 1)
                ast.parse(changed, filename=str(path))
                path.write_text(changed, encoding="utf-8", newline="\n")
            done = subprocess.run([sys.executable, "-B", "-X", "utf8",
                    str(tree / "check_configured.py"), str(tree / "courier.py")],
                    capture_output=True, timeout=120, cwd=tree)
            if len(done.stdout) > 65536 or len(done.stderr) > 20000:
                raise AssertionError("MUTATION_OUTPUT_BOUND:" + case["id"])
            observed = outcome(done.returncode, done.stdout)
            if (observed is None or observed[1] != case["first_red"] or
                    (observed[0] != 12 if case["kind"] == "SURVIVE"
                     else not 0 < observed[0] < 12)):
                raise AssertionError((case["id"], observed, done.stdout, done.stderr))
            print("regression %s: first=%s" % (case["id"], observed[1]), flush=True)
    print("mutations: 14/14; result-reader controls: 5/5", flush=True)
    return 0


def main():
    result = check_only()
    if result or "--mutations" not in sys.argv:
        return result
    return mutations()


if __name__ == "__main__":
    raise SystemExit(main())
