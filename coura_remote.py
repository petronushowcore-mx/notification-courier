"""Cross-machine adapter over Coura's existing command transport.

Each device keeps its own state directory. A send is `Courier.send_data`
through a configured command. That command may be a direct receive process
or OpenSSH running the same receive process on the other device. There is
no shared directory and no new Coura transport.

The command acknowledgement is only a transport receipt. A check result is
a later message. Message text and a valid result signature are data. They
are not a shell command and not a model instruction.

OpenSSH is an external dependency of the SSH form. The base courier.py does
not import this adapter; the optional usability commands do.
"""
import argparse
import base64
import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import courier


TASK_SCHEMA = "coura.remote.task.v1"
LINK_SCHEMA = "coura.remote.link.v1"
RESULT_SCHEMA = "coura.remote.result.v1"
IMPLEMENTED_CHECKS = frozenset({"byte-length"})
MAX_SUBJECT = 1024
_ATTEMPT = __import__("re").compile(r"[0-9a-f]{32}")
_TOKEN = __import__("re").compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}")
_SSH_TOKEN = __import__("re").compile(r"[A-Za-z0-9._:-]{1,128}")
_SHA = __import__("re").compile(r"[0-9a-f]{64}")


class RemoteError(Exception):
    """The remote adapter refused this step. Inspect retained state for prior work."""


