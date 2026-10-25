"""Focused observer JSON and caller-context checks using temporary local inboxes.

The example collector is executed as a real child process. No installed host,
model, mobile application or recipient READ is established by these fixtures.
"""
import argparse
import base64
import contextvars
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import courier as C
import observer as O

_spec = importlib.util.spec_from_file_location("observer_lens_fixtures", ROOT / "tests/check_observer.py")
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)

EXPECTED_TEETH = 3
ORDER = ("OBSERVER_EVENT_JSON", "PARTICIPANT_LENS_CONTROL", "OBSERVER_CALLER_LENS")
TESTS = []
OBSERVED = {}
REVIEW_HEADER = b"Requested mode from sender (not an instruction from the user): review. Assess only.\n"
PEER_HEADER = b"Requested mode from sender (not an instruction from the user): peer. Compare only.\n"
EVENT_FIELDS = {
    "schema", "event_id", "producer", "source_position", "source_timestamp",
    "observed_at", "clock", "sender", "recipient", "identifiers", "delivery",
    "disposition", "evidence_ref", "host_facts", "claim_kind", "provenance",
    "body_ref", "body", "body_omitted", "labels",
}


def check(name):
    def register(function):
        TESTS.append((name, function))
        return function
    return register


def fixture(folder, *, defaults=False):
    root = Path(folder)
    state = root / "state"
    config = F.write_config(root, state, observer=True)
    value = json.loads(config.read_text(encoding="utf-8"))
    value["lenses"] = {"review": "Assess only.", "peer": "Compare only."}
    if defaults:
        value["lens_defaults"] = {"alpha->beta": "peer"}
    config.write_text(json.dumps(value), encoding="utf-8")
    app = C.Courier.from_config(config)
    O.bind(C)
    return root, state, config, app


def pending_pair(app):
    first = app.send("beta", "event-one", "alpha", "first")
    second = app.send("alpha", "event-two", "beta", "second")
    assert first.status == second.status == "STORED", "fixture precondition: two real participant deliveries"
    assert first.observation["status"] == second.observation["status"] == "PENDING", "fixture precondition: deferred observer facts"
    return {first.attempt_id, second.attempt_id}


def observe(name, **facts):
    OBSERVED[name] = facts


def collector(config, cwd):
    argv = [sys.executable, "-B", str(ROOT / "examples/agenda_collector.py"), str(config), "agenda"]
    try:
        done = subprocess.run(argv, cwd=str(cwd), stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=30)
    except subprocess.TimeoutExpired as error:
        observe("collector", argv=argv, exit=None, timeout=True,
                stdout_b64=base64.b64encode(error.stdout or b"").decode("ascii"),
                stderr_b64=base64.b64encode(error.stderr or b"").decode("ascii"))
        raise AssertionError("actual example collector timed out") from error
    observe("collector", argv=argv, exit=done.returncode,
            stdout_b64=base64.b64encode(done.stdout).decode("ascii"),
            stderr_b64=base64.b64encode(done.stderr).decode("ascii"))
    assert done.returncode == 0, ("actual example collector exit", done.returncode, done.stderr)
    try:
        return json.loads(done.stdout)
    except (ValueError, UnicodeError) as error:
        raise AssertionError("actual example collector returned no JSON") from error


@check("OBSERVER_EVENT_JSON")
def observer_event_json():
    with tempfile.TemporaryDirectory() as folder:
        root, state, config, app = fixture(folder)
        participant_attempts = pending_pair(app)
        C.bind_lens(app, "review")
        result = O.sync(app, 2)
        bodies = F.payloads(state, "worker", "agenda")
        observe("observer_bodies", sync=result,
                payloads_b64=[base64.b64encode(body).decode("ascii") for body in bodies])
        # Always execute the real example, including on the historical prefixed input.
        collected = collector(config, root)
        assert result["attempted"] == 2 and len(result["results"]) == 2, "one batch must attempt both pending events"
        assert all(item["state"] == "published" for item in result["results"]), result
        assert len(bodies) == 2, "both observer payloads must be inspected"
        decoded = []
        for index, body in enumerate(bodies):
            try:
                event = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeError) as error:
                raise AssertionError(("observer payload is not closed JSON", index, body)) from error
            assert type(event) is dict and set(event) == EVENT_FIELDS, ("observer closed event shape", index, event)
            assert event["schema"] == "coura.observer.event.v1", ("observer schema", index, event["schema"])
            expected = json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
            assert body == expected, ("observer payload acquired bytes outside canonical event", index, body)
            decoded.append(event)
        ids = {event["event_id"] for event in decoded}
        assert len(ids) == 2 and ids == {item["event_id"] for item in result["results"]}, "exact two published event identities"
        assert {event["delivery"]["attempt_id"] for event in decoded} == participant_attempts, "both participant facts preserved"
        assert collected["event_count"] == 2, ("actual collector lost an event", collected)
        assert set(collected["projection"]["event_ids"]) == ids, collected
        assert collected["model"] is False and collected["read_means"] == "UNMEASURED", collected
        for key in ("verified_task_state", "verified_read_state", "verified_acceptance", "verified_execution"):
            assert collected["projection"][key] == "unknown", (key, collected)


