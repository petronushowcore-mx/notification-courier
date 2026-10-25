"""Two devices over the existing command contract. No shared Coura state."""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import courier
import coura_remote


ADAPTER = str(ROOT / "coura_remote.py")
SECRET_HEX = "ab" * 32
PHONE = {"kind": "phone", "id": "handset"}
DESK = {"kind": "computer", "id": "desk"}


def canonical(document):
    return coura_remote.canonical(document)


def task_bytes(task_id, subject, key, check="byte-length", origin=None):
    document = {
        "schema": coura_remote.TASK_SCHEMA,
        "task_id": task_id,
        "check": check,
        "subject": subject,
        "key": key,
    }
    if origin is not None:
        document["from"] = origin
    return canonical(document)


def payloads(state):
    found = []
    for row in coura_remote.inbox_rows(state):
        found.append(base64.b64decode(row["payload_b64"], validate=True))
    return found


def executed(state):
    return [row for row in coura_remote.exec_rows(state) if row.get("disposition") == "executed"]


def write_secret(path):
    path.write_text(SECRET_HEX, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_coura(path, state, transport_kind, command, alias, client, identity, timeout=30):
    path.write_text(json.dumps({
        "state_dir": str(state),
        "transports": {
            transport_kind: {
                "type": "command",
                "command": command,
                "timeout": timeout,
                "data_only": True,
                "receipt_status": "STORED",
            }
        },
        "recipients": {alias: {"client": client, "id": identity}},
    }), encoding="utf-8")


def command_wire(recipient, attempt, payload):
    return canonical({
        "recipient": recipient,
        "attempt": attempt,
        "payload": base64.b64encode(payload).decode("ascii"),
    })


def receive_argv(state, kind, identity):
    return [sys.executable, ADAPTER, "receive", "--state", str(state),
            "--expect-kind", kind, "--expect-id", identity]


def peer_file(path, argv):
    path.write_text(json.dumps({"direct": argv}), encoding="utf-8")


def ssh_exec(peer):
    return [sys.executable, ADAPTER, "ssh-exec", "--peer", str(peer)]


class Pair:
    def __init__(self, root, phone_argv=None):
        self.root = Path(root)
        self.computer_remote = self.root / "computer" / "remote-state"
        self.phone_remote = self.root / "phone" / "remote-state"
        self.computer_state = self.root / "computer" / "coura-state"
        self.phone_state = self.root / "phone" / "coura-state"
        self.computer_config = self.root / "computer" / "coura.json"
        self.phone_config = self.root / "phone" / "coura.json"
        self.secret = self.root / "phone" / "result.key"
        for directory in (self.computer_remote, self.phone_remote,
                          self.computer_state, self.phone_state):
            directory.mkdir(parents=True, exist_ok=True)
        write_secret(self.secret)
        phone_argv = phone_argv or receive_argv(self.phone_remote, "phone", "handset")
        computer_peer = self.root / "computer" / "peer.json"
        phone_peer = self.root / "phone" / "peer.json"
        peer_file(computer_peer, phone_argv)
        peer_file(phone_peer, receive_argv(self.computer_remote, "computer", "desk"))
        write_coura(self.computer_config, self.computer_state, "phone",
                    ssh_exec(computer_peer), "handset", "phone", "handset")
        write_coura(self.phone_config, self.phone_state, "computer",
                    ssh_exec(phone_peer), "desk", "computer", "desk")

    def computer(self):
        return courier.Courier.from_config(self.computer_config)

    def complete(self):
        return coura_remote.complete(
            self.phone_remote, self.phone_config, "desk", "phone-1",
            bytes.fromhex(SECRET_HEX))


def terminal(state, key):
    found = None
    for row in courier.records(Path(state) / "journal.jsonl"):
        if row.get("key") == key and row.get("status") in {
                "STORED", "OUTCOME_UNKNOWN", "SKIPPED", "REFUSED"}:
            found = row
    return found


def test_core_does_not_import_the_adapter():
    for name in ("courier.py", "observer.py", "meta_coura.py", "coura_mcp.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        if "coura_remote" in text:
            raise AssertionError("%s imports the remote adapter" % name)
    source = (ROOT / "coura_remote.py").read_text(encoding="utf-8")
    if "shell=True" in source or "os.system" in source:
        raise AssertionError("remote adapter contains a shell call")


def test_two_devices_deliver_both_ways():
    with tempfile.TemporaryDirectory() as folder:
        pair = Pair(folder)
        if pair.computer_state == pair.phone_state or pair.computer_remote == pair.phone_remote:
            raise AssertionError("devices share a state directory")
        subject = "measure me"
        payload = task_bytes(
            "task-1", subject, "task-1",
            origin={"kind": "phone", "id": "not-the-route"})
        peer_before = (pair.root / "computer" / "peer.json").read_bytes()
        receipt = pair.computer().send_data("handset", "task-1", payload, data_only=True)
        if receipt.status != "STORED" or receipt.attempt_id is None:
            raise AssertionError("task was not stored: %s" % receipt.status)
        if (pair.root / "computer" / "peer.json").read_bytes() != peer_before:
            raise AssertionError("message changed the configured route")
        if payloads(pair.phone_remote) != [payload]:
            raise AssertionError("phone did not keep the exact task bytes")
        if payloads(pair.computer_remote):
            raise AssertionError("task payload landed in the computer inbox")
        row = coura_remote.inbox_rows(pair.phone_remote)[0]
        if row["attempt"] != receipt.attempt_id or row["recipient"] != PHONE:
            raise AssertionError("stored attempt or recipient differs from the send")
        journal = terminal(pair.computer_state, "task-1")
        if journal["status"] != "STORED" or journal["attempt"] != receipt.attempt_id:
            raise AssertionError("computer journal does not match the acknowledgement")
        reports = pair.complete()
        if reports != [{
                "task_id": "task-1", "disposition": "executed",
                "send_status": "STORED", "result_key": "result-task-1"}]:
            raise AssertionError("result was not a separate stored message: %r" % reports)
        if len(executed(pair.phone_remote)) != 1:
            raise AssertionError("check did not run once")
        stored = payloads(pair.computer_remote)
        if len(stored) != 1:
            raise AssertionError("computer did not receive one result")
        body = coura_remote.verify_result(
            json.loads(stored[0].decode("utf-8")), "phone-1", bytes.fromhex(SECRET_HEX))
        if body["task_id"] != "task-1" or body["about_key"] != "task-1":
            raise AssertionError("result is not linked to the task")
        if body["about_attempt"] != receipt.attempt_id:
            raise AssertionError("result is not linked to the transport attempt")
        if body["output"] != {"byte_length": len(subject.encode("utf-8"))}:
            raise AssertionError("check output mismatch")
        result_row = coura_remote.inbox_rows(pair.computer_remote)[0]
        if result_row["recipient"] != DESK:
            raise AssertionError("result was routed from the task label")
        if len(payloads(pair.phone_remote)) != 1:
            raise AssertionError("result was written into the phone inbox")


def test_exact_bytes_and_acknowledgement_echo():
    with tempfile.TemporaryDirectory() as folder:
        pair = Pair(folder)
        subject = "quote \" and héllo"
        payload = task_bytes("task-exact", subject, "task-exact")
        blob = b"\x00\xff; rm -rf /"
        task = pair.computer().send_data("handset", "task-exact", payload, data_only=True)
        opaque = pair.computer().send_data("handset", "blob-1", blob, data_only=True)
        if task.status != "STORED" or opaque.status != "STORED":
            raise AssertionError("clean delivery failed")
        if payloads(pair.phone_remote) != [payload, blob]:
            raise AssertionError("stored bytes differ from the send")
        bad = pair.root / "bad_ack.py"
        bad.write_text(
            "import json, sys\n"
            "raw = json.loads(sys.stdin.read())\n"
            "raw['attempt'] = '0' * 32\n"
            "sys.stdout.write(json.dumps({'status':'STORED','recipient':raw['recipient'],"
            "'attempt':raw['attempt']}))\n",
            encoding="utf-8")
        write_coura(pair.root / "bad.json", pair.root / "bad-state", "phone",
                    [sys.executable, str(bad)], "handset", "phone", "handset")
        (pair.root / "bad-state").mkdir()
        wrong = courier.Courier.from_config(pair.root / "bad.json").send_data(
            "handset", "bad-ack", b"same-bytes", data_only=True)
        if wrong.status != "OUTCOME_UNKNOWN":
            raise AssertionError("mismatched attempt was accepted: %s" % wrong.status)
        if len(payloads(pair.phone_remote)) != 2:
            raise AssertionError("mismatched acknowledgement wrote the phone inbox")


def _drop_wrapper(path, counter, receive):
    path.write_text(
        "import pathlib, subprocess, sys\n"
        "pathlib.Path(%r).open('a', encoding='utf-8').write('start\\n')\n"
        "data = sys.stdin.buffer.read(16385)\n"
        "subprocess.run(%r, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
        "raise SystemExit(1)\n" % (str(counter), receive),
        encoding="utf-8")


def _reopen(spec_path):
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    if spec["mode"] == "retry":
        instance = courier.Courier.from_config(spec["config"])
        receipt = instance.send_data(
            spec["recipient"], spec["key"],
            base64.b64decode(spec["payload_b64"]), data_only=True)
        Path(spec["out"]).write_text(json.dumps({
            "status": receipt.status, "held": receipt.held_status}), encoding="utf-8")
        return 0
    receipt = courier.Courier.from_config(spec["computer_config"]).send_data(
        "handset", spec["key"], base64.b64decode(spec["payload_b64"]), data_only=True)
    coura_remote.complete(
        spec["phone_remote"], spec["phone_config"], "desk", "phone-1",
        bytes.fromhex(SECRET_HEX))
    Path(spec["out"]).write_text(json.dumps({
        "status": receipt.status,
        "held": receipt.held_status,
        "executed": len(executed(spec["phone_remote"])),
        "results": len(payloads(spec["computer_remote"])),
    }), encoding="utf-8")
    return 0


def _run_reopen(spec):
    with tempfile.TemporaryDirectory() as folder:
        spec_path = Path(folder) / "spec.json"
        out = Path(folder) / "out.json"
        spec = dict(spec)
        spec["out"] = str(out)
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        done = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--reopen", str(spec_path)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if done.returncode != 0:
            raise AssertionError("reopen failed: %s" % done.stderr.decode("utf-8", "replace")[:400])
        return json.loads(out.read_text(encoding="utf-8"))


def test_ack_lost_after_receipt():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        counter = root / "starts.txt"
        remote = root / "phone" / "remote-state"
        wrapper = root / "drop_ack.py"
        _drop_wrapper(wrapper, counter, receive_argv(remote, "phone", "handset"))
        pair = Pair(root, phone_argv=[sys.executable, str(wrapper)])
        payload = task_bytes("task-lost", "remembered", "task-lost")
        receipt = pair.computer().send_data("handset", "task-lost", payload, data_only=True)
        if receipt.status != "OUTCOME_UNKNOWN":
            raise AssertionError("lost acknowledgement looked successful: %s" % receipt.status)
        if payloads(pair.phone_remote) != [payload]:
            raise AssertionError("receipt was lost and so was the stored payload")
        if counter.read_text(encoding="utf-8") != "start\n":
            raise AssertionError("receiver did not run once")
        reopened = _run_reopen({
            "mode": "retry",
            "config": str(pair.computer_config),
            "recipient": "handset",
            "key": "task-lost",
            "payload_b64": base64.b64encode(payload).decode("ascii"),
        })
        if reopened["status"] != "SKIPPED" or reopened["held"] != "OUTCOME_UNKNOWN":
            raise AssertionError("restart resent an uncertain key: %r" % reopened)
        if counter.read_text(encoding="utf-8") != "start\n":
            raise AssertionError("restart started the command again")
        replay = canonical({
            "attempt": uuid.uuid4().hex,
            "payload": base64.b64encode(payload).decode("ascii"),
            "recipient": PHONE,
        })
        ack = coura_remote.receive_bytes(pair.phone_remote, replay, "phone", "handset")
        if ack["status"] != "STORED" or len(payloads(pair.phone_remote)) != 1:
            raise AssertionError("repeat delivery stored a second task")
        if len(coura_remote.repeat_rows(pair.phone_remote)) != 1:
            raise AssertionError("repeat was not recorded")
        first = pair.complete()
        second = pair.complete()
        if len(executed(pair.phone_remote)) != 1:
            raise AssertionError("task ran more than once")
        if first[0]["disposition"] != "executed" or second != []:
            raise AssertionError("second complete ran the check: %r %r" % (first, second))
        if len(payloads(pair.computer_remote)) != 1:
            raise AssertionError("result was duplicated")


def test_restart_both_sides():
    with tempfile.TemporaryDirectory() as folder:
        pair = Pair(folder)
        payload = task_bytes("task-restart", "stable", "task-restart")
        receipt = pair.computer().send_data(
            "handset", "task-restart", payload, data_only=True)
        if receipt.status != "STORED":
            raise AssertionError("setup delivery failed")
        pair.complete()
        before_exec = len(executed(pair.phone_remote))
        before_results = len(payloads(pair.computer_remote))
        computer_journal = (pair.computer_state / "journal.jsonl").read_bytes()
        phone_journal = (pair.phone_state / "journal.jsonl").read_bytes()
        reopened = _run_reopen({
            "mode": "reopen",
            "computer_config": str(pair.computer_config),
            "phone_config": str(pair.phone_config),
            "phone_remote": str(pair.phone_remote),
            "computer_remote": str(pair.computer_remote),
            "key": "task-restart",
            "payload_b64": base64.b64encode(payload).decode("ascii"),
        })
        if reopened["status"] != "SKIPPED" or reopened["held"] != "STORED":
            raise AssertionError("reopened sender did not keep the stored result: %r" % reopened)
        if reopened["executed"] != before_exec or reopened["results"] != before_results:
            raise AssertionError("restart changed execution or results")
        if (pair.computer_state / "journal.jsonl").read_bytes() != computer_journal:
            raise AssertionError("restart rewrote the computer journal")
        if (pair.phone_state / "journal.jsonl").read_bytes() != phone_journal:
            raise AssertionError("restart rewrote the phone journal")
        if len(executed(pair.phone_remote)) != 1:
            raise AssertionError("restart re-executed the check")


def test_duplicate_delivery_does_not_reexecute():
    with tempfile.TemporaryDirectory() as folder:
        pair = Pair(folder)
        first = task_bytes("task-dup", "alpha", "task-dup")
        second = task_bytes("task-dup", "beta-longer", "task-dup-repeat")
        original = pair.computer().send_data("handset", "task-dup", first, data_only=True)
        if original.status != "STORED":
            raise AssertionError("first delivery failed")
        pair.complete()
        output = executed(pair.phone_remote)[0]["output"]
        repeat = pair.computer().send_data("handset", "task-dup-repeat", first, data_only=True)
        changed = pair.computer().send_data(
            "handset", "task-dup-changed", second, data_only=True)
        if repeat.status != "STORED" or changed.status != "STORED":
            raise AssertionError("a new transport key was refused")
        if payloads(pair.phone_remote) != [first]:
            raise AssertionError("a repeat replaced the stored task")
        flags = [row["same_bytes"] for row in coura_remote.repeat_rows(pair.phone_remote)]
        if flags != [True, False]:
            raise AssertionError("repeat flags were %r" % flags)
        again = pair.complete()
        if again != [] or len(executed(pair.phone_remote)) != 1:
            raise AssertionError("duplicate delivery re-executed the check")
        if executed(pair.phone_remote)[0]["output"] != output:
            raise AssertionError("stored check output changed")
        if output != {"byte_length": len("alpha".encode("utf-8"))}:
            raise AssertionError("first subject was not the one checked")
        if len(payloads(pair.computer_remote)) != 1:
            raise AssertionError("duplicate delivery created another result")


def test_oversize_refused_and_link_is_not_fetched():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        started = root / "started.txt"
        script = root / "ack.py"
        script.write_text(
            "import json, pathlib, sys\n"
            "pathlib.Path(%r).write_text('started', encoding='utf-8')\n"
            "raw = json.loads(sys.stdin.read())\n"
            "sys.stdout.write(json.dumps({'status':'STORED','recipient':raw['recipient'],"
            "'attempt':raw['attempt']}))\n" % str(started),
            encoding="utf-8")
        state = root / "state"
        state.mkdir()
        write_coura(root / "coura.json", state, "phone",
                    [sys.executable, str(script)], "handset", "phone", "handset")
        instance = courier.Courier.from_config(root / "coura.json")
        try:
            instance.send_data("handset", "too-big", b"x" * 6145, data_only=True)
        except courier.Refused:
            pass
        else:
            raise AssertionError("6145-byte payload was accepted")
        if started.exists():
            raise AssertionError("oversize payload started the command")
        accepted = instance.send_data("handset", "fits", b"y" * 6144, data_only=True)
        if accepted.status != "STORED" or not started.exists():
            raise AssertionError("6144-byte payload was not delivered")
        pair = Pair(root / "link-pair")
        sentinel = root / "sentinel.bin"
        sentinel.write_bytes(b"do-not-read")
        link = {
            "schema": coura_remote.LINK_SCHEMA,
            "task_id": "link-1",
            "name": str(sentinel),
            "size": 100000,
            "sha256": "cd" * 32,
        }
        described = coura_remote.describe_fetch(dict(link))
        if described["fetch"] != "not-run" or described["size"] != 100000:
            raise AssertionError("link description fetched or dropped the size")
        if sentinel.read_bytes() != b"do-not-read":
            raise AssertionError("describing a link read the named file")
        body = canonical(link)
        receipt = pair.computer().send_data("handset", "link-1", body, data_only=True)
        if receipt.status != "STORED" or payloads(pair.phone_remote) != [body]:
            raise AssertionError("link message was not stored")
        if pair.complete() != [] or executed(pair.phone_remote):
            raise AssertionError("a link ran a check or a fetch")
        if sentinel.read_bytes() != b"do-not-read":
            raise AssertionError("storing a link read the named file")
        try:
            coura_remote.receive_bytes(pair.phone_remote, b"x" * 16385, "phone", "handset")
        except coura_remote.RemoteError:
            pass
        else:
            raise AssertionError("oversized command input was stored")
        if len(payloads(pair.phone_remote)) != 1:
            raise AssertionError("oversized command input changed the inbox")


def test_signed_result_verification():
    secret = bytes.fromhex(SECRET_HEX)
    body = {
        "schema": coura_remote.RESULT_SCHEMA,
        "task_id": "task-sig",
        "check": "byte-length",
        "output": {"byte_length": 4},
        "about_key": "task-sig",
        "about_attempt": "ab" * 16,
    }
    signed = coura_remote.sign_result(body, "phone-1", secret)
    checked = coura_remote.verify_result(signed, "phone-1", secret)
    if checked["output"] != {"byte_length": 4}:
        raise AssertionError("valid result did not verify")
    tampered = json.loads(canonical(signed).decode("ascii"))
    tampered["output"] = {"byte_length": 5}
    try:
        coura_remote.verify_result(tampered, "phone-1", secret)
    except coura_remote.RemoteError:
        pass
    else:
        raise AssertionError("tampered output verified")
    flipped = json.loads(canonical(signed).decode("ascii"))
    flipped["signature"]["mac"] = "00" * 32
    try:
        coura_remote.verify_result(flipped, "phone-1", secret)
    except coura_remote.RemoteError:
        pass
    else:
        raise AssertionError("tampered mac verified")
    try:
        coura_remote.verify_result(signed, "phone-1", b"\x00" * 32)
    except coura_remote.RemoteError:
        pass
    else:
        raise AssertionError("wrong key verified")
    with tempfile.TemporaryDirectory() as folder:
        sentinel = Path(folder) / "ran.txt"
        dangerous_body = dict(body)
        dangerous_body["output"] = {"note": "touch %s" % sentinel}
        signed_danger = coura_remote.sign_result(dangerous_body, "phone-1", secret)
        coura_remote.verify_result(signed_danger, "phone-1", secret)
        if sentinel.exists():
            raise AssertionError("verifying a result executed its output")


def test_message_text_is_never_executed():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        sentinel = root / "pwned"
        pair = Pair(root / "devices")
        text = "touch %s" % sentinel
        allowed = task_bytes("task-text", text, "task-text")
        blocked = task_bytes("task-shell", text, "task-shell", check="os.system")
        first = pair.computer().send_data("handset", "task-text", allowed, data_only=True)
        second = pair.computer().send_data("handset", "task-shell", blocked, data_only=True)
        if first.status != "STORED" or second.status != "STORED":
            raise AssertionError("text payloads were not delivered")
        reports = pair.complete()
        dispositions = {row["task_id"]: row["disposition"] for row in reports}
        if dispositions != {"task-text": "executed", "task-shell": "not_allowed"}:
            raise AssertionError("dispositions were %r" % dispositions)
        if sentinel.exists():
            raise AssertionError("message text was executed")
        if executed(pair.phone_remote)[0]["output"] != {
                "byte_length": len(text.encode("utf-8"))}:
            raise AssertionError("allowed check did not return the byte length")
        if any(row.get("check") == "os.system" and row.get("disposition") == "executed"
               for row in coura_remote.exec_rows(pair.phone_remote)):
            raise AssertionError("disallowed check ran")


def test_unsent_result_is_delivered_without_rerun():
    with tempfile.TemporaryDirectory() as folder:
        pair = Pair(folder)
        payload = task_bytes("task-gap", "hello", "task-gap")
        receipt = pair.computer().send_data("handset", "task-gap", payload, data_only=True)
        if receipt.status != "STORED":
            raise AssertionError("setup delivery failed")
        coura_remote._append(pair.phone_remote / "remote" / "exec.jsonl", {
            "task_id": "task-gap",
            "check": "byte-length",
            "disposition": "executed",
            "output": {"byte_length": 1},
            "about_key": "task-gap",
            "about_attempt": "cd" * 16,
        })
        reports = pair.complete()
        if len(executed(pair.phone_remote)) != 1:
            raise AssertionError("the check ran again after a crash")
        if executed(pair.phone_remote)[0]["output"] != {"byte_length": 1}:
            raise AssertionError("stored output was recomputed")
        if reports[0]["send_status"] != "STORED":
            raise AssertionError("stored result was not sent")
        body = json.loads(payloads(pair.computer_remote)[0].decode("utf-8"))
        if body["output"] != {"byte_length": 1}:
            raise AssertionError("sent result was not the stored output")
        if pair.complete() != []:
            raise AssertionError("a second complete sent the result again")


def test_ssh_argv_is_fixed_and_has_no_shell():
    argv = coura_remote.ssh_argv({
        "program": "ssh",
        "user": "u0",
        "address": "192.0.2.10",
        "port": 8022,
        "identity_file": "/home/me/key",
        "known_hosts": "/home/me/known_hosts",
        "remote_command": ["python3", "coura_remote.py", "receive", "--state",
                           "/data/coura-state"],
    })
    if argv[0] != "ssh" or "-T" not in argv or "BatchMode=yes" not in argv:
        raise AssertionError("ssh argv lost a required flag: %r" % argv)
    if "--" not in argv or argv[argv.index("--") + 1] != "python3":
        raise AssertionError("remote command is not separated from ssh options")
    if "u0@192.0.2.10" not in argv or "u0@[192.0.2.10]" in argv:
        raise AssertionError("an IPv4 destination was rewritten: %r" % argv)
    if any(item in argv for item in ("bash", "sh", "-c")):
        raise AssertionError("ssh argv contains a shell")
    try:
        coura_remote.ssh_argv({
            "program": "ssh", "user": "a;rm", "address": "192.0.2.10", "port": 22,
            "identity_file": "/home/me/key", "known_hosts": "/home/me/known_hosts",
            "remote_command": ["python3"],
        })
    except coura_remote.RemoteError:
        pass
    else:
        raise AssertionError("shell metacharacters were accepted in the ssh user")


def test_ssh_address_with_a_colon_is_bracketed():
    argv = coura_remote.ssh_argv({
        "program": "ssh",
        "user": "u0",
        "address": "2001:db8::10",
        "port": 8022,
        "identity_file": "/home/me/key",
        "known_hosts": "/home/me/known_hosts",
        "remote_command": ["python3", "coura_remote.py", "receive"],
    })
    if "u0@[2001:db8::10]" not in argv:
        raise AssertionError("IPv6 destination was not bracketed: %r" % argv)
    if "u0@2001:db8::10" in argv:
        raise AssertionError("unbracketed IPv6 destination was passed to ssh")


def test_wrong_recipient_is_not_stored():
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder) / "state"
        payload = task_bytes("task-wrong", "nope", "task-wrong")
        wire = command_wire(DESK, "aa" * 16, payload)
        try:
            coura_remote.receive_bytes(state, wire, "phone", "handset")
        except coura_remote.RemoteError as error:
            if "does not match" not in str(error):
                raise AssertionError(error)
        else:
            raise AssertionError("wrong recipient was accepted")
        if (state / "remote" / "inbox.jsonl").exists() or payloads(state):
            raise AssertionError("wrong recipient was stored")
        done = subprocess.run(
            receive_argv(state, "phone", "handset"), input=wire,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if done.returncode == 0 or b"STORED" in done.stdout:
            raise AssertionError("wrong recipient was acknowledged: %r" % done.stdout)
        if b"does not match" not in done.stderr:
            raise AssertionError(done.stderr)
        if payloads(state):
            raise AssertionError("command receive stored a wrong recipient")


def test_result_with_the_same_task_id_is_stored():
    with tempfile.TemporaryDirectory() as folder:
        pair = Pair(folder)
        result = canonical({
            "schema": coura_remote.RESULT_SCHEMA,
            "task_id": "task-same",
            "check": "byte-length",
            "output": {"byte_length": 9},
            "about_key": "task-same",
            "about_attempt": "ab" * 16,
            "signature": {"alg": "hmac-sha256", "key_id": "phone-1", "mac": "cd" * 32},
        })
        first = coura_remote.receive_bytes(
            pair.phone_remote, command_wire(PHONE, "11" * 16, result), "phone", "handset")
        payload = task_bytes("task-same", "same-task", "task-same")
        second = coura_remote.receive_bytes(
            pair.phone_remote, command_wire(PHONE, "22" * 16, payload), "phone", "handset")
        if first["status"] != "STORED" or second["status"] != "STORED":
            raise AssertionError("setup receive failed")
        if payloads(pair.phone_remote) != [result, payload]:
            raise AssertionError("a result was dropped as a repeat of the task")
        reports = pair.complete()
        if len(reports) != 1 or reports[0]["send_status"] != "STORED":
            raise AssertionError("complete did not send one result: %r" % reports)
        if len(executed(pair.phone_remote)) != 1:
            raise AssertionError("the shared task id ran more than once")
        if executed(pair.phone_remote)[0]["output"] != {"byte_length": len("same-task".encode("utf-8"))}:
            raise AssertionError("the task subject was not the checked data")
        if len(payloads(pair.computer_remote)) != 1:
            raise AssertionError("complete created more than one result message")


def test_complete_does_not_hold_the_inbox_during_send():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        remote = root / "remote"
        coura_state = root / "coura-state"
        remote.mkdir()
        coura_state.mkdir()
        secret = root / "result.key"
        write_secret(secret)
        config = root / "coura.json"
        write_coura(config, coura_state, "computer",
                    receive_argv(remote, "computer", "desk"),
                    "desk", "computer", "desk", timeout=3)
        payload = task_bytes("task-lock", "abcdef", "task-lock")
        ack = coura_remote.receive_bytes(
            remote, command_wire(PHONE, "33" * 16, payload), "phone", "handset")
        if ack["status"] != "STORED":
            raise AssertionError("task setup failed")
        started = time.monotonic()
        reports = coura_remote.complete(
            remote, config, "desk", "phone-1", bytes.fromhex(SECRET_HEX))
        elapsed = time.monotonic() - started
        if elapsed >= 2.5:
            raise AssertionError("complete waited on its own inbox lock: %.2fs" % elapsed)
        if len(reports) != 1 or reports[0]["send_status"] != "STORED":
            raise AssertionError("same-state result was not stored: %r" % reports)
        if len(executed(remote)) != 1:
            raise AssertionError("check did not run once")
        schemas = []
        for item in payloads(remote):
            schemas.append(json.loads(item.decode("utf-8"))["schema"])
        if schemas != [coura_remote.TASK_SCHEMA, coura_remote.RESULT_SCHEMA]:
            raise AssertionError("task and result were not both stored: %r" % schemas)
        if coura_remote.complete(
                remote, config, "desk", "phone-1", bytes.fromhex(SECRET_HEX)) != []:
            raise AssertionError("a second complete sent the result again")


def test_max_data_payload_is_stored_in_the_inbox():
    subject = ""
    payload = task_bytes("task-max", subject, "task-max")
    while len(payload) < courier.MAX_DATA:
        subject += "a"
        payload = task_bytes("task-max", subject, "task-max")
    if len(payload) != courier.MAX_DATA:
        raise AssertionError("fixture is %s bytes" % len(payload))
    attempt = "de" * 16
    raw = command_wire(PHONE, attempt, payload)
    if len(raw) > courier.MAX_DATA_RECORD:
        raise AssertionError("command wire exceeds 16384")
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder)
        try:
            ack = coura_remote.receive_bytes(state, raw, "phone", "handset")
        except courier.Refused as error:
            raise AssertionError("6144-byte payload was not stored: %s" % error) from error
        if ack["status"] != "STORED" or ack["attempt"] != attempt:
            raise AssertionError("acknowledgement mismatch %r" % ack)
        if payloads(state) != [payload]:
            raise AssertionError("stored bytes do not match the 6144-byte payload")
        again = coura_remote.receive_bytes(state, raw, "phone", "handset")
        if again["status"] != "STORED" or payloads(state) != [payload]:
            raise AssertionError("the same task was stored twice")


def test_oversized_inbox_line_is_refused():
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder)
        path = state / "remote" / "inbox.jsonl"
        path.parent.mkdir(parents=True)
        filler = "a" * (courier.MAX_DATA_RECORD + 64)
        line = (json.dumps({"payload_b64": filler}) + "\n").encode("utf-8")
        if len(line) <= courier.MAX_DATA_RECORD:
            raise AssertionError("fixture line is not oversized")
        path.write_bytes(line)
        try:
            coura_remote.inbox_rows(state)
        except coura_remote.RemoteError as error:
            if "oversized" not in str(error):
                raise AssertionError(error)
        else:
            raise AssertionError("oversized inbox line was read")