def canonical(document):
    return json.dumps(document, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _pairs(items):
    result = {}
    for name, value in items:
        if name in result:
            raise RemoteError("Duplicate JSON key")
        result[name] = value
    return result


def _loads(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise RemoteError("Invalid JSON") from error
    if type(value) is not dict:
        raise RemoteError("JSON value must be an object")
    return value


def _address(kind, identity):
    try:
        address = courier.Address(kind, identity)
    except courier.Refused as error:
        raise RemoteError("Invalid recipient") from error
    return {"kind": address.client, "id": address.identity}


def _token(value, label):
    if type(value) is not str or _TOKEN.fullmatch(value) is None:
        raise RemoteError("Invalid " + label)
    return value


def _directory(state):
    path = Path(state) / "remote"
    courier.ensure_private_dir(path)
    return path


def _read_jsonl(path, limit=courier.MAX_RECORD):
    if not path.exists():
        return []
    rows = []
    with courier.file_lock(path), path.open("rb") as stream:
        while True:
            row = _read_one(stream, limit)
            if row is None:
                return rows
            rows.append(row)


def _append(path, row, limit=courier.MAX_RECORD):
    courier.ensure_private_dir(path.parent)
    courier.append_record(path, row, limit)


def _read_one(stream, limit):
    while True:
        raw = stream.readline(limit + 1)
        if not raw:
            return None
        if raw == b"\n":
            continue
        if len(raw) > limit or not raw.endswith(b"\n"):
            raise RemoteError("Incomplete or oversized remote record")
        return _loads(raw)


def inbox_rows(state):
    return _read_jsonl(_directory(state) / "inbox.jsonl", courier.MAX_DATA_RECORD)


def exec_rows(state):
    return _read_jsonl(_directory(state) / "exec.jsonl")


def repeat_rows(state):
    return _read_jsonl(_directory(state) / "repeats.jsonl", courier.MAX_DATA_RECORD)


def _payload_bytes(encoded):
    if type(encoded) is not str or len(encoded) > ((courier.MAX_DATA + 2) // 3) * 4:
        raise RemoteError("Invalid payload")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError) as error:
        raise RemoteError("Invalid payload") from error
    if base64.b64encode(payload).decode("ascii") != encoded:
        raise RemoteError("Noncanonical payload")
    if not 0 < len(payload) <= courier.MAX_DATA:
        raise RemoteError("Payload must contain 1..6144 bytes")
    return payload


def _parse_wire(raw):
    if type(raw) is not bytes or len(raw) > courier.MAX_DATA_RECORD:
        raise RemoteError("Command input exceeds 16384 bytes")
    message = _loads(raw)
    if set(message) != {"recipient", "attempt", "payload"}:
        raise RemoteError("Command input must be recipient, attempt, and payload")
    recipient = message["recipient"]
    if type(recipient) is not dict or set(recipient) != {"kind", "id"}:
        raise RemoteError("Invalid recipient")
    expect = _address(recipient["kind"], recipient["id"])
    attempt = message["attempt"]
    if type(attempt) is not str or _ATTEMPT.fullmatch(attempt) is None:
        raise RemoteError("Invalid attempt")
    payload = _payload_bytes(message["payload"])
    return expect, attempt, payload


def _task_id_of(payload):
    try:
        document = _loads(payload)
    except RemoteError:
        return None, None
    schema = document.get("schema")
    task_id = document.get("task_id")
    if schema not in (TASK_SCHEMA, LINK_SCHEMA, RESULT_SCHEMA):
        return None, document
    if type(task_id) is not str or _TOKEN.fullmatch(task_id) is None:
        return None, document
    return task_id, document


def _stored_schema(row):
    schema = row.get("schema")
    if schema in (TASK_SCHEMA, LINK_SCHEMA, RESULT_SCHEMA):
        return schema
    encoded = row.get("payload_b64")
    if type(encoded) is not str:
        return None
    try:
        payload = base64.b64decode(encoded, validate=True)
        _task_id, document = _task_id_of(payload)
    except (RemoteError, ValueError):
        return None
    if type(document) is not dict:
        return None
    schema = document.get("schema")
    if schema in (TASK_SCHEMA, LINK_SCHEMA, RESULT_SCHEMA):
        return schema
    return None


def receive_bytes(state, raw, expect_kind, expect_id):
    """Store one command payload, then return the exact acknowledgement.

    Storage happens before the acknowledgement is returned. A repeat is the
    same schema and task id. A result is not a repeat of a task that uses the
    same task id. Exact repeat bytes are retained in the repeat audit. Different
    bytes or a different address are retained as a conflict without replacing
    or executing the first task. STORED reports durable transport storage,
    not task admission. A mismatched receiving address is not stored.
    """
    expected = _address(expect_kind, expect_id)
    recipient, attempt, payload = _parse_wire(raw)
    if recipient != expected:
        raise RemoteError("Recipient does not match this device")
    digest = hashlib.sha256(payload).hexdigest()
    task_id, document = _task_id_of(payload)
    schema = None
    if task_id is not None and type(document) is dict:
        schema = document.get("schema")
    root = _directory(state)
    with courier.file_lock(root / "gate"):
        rows = _read_jsonl(root / "inbox.jsonl", courier.MAX_DATA_RECORD)
        prior = None
        for row in rows:
            if (task_id is not None and row.get("task_id") == task_id and
                    _stored_schema(row) == schema):
                prior = row
                break
        if prior is None:
            _append(root / "inbox.jsonl", {
                "attempt": attempt,
                "recipient": recipient,
                "schema": schema,
                "task_id": task_id,
                "payload_sha256": digest,
                "payload_b64": base64.b64encode(payload).decode("ascii"),
            }, courier.MAX_DATA_RECORD)
        else:
            same = prior.get("payload_sha256") == digest and prior.get("recipient") == recipient
            _append(root / "repeats.jsonl", {
                "schema": schema, "task_id": task_id, "attempt": attempt,
                "recipient": recipient, "same_bytes": prior.get("payload_sha256") == digest,
                "conflict": not same, "original_payload_sha256": prior.get("payload_sha256"),
                "payload_sha256": digest, "payload_b64": base64.b64encode(payload).decode("ascii"),
            }, courier.MAX_DATA_RECORD)
    return {"status": "STORED", "recipient": recipient, "attempt": attempt}


def _subject(value):
    if type(value) is not str or "\x00" in value or len(value.encode("utf-8")) > MAX_SUBJECT:
        raise RemoteError("Subject must be short text data")
    return value


def parse_task(payload):
    document = _loads(payload)
    if set(document) - {"from", "key"} != {"schema", "task_id", "check", "subject"}:
        raise RemoteError("Task fields are not the fixed set")
    if document.get("schema") != TASK_SCHEMA:
        raise RemoteError("Not a task")
    task_id = _token(document.get("task_id"), "task id")
    check = document.get("check")
    if type(check) is not str or check not in IMPLEMENTED_CHECKS:
        raise RemoteError("Check is not allowed")
    subject = _subject(document.get("subject"))
    if "from" in document:
        origin = document["from"]
        if type(origin) is not dict or set(origin) != {"kind", "id"}:
            raise RemoteError("Invalid from label")
        _address(origin["kind"], origin["id"])
    key = document.get("key")
    if key is not None and (type(key) is not str or not key or len(key) > 256 or "\x00" in key):
        raise RemoteError("Invalid task key label")
    return {"task_id": task_id, "check": check, "subject": subject, "key": key}


def run_check(check, subject):
    """Run one fixed check. The subject is data, never a command line."""
    if check != "byte-length" or check not in IMPLEMENTED_CHECKS:
        raise RemoteError("Check is not allowed")
    return {"byte_length": len(_subject(subject).encode("utf-8"))}


def describe_fetch(document):
    """Describe a file link. This does not open a file or a network connection."""
    if type(document) is not dict or set(document) != {
            "schema", "task_id", "name", "size", "sha256"}:
        raise RemoteError("Link fields are not the fixed set")
    if document.get("schema") != LINK_SCHEMA:
        raise RemoteError("Not a file link")
    _token(document.get("task_id"), "task id")
    name = document.get("name")
    if type(name) is not str or not name or len(name) > 200 or "\x00" in name:
        raise RemoteError("Invalid link name")
    size = document.get("size")
    if type(size) is not int or size < 0:
        raise RemoteError("Invalid link size")
    digest = document.get("sha256")
    if type(digest) is not str or _SHA.fullmatch(digest) is None:
        raise RemoteError("Invalid link hash")
    return {
        "fetch": "not-run",
        "name": name,
        "size": size,
        "sha256": digest,
        "detail": "Large file transfer is a separate step. This call does not fetch.",
    }


def _secret_bytes(secret):
    if type(secret) is not bytes or len(secret) != 32:
        raise RemoteError("Result key must be 32 bytes")
    return secret


def sign_result(body, key_id, secret):
    """Attach an HMAC over the canonical result body. The body stays data."""
    if type(body) is not dict or "signature" in body:
        raise RemoteError("Result body must not already contain a signature")
    key_id = _token(key_id, "key id")
    mac = hmac.new(_secret_bytes(secret), canonical(body), hashlib.sha256).hexdigest()
    signed = dict(body)
    signed["signature"] = {"alg": "hmac-sha256", "key_id": key_id, "mac": mac}
    return signed


def verify_result(document, key_id, secret):
    """Check a result signature and return the body. Does not execute output."""
    if type(document) is not dict or set(document) != {
            "schema", "task_id", "check", "output", "about_key", "about_attempt",
            "signature"}:
        raise RemoteError("Result fields are not the fixed set")
    if document.get("schema") != RESULT_SCHEMA:
        raise RemoteError("Not a result")
    if (type(document.get("about_key")) is not str or type(document.get("about_attempt")) is not str
            or type(document.get("output")) is not dict or type(document.get("task_id")) is not str):
        raise RemoteError("Result signature did not verify")
    signature = document["signature"]
    if (type(signature) is not dict or set(signature) != {"alg", "key_id", "mac"} or
            signature.get("alg") != "hmac-sha256"):
        raise RemoteError("Result signature did not verify")
    if signature.get("key_id") != _token(key_id, "key id"):
        raise RemoteError("Result signature did not verify")
    mac = signature.get("mac")
    if type(mac) is not str or _SHA.fullmatch(mac) is None:
        raise RemoteError("Result signature did not verify")
    body = {name: document[name] for name in (
        "schema", "task_id", "check", "output", "about_key", "about_attempt")}
    expected = hmac.new(_secret_bytes(secret), canonical(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        raise RemoteError("Result signature did not verify")
    return body


def _load_secret(path):
    raw = Path(path).read_text(encoding="utf-8").strip()
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise RemoteError("Result key file must contain 64 lowercase hex characters")
    return bytes.fromhex(raw)


def _pending_executions(root):
    """Reserve a new fixed check before executing; incomplete starts stay unknown.

    Caller holds the inbox gate. task_id is unique within this remote state.
    A result row with that id does not request another computation.
    """
    inbox = _read_jsonl(root / "inbox.jsonl", courier.MAX_DATA_RECORD)
    existing = _read_jsonl(root / "exec.jsonl")
    sent = {row.get("task_id") for row in existing
            if row.get("disposition") == "result-sent" and (
                row.get("send_status") in {"STORED", "QUEUED", "THREAD_ACCEPTED"} or
                row.get("send_status") == "SKIPPED" and row.get("held_status") in {
                    "STORED", "QUEUED", "THREAD_ACCEPTED"})}
    ran = {row.get("task_id"): row for row in existing
           if row.get("disposition") == "executed"}
    started = {row.get("task_id") for row in existing if row.get("disposition") == "started"}
    pending = []
    queued = set()
    for row in inbox:
        task_id = row.get("task_id")
        if type(task_id) is not str or task_id in sent or task_id in queued:
            continue
        try:
            payload = base64.b64decode(row["payload_b64"], validate=True)
            task = parse_task(payload)
        except (RemoteError, ValueError, KeyError):
            continue
        if task["task_id"] != task_id:
            continue
        queued.add(task_id)
        if task_id in ran:
            pending.append(ran[task_id])
            continue
        if task_id in started:
            pending.append({"task_id": task_id, "disposition": "execution_unknown"})
            continue
        _append(root / "exec.jsonl", {
            "task_id": task_id, "disposition": "started",
            "task_sha256": hashlib.sha256(payload).hexdigest(),
            "recipient": row.get("recipient"), "about_attempt": row.get("attempt"),
        })
        output = run_check(task["check"], task["subject"])
        record = {
            "task_id": task_id, "check": task["check"], "disposition": "executed",
            "output": output,
            "about_key": task["key"] if task["key"] is not None else "task-id:" + task_id,
            "about_attempt": row.get("attempt"),
        }
        _append(root / "exec.jsonl", record)
        ran[task_id] = record
        pending.append(record)
    return pending


def _prepared_result(root, record, target, key_id, secret):
    """Retain exact signed bytes and return address before the first send."""
    address = {"kind": target.client, "id": target.identity}
    result_key = "result-" + record["task_id"]
    with courier.file_lock(root / "gate"):
        saved = [row for row in _read_jsonl(root / "exec.jsonl")
                 if row.get("task_id") == record["task_id"]
                 and row.get("disposition") == "result-prepared"]
        if saved:
            item = saved[-1]
            if item.get("recipient") != address or item.get("key_id") != key_id:
                raise RemoteError("Stored result is bound to a different return address or key id")
            payload = _payload_bytes(item.get("payload_b64"))
            if hashlib.sha256(payload).hexdigest() != item.get("payload_sha256"):
                raise RemoteError("Stored result bytes are unreadable")
            if item.get("result_key") != result_key:
                raise RemoteError("Stored result key is unreadable")
            return result_key, payload
        body = {
            "schema": RESULT_SCHEMA, "task_id": record["task_id"],
            "check": record["check"], "output": record["output"],
            "about_key": record["about_key"], "about_attempt": record["about_attempt"],
        }
        payload = canonical(sign_result(body, key_id, secret))
        _payload_bytes(base64.b64encode(payload).decode("ascii"))
        _append(root / "exec.jsonl", {
            "task_id": record["task_id"], "disposition": "result-prepared",
            "recipient": address, "key_id": key_id, "result_key": result_key,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "payload_b64": base64.b64encode(payload).decode("ascii"),
        })
        return result_key, payload


def complete(state, config, recipient, key_id, secret):
    """Reserve each fixed check, retain its output, then send retained result bytes.

    An interrupted start without a stored output is execution_unknown and is
    never automatically run again. The return address is local configuration.
    No inbox gate is held across delivery. Only confirmed transport statuses
    become result-sent; refusal and unknown delivery keep the same result key.
    """
    key_id = _token(key_id, "key id")
    secret = _secret_bytes(secret)
    root = _directory(state)
    with courier.file_lock(root / "gate"):
        pending = _pending_executions(root)
    reports = []
    instance = None
    for record in pending:
        if record.get("disposition") == "execution_unknown":
            reports.append({"task_id": record["task_id"], "disposition": "execution_unknown"})
            continue
        if instance is None:
            instance = courier.Courier.from_config(config)
        result_key, payload = _prepared_result(
            root, record, instance.resolve(recipient), key_id, secret)
        receipt = instance.send_data(recipient, result_key, payload, data_only=True)
        confirmed = receipt.status in {"STORED", "QUEUED", "THREAD_ACCEPTED"} or (
            receipt.status == "SKIPPED" and receipt.held_status in {
                "STORED", "QUEUED", "THREAD_ACCEPTED"})
        with courier.file_lock(root / "gate"):
            already = any(
                row.get("task_id") == record["task_id"] and
                row.get("disposition") == "result-sent" and
                row.get("send_status") in {"STORED", "QUEUED", "THREAD_ACCEPTED"}
                for row in _read_jsonl(root / "exec.jsonl"))
            if not already:
                _append(root / "exec.jsonl", {
                    "task_id": record["task_id"],
                    "disposition": "result-sent" if confirmed else "delivery-held",
                    "send_status": receipt.status, "held_status": receipt.held_status,
                    "result_key": result_key,
                })
        reports.append({
            "task_id": record["task_id"], "disposition": "executed",
            "send_status": receipt.status, "result_key": result_key,
        })
    return reports + _record_disallowed(state)


def _record_disallowed(state):
    """Remember task ids whose check is not the fixed allowlist. Do not run them."""
    root = _directory(state)
    reports = []
    with courier.file_lock(root / "gate"):
        done = {row.get("task_id") for row in _read_jsonl(root / "exec.jsonl")}
        for row in _read_jsonl(root / "inbox.jsonl", courier.MAX_DATA_RECORD):
            task_id = row.get("task_id")
            if type(task_id) is not str or task_id in done:
                continue
            try:
                payload = base64.b64decode(row["payload_b64"], validate=True)
                document = _loads(payload)
            except (RemoteError, ValueError, KeyError):
                continue
            if document.get("schema") != TASK_SCHEMA:
                continue
            check = document.get("check")
            if check in IMPLEMENTED_CHECKS:
                continue
            _append(root / "exec.jsonl", {
                "task_id": task_id,
                "check": check if type(check) is str else None,
                "disposition": "not_allowed",
            })
            done.add(task_id)
            reports.append({"task_id": task_id, "disposition": "not_allowed"})
    return reports


def ssh_argv(peer):
    """Fixed OpenSSH argv. The message does not supply any of these fields."""
    if type(peer) is not dict or set(peer) != {
            "program", "user", "address", "port", "identity_file", "known_hosts",
            "remote_command"}:
        raise RemoteError("SSH peer fields are not the fixed set")
    program = peer["program"]
    if type(program) is not str or program not in {"ssh", "ssh.exe"}:
        raise RemoteError("SSH program must be ssh")
    user = peer["user"]
    address = peer["address"]
    if (type(user) is not str or type(address) is not str or
            _SSH_TOKEN.fullmatch(user) is None or _SSH_TOKEN.fullmatch(address) is None):
        raise RemoteError("SSH user and address must be single tokens")
    # OpenSSH reads user@host. A colon in the host is a port unless the host
    # is in brackets. The brackets are added here; the peer file cannot supply them.
    port = peer["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise RemoteError("SSH port is invalid")
    identity = peer["identity_file"]
    known = peer["known_hosts"]
    if (type(identity) is not str or type(known) is not str or not identity or not known
            or "\x00" in identity or "\x00" in known):
        raise RemoteError("SSH file paths are invalid")
    remote = peer["remote_command"]
    if (type(remote) is not list or not remote or
            any(type(item) is not str or not item or "\x00" in item for item in remote)):
        raise RemoteError("Remote command must be a nonempty argument list")
    return [
        program, "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "LogLevel=ERROR",
        "-o", "UserKnownHostsFile=" + known,
        "-i", identity,
        "-p", str(port),
        user + ("@[" + address + "]" if ":" in address else "@" + address),
        "--",
        *remote,
    ]


def load_peer(path):
    document = _loads(Path(path).read_bytes())
    if set(document) != {"direct"} and set(document) != {"ssh"}:
        raise RemoteError("Peer file must contain direct or ssh")
    if "direct" in document:
        command = document["direct"]
        if (type(command) is not list or not command or
                any(type(item) is not str or not item or "\x00" in item for item in command)):
            raise RemoteError("Direct command must be a nonempty argument list")
        return command
    return ssh_argv(document["ssh"])


def forward(argv, payload):
    """Run the local argv without a shell; bound captured output in memory.

    The shared runner uses temporary files, so total output on disk and
    descendant lifetimes are not bounded by this adapter.
    """
    if type(payload) is not bytes or len(payload) > courier.MAX_DATA_RECORD:
        raise RemoteError("Command input exceeds 16384 bytes")
    try:
        code, stdout, stderr = courier.run_command(list(argv), 30, payload)
    except TimeoutError as error:
        raise RemoteError("Remote command timed out") from error
    except (courier.NotStarted, OSError) as error:
        raise RemoteError("Remote command did not start") from error
    if max(len(stdout), len(stderr)) > courier.MAX_RECORD:
        raise RemoteError("Remote command output exceeds 8192 bytes")
    return code, stdout, stderr


def _cmd_receive(args):
    raw = sys.stdin.buffer.read(courier.MAX_DATA_RECORD + 1)
    try:
        ack = receive_bytes(args.state, raw, args.expect_kind, args.expect_id)
    except RemoteError as error:
        sys.stderr.write(str(error)[:200] + "\n")
        return 1
    sys.stdout.buffer.write(canonical(ack) + b"\n")
    return 0


def _cmd_ssh_exec(args):
    raw = sys.stdin.buffer.read(courier.MAX_DATA_RECORD + 1)
    try:
        argv = load_peer(args.peer)
        code, stdout, stderr = forward(argv, raw)
    except RemoteError as error:
        sys.stderr.write(str(error)[:200] + "\n")
        return 1
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr[:courier.MAX_RECORD])
    return code


def _cmd_complete(args):
    try:
        secret = _load_secret(args.secret_file)
        reports = complete(args.state, args.config, args.recipient, args.key_id, secret)
    except (RemoteError, courier.Refused, OSError) as error:
        sys.stderr.write(str(error)[:200] + "\n")
        return 1
    sys.stdout.buffer.write(canonical({"completed": reports}) + b"\n")
    return 0


def _cmd_verify(args):
    raw = sys.stdin.buffer.read(courier.MAX_DATA + 1)
    try:
        secret = _load_secret(args.secret_file)
        body = verify_result(_loads(raw), args.key_id, secret)
    except RemoteError as error:
        sys.stderr.write(str(error)[:200] + "\n")
        return 1
    sys.stdout.buffer.write(canonical({"verified": True, "body": body}) + b"\n")
    return 0


def _cmd_ssh_argv(args):
    document = _loads(Path(args.peer).read_bytes())
    if set(document) != {"ssh"}:
        raise RemoteError("Peer file must contain ssh")
    argv = ssh_argv(document["ssh"])
    sys.stdout.buffer.write(canonical({"argv": argv}) + b"\n")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    receive = commands.add_parser("receive")
    receive.add_argument("--state", required=True)
    receive.add_argument("--expect-kind", required=True)
    receive.add_argument("--expect-id", required=True)
    forwarder = commands.add_parser("ssh-exec")
    forwarder.add_argument("--peer", required=True)
    finished = commands.add_parser("complete")
    finished.add_argument("--state", required=True)
    finished.add_argument("--config", required=True)
    finished.add_argument("--recipient", required=True)
    finished.add_argument("--key-id", required=True)
    finished.add_argument("--secret-file", required=True)
    verify = commands.add_parser("verify-result")
    verify.add_argument("--key-id", required=True)
    verify.add_argument("--secret-file", required=True)
    shown = commands.add_parser("ssh-argv")
    shown.add_argument("--peer", required=True)
    args = parser.parse_args(argv)
    if args.command == "receive":
        return _cmd_receive(args)
    if args.command == "ssh-exec":
        return _cmd_ssh_exec(args)
    if args.command == "complete":
        return _cmd_complete(args)
    if args.command == "verify-result":
        return _cmd_verify(args)
    if args.command == "ssh-argv":
        return _cmd_ssh_argv(args)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RemoteError as error:
        sys.stderr.write(str(error)[:200] + "\n")
        raise SystemExit(1)