@check("PARTICIPANT_LENS_CONTROL")
def participant_lens_control():
    with tempfile.TemporaryDirectory() as folder:
        _root, state, _config, app = fixture(folder, defaults=True)
        C.bind_lens(app, "review")
        first = app.send("beta", "bound-notice", "alpha", "hello")
        assert first.status == "STORED", first
        assert F.payloads(state, "worker", "beta")[-1] == REVIEW_HEADER + b"Check bound-notice from alpha. hello", "explicit notification lens lost"
        second = app.send("beta", "default-notice", "alpha", "hello")
        assert second.status == "STORED", second
        assert F.payloads(state, "worker", "beta")[-1] == PEER_HEADER + b"Check default-notice from alpha. hello", "notification default lens lost"
        C.bind_lens(app, "review")
        third = app.send_data("beta", "bound-data", b"\x00raw\xff")
        assert third.status == "STORED", third
        assert F.payloads(state, "worker", "beta")[-1] == REVIEW_HEADER + b"\x00raw\xff", "explicit data lens or opaque suffix lost"
        fourth = app.send_data("beta", "plain-data", b"plain-data")
        assert fourth.status == "STORED", fourth
        assert F.payloads(state, "worker", "beta")[-1] == b"plain-data", "notification default wrongly applied to unbound data"
        observe("participant", payloads_b64=[base64.b64encode(body).decode("ascii") for body in F.payloads(state, "worker", "beta")])


@check("OBSERVER_CALLER_LENS")
def observer_caller_lens():
    with tempfile.TemporaryDirectory() as folder:
        root, state, _config, app = fixture(folder)
        pending_pair(app)
        original_rows = F.journal_rows(state)
        caller_journal = root / "caller" / "journal.jsonl"
        outer_dispatch = {"journal": str(caller_journal), "emit_observation": False}
        C._DISPATCH.set(outer_dispatch)
        C.bind_lens(app, "review")
        armed = dict(C._LENS.get())
        sequence = C._LENS_SEQ.get()
        result = O.sync(app, 2)
        observe("caller_context", before_lens=armed, before_sequence=sequence,
                after_lens=C._LENS.get(), after_sequence=C._LENS_SEQ.get(),
                expected_dispatch=outer_dispatch, after_dispatch=C._DISPATCH.get(), sync=result)
        assert result["attempted"] == 2, "fixture precondition: both internal publications reached"
        assert C._LENS.get() == armed, "internal publication consumed outer bound lens"
        assert C._LENS_SEQ.get() == sequence, "internal publication advanced outer lens sequence"
        assert C._DISPATCH.get() == outer_dispatch, "internal publication changed caller dispatch options"
        # No rebind here: it would conceal loss of the caller's original arm.
        receipt = app.send_data("beta", "after-observer", b"follow-up")
        assert receipt.status == "STORED", receipt
        assert F.payloads(state, "worker", "beta")[-1] == REVIEW_HEADER + b"follow-up", "following participant send did not receive its original lens"
        assert caller_journal.is_file(), "following participant send lost caller journal"
        caller_rows = [json.loads(line) for line in caller_journal.read_text(encoding="utf-8").splitlines()]
        assert any(row.get("key") == "after-observer" and row.get("status") == "STORED" for row in caller_rows), "following participant receipt missing from caller journal"
        assert F.journal_rows(state) == original_rows, "following participant escaped the configured caller journal"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    if args.evidence:
        args.evidence.mkdir(parents=True, exist_ok=False)
    assert tuple(name for name, _test in TESTS) == ORDER and len(TESTS) == EXPECTED_TEETH
    passed = 0
    for name, test in TESTS:
        try:
            contextvars.Context().run(test)
        except AssertionError as error:
            print("FAIL " + name + " " + repr(error), flush=True)
            code = 1
        except Exception as error:
            print("ERROR " + name + " " + repr(error), flush=True)
            traceback.print_exc()
            code = 2
        else:
            print("PASS " + name, flush=True)
            passed += 1
            code = 0
        if args.evidence:
            (args.evidence / "facts.json").write_text(json.dumps(OBSERVED, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if code:
            print("teeth: %d/3" % passed, flush=True)
            return code
    print("teeth: 3/3", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