def test_finished_result_is_not_sent_again_when_the_mark_is_lost():
    with tempfile.TemporaryDirectory() as folder:
        pair = Pair(folder)
        payload = task_bytes("task-mark", "marked", "task-mark")
        receipt = pair.computer().send_data("handset", "task-mark", payload, data_only=True)
        if receipt.status != "STORED":
            raise AssertionError("setup delivery failed")
        first = pair.complete()
        if first[0]["send_status"] != "STORED" or len(payloads(pair.computer_remote)) != 1:
            raise AssertionError("first result was not stored")
        path = pair.phone_remote / "remote" / "exec.jsonl"
        kept = [row for row in coura_remote.exec_rows(pair.phone_remote)
                if row.get("disposition") != "result-sent"]
        path.write_text("".join(
            canonical(row).decode("ascii") + "\n" for row in kept), encoding="ascii")
        again = pair.complete()
        if len(executed(pair.phone_remote)) != 1:
            raise AssertionError("losing the sent mark reran the check")
        if len(again) != 1 or again[0]["send_status"] != "SKIPPED":
            raise AssertionError("a held result was sent again: %r" % again)
        if len(payloads(pair.computer_remote)) != 1:
            raise AssertionError("a second result payload was stored")


def _unrun_lines():
    sshd = shutil.which("sshd")
    client = shutil.which("ssh")
    if sshd:
        hop = ("UNRUN sshd hop: sshd is on PATH at %s but this run did not start it" % sshd)
    else:
        located = client or "absent"
        hop = ("UNRUN sshd hop: OpenSSH client is %s; sshd is not on PATH. "
               "No server was installed or started. The measured path is the "
               "command contract through a local receive process." % located)
    return [
        hop,
        "UNRUN Termux: not run on an Android device.",
    ]


def main():
    tests = [
        test_core_does_not_import_the_adapter,
        test_two_devices_deliver_both_ways,
        test_exact_bytes_and_acknowledgement_echo,
        test_ack_lost_after_receipt,
        test_restart_both_sides,
        test_duplicate_delivery_does_not_reexecute,
        test_oversize_refused_and_link_is_not_fetched,
        test_signed_result_verification,
        test_message_text_is_never_executed,
        test_unsent_result_is_delivered_without_rerun,
        test_ssh_argv_is_fixed_and_has_no_shell,
        test_ssh_address_with_a_colon_is_bracketed,
        test_wrong_recipient_is_not_stored,
        test_result_with_the_same_task_id_is_stored,
        test_complete_does_not_hold_the_inbox_during_send,
        test_finished_result_is_not_sent_again_when_the_mark_is_lost,
        test_max_data_payload_is_stored_in_the_inbox,
        test_oversized_inbox_line_is_refused,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as error:
            traceback.print_exc()
            print("FAIL %s %s" % (test.__name__, error))
            failed += 1
        else:
            print("PASS %s" % test.__name__)
    for line in _unrun_lines():
        print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--reopen":
        raise SystemExit(_reopen(sys.argv[2]))
    raise SystemExit(main())
