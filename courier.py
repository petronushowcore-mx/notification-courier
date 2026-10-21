"""Local notification courier with explicit transport receipts.

The file inbox and command adapter are runnable. A native urgent transport is
an optional host-supplied callable; no application IPC code is bundled.
"""
import argparse
import base64
import errno
import hashlib
import math
from contextlib import contextmanager, nullcontext
import uuid
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
PREFIX = re.compile(r"[0-9a-f][0-9a-f-]{7,35}")
KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}")
ENDPOINT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MAX_DATA = 6144
MAX_DATA_RECORD = 16384
MAX_RECORD = 8192
MAX_BATCH = 64
TERMINAL = {"STORED", "QUEUED", "THREAD_ACCEPTED", "REFUSED", "OUTCOME_UNKNOWN"}


class Refused(Exception):
    """Explicit refusal; generic adapter exceptions do not establish no dispatch."""


class NotStarted(Refused):
    """Child-creation failure claim; custom runners cannot establish it."""


@dataclass(frozen=True)
class Address:
    client: str
    identity: str

    def __post_init__(self):
        if type(self.client) is not str or type(self.identity) is not str:
            raise Refused("Address fields must be strings")
        if self.client in ("claude", "codex"):
            if not ID.fullmatch(self.identity):
                raise Refused("Legacy address requires a full lowercase UUID")
        elif not KIND.fullmatch(self.client) or not ENDPOINT_ID.fullmatch(self.identity):
            raise Refused("Configured address needs a bounded kind and a safe local id")


@dataclass(frozen=True)
class Receipt:
    status: str
    detail: str
    recipient_id: str = None
    queue_item_id: str = None
    held_status: str = None
    recipient_kind: str = None
    attempt_id: str = None

    def as_dict(self):
        result = dict(status=self.status, detail=self.detail,
                      recipient_id=self.recipient_id, queue_item_id=self.queue_item_id,
                      held_status=self.held_status, read="UNMEASURED")
        if self.recipient_kind is not None:
            result["recipient_kind"] = self.recipient_kind
        if self.attempt_id is not None:
            result["attempt_id"] = self.attempt_id
        return result


def failure_detail(error):
    """Keep plain local diagnostics without invoking supplied exception methods."""
    if type(error) is Refused or type(error) is NotStarted or type(error) is ValueError:
        args = error.args
        if len(args) == 1 and type(args[0]) is str:
            detail = args[0][:300]
            try:
                detail.encode("utf-8")
            except UnicodeEncodeError:
                pass
            else:
                return detail
    return "Exception detail not copied; inspect state"


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextmanager
def file_lock(path):
    """Cooperating processes lock a permanent file: one byte on Windows, flock on POSIX."""
    path = Path(str(path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + 30
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            lock = lambda: msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            unlock = lambda: msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(fd, fcntl.LOCK_UN)
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                lock()
                acquired = True
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise Refused("Local state lock timed out") from error
                time.sleep(0.01)
        yield
    finally:
        if acquired:
            os.lseek(fd, 0, os.SEEK_SET)
            unlock()
        os.close(fd)


def encoded_record(row, limit=MAX_RECORD):
    encoded = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > limit:
        raise Refused("Record exceeds %s bytes" % limit)
    return encoded


def append_record(path, row, limit=MAX_RECORD):
    encoded = encoded_record(row, limit)
    with file_lock(path):
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_APPEND |
                     getattr(os, "O_BINARY", 0), 0o600)
        try:
            size = os.fstat(fd).st_size
            if size:
                os.lseek(fd, -1, os.SEEK_END)
            if size and os.read(fd, 1) != b"\n":
                raise Refused("Incomplete append tail; inspect state before writing")
            if os.write(fd, encoded) != len(encoded):
                raise OSError("Incomplete append; outcome needs inspection")
            os.fsync(fd)
        finally:
            os.close(fd)


def records(path):
    if not path.exists():
        return
    with file_lock(path), path.open("rb") as stream:
        while True:
            raw = stream.readline(MAX_RECORD + 1)
            if not raw:
                return
            if len(raw) > MAX_RECORD or not raw.endswith(b"\n"):
                raise Refused("Incomplete or oversized journal record; inspect state")
            row = json.loads(raw.decode("utf-8"))
            if not isinstance(row, dict):
                raise Refused("Invalid journal record; inspect state")
            yield row


def same_address(left, right):
    return left == right


def held_outcome(rows, target, key):
    attempts = {}
    for row in rows:
        if (row.get("client"), row.get("id"), row.get("key")) != (
                target.client, target.identity, key):
            continue
        attempt = row.get("attempt", "legacy")
        if (row.get("status") == "REFUSED" and row.get("dispatch") == "NOT_STARTED"
                and attempt != "legacy"):
            attempts.pop(attempt, None)
        elif row.get("status") in TERMINAL | {"REQUESTED", "RESERVED"}:
            attempts[attempt] = row
    return next(reversed(attempts.values()), None)


def blocked(rows, target, key):
    return held_outcome(rows, target, key) is not None


class Inbox:
    """Legacy letters/ring/numeric offset plus a bound identity sidecar."""

    record_limit = MAX_RECORD

    def __init__(self, state):
        self.root = Path(state) / "inbox"

    @staticmethod
    def valid_row(row, target):
        return isinstance(row, dict) and isinstance(row.get("line"), str)

    def paths(self, target):
        if target.client != "claude":
            raise Refused("File inbox expects a Claude address")
        return {part: self.root / (target.identity + "." + part)
                for part in ("jsonl", "ring", "offset", "identity")}

    def __call__(self, target, line, requested):
        paths = self.paths(target)
        requested()
        append_record(paths["jsonl"], dict(ts=now(), line=line))
        paths["ring"].touch()
        return Receipt("STORED", "Letter stored; host wake and reading are separate",
                       target.identity)

    @staticmethod
    def fingerprint(stream, end):
        stat = os.fstat(stream.fileno())
        position = stream.tell()
        stream.seek(0)
        prefix = stream.read(min(end, 4096))
        stream.seek(position)
        return {"file": [stat.st_dev, stat.st_ino], "offset": end,
                "prefix_sha256": hashlib.sha256(prefix).hexdigest()}

    def _unread(self, target):
        paths = self.paths(target)
        try:
            start = int(paths["offset"].read_text(encoding="utf-8"))
        except FileNotFoundError:
            start = 0
        if start < 0:
            raise Refused("Invalid inbox offset")
        try:
            identity = json.loads(paths["identity"].read_text(encoding="utf-8"))
        except FileNotFoundError:
            identity = None
        if (start and identity is None) or (identity is not None and (
                not isinstance(identity, dict) or identity.get("offset") != start)):
            raise Refused("Inbox cursor is unbound; operator recovery required")
        letters, end = [], start
        if not paths["jsonl"].exists():
            if start or identity is not None:
                raise Refused("Inbox missing after a recorded cursor")
            return letters, end, None
        with file_lock(paths["jsonl"]), paths["jsonl"].open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            if start > stream.tell():
                raise Refused("Inbox truncated; inspect offset before reading")
            if identity is not None and identity != self.fingerprint(stream, start):
                raise Refused("Inbox identity or consumed prefix changed")
            stream.seek(start)
            while len(letters) < MAX_BATCH:
                raw = stream.readline(self.record_limit + 1)
                if len(raw) > self.record_limit:
                    raise Refused("Oversized inbox record")
                if not raw or not raw.endswith(b"\n"):
                    break
                row = json.loads(raw.decode("utf-8"))
                if not self.valid_row(row, target):
                    raise Refused("Invalid inbox record")
                letters.append(row)
                end = stream.tell()
            return letters, end, self.fingerprint(stream, end)

    def unread(self, target):
        with file_lock(self.paths(target)["offset"]):
            letters, end, _ = self._unread(target)
            return letters, end

    @staticmethod
    def save_cursor(path, text):
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def show(self, target, event="UserPromptSubmit", out=None):
        out = sys.stdout if out is None else out
        paths = self.paths(target)
        with file_lock(paths["offset"]):
            letters, end, identity = self._unread(target)
            if not letters:
                return 0
            text = "Notifications:\n" + "\n".join(row["line"] for row in letters)
            out.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": event, "additionalContext": text}},
                ensure_ascii=True) + "\n")
            out.flush()
            # A crash between these writes leaves an explicit cursor mismatch.
            self.save_cursor(paths["identity"], json.dumps(identity))
            self.save_cursor(paths["offset"], str(end))
            return len(letters)

    def watch(self, target, timeout=None, poll=2.0):
        if (not math.isfinite(poll) or poll <= 0 or
                timeout is not None and (not math.isfinite(timeout) or timeout < 0)):
            raise Refused("Invalid watch interval")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.unread(target)[0]:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            try:
                time.sleep(poll)
            except OverflowError:
                raise Refused("Invalid watch interval") from None


def run_command(command, timeout, input_bytes=None):
    """Wait for the direct child, with file output rather than descendant pipes."""
    if input_bytes is not None and (type(input_bytes) is not bytes or len(input_bytes) > MAX_DATA_RECORD):
        raise Refused("Invalid bounded command input")
    input_context = (tempfile.TemporaryFile() if input_bytes is not None else
                     nullcontext(subprocess.DEVNULL))
    with input_context as source, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        if input_bytes is not None:
            source.write(input_bytes)
            source.seek(0)
        try:
            process = subprocess.Popen(command, stdin=source,
                                       stdout=stdout, stderr=stderr, shell=False)
        except OSError as error:
            raise NotStarted("Command not started: " + str(error)) from error
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
            raise TimeoutError("Command timed out; descendants are not managed") from None
        stdout.seek(0)
        stderr.seek(0)
        return code, stdout.read(MAX_RECORD + 1), stderr.read(MAX_RECORD + 1)


def command_prefix(command):
    """Resolve a configured executable without invoking a command shell."""
    command = list(command)
    executable = shutil.which(command[0]) or command[0]
    name = Path(executable).name.lower().rstrip(" .")
    if os.name == "nt" and name != Path(executable).name.lower():
        raise Refused("Ambiguous Windows executable name")
    command[0] = str(Path(executable).resolve())
    if name in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
                "bash", "bash.exe", "sh", "sh.exe"}:
        raise Refused("Shell interpreters need a separately authorised adapter")
    if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"):
        raise Refused("Windows batch wrappers need a separately authorised adapter")
    return command


class CodexQueue:
    """Configured executable only; exact receiver/item echo required."""

    def __init__(self, command, timeout=30, runner=None):
        if (not isinstance(command, list) or not command or
                any(not isinstance(x, str) or not x or "\x00" in x for x in command)):
            raise Refused("queue command must be a nonempty NUL-free argument list")
        if not 0 < timeout <= 120:
            raise Refused("queue timeout must be within 120 seconds")
        self.command, self.timeout = list(command), timeout
        self.runner = run_command if runner is None else runner

    def __call__(self, target, line, requested):
        if target.client != "codex":
            raise Refused("Queue expects a Codex address")
        if not isinstance(line, str) or "\x00" in line:
            raise Refused("Queue notice must be a NUL-free string")
        command = list(self.command)
        if self.runner is run_command:
            command = command_prefix(command)
        requested()
        runner = self.runner
        default_runner = runner is run_command
        try:
            result = runner(
                command + ["queue", "--thread", target.identity, "--message", line],
                self.timeout)
            if type(result) is not tuple and type(result) is not list:
                raise ValueError("Queue runner must return three plain values")
            if len(result) != 3:
                raise ValueError("Queue runner must return three plain values")
            code, stdout, stderr = result
            if type(code) is not int or type(stdout) is not bytes or type(stderr) is not bytes:
                raise ValueError("Queue runner requires int, bytes, bytes")
            if max(len(stdout), len(stderr)) > MAX_RECORD:
                return Receipt("OUTCOME_UNKNOWN", "Command output exceeds limit")
            detail = (stdout + stderr).decode("utf-8", "strict").strip()
            match = re.fullmatch(r"Queued message (" + ID.pattern +
                                 r") for thread (" + ID.pattern + r")\.", detail)
            if code == 0 and match and match.group(2) == target.identity:
                return Receipt("QUEUED", "Queue item acknowledged by the command",
                               target.identity, match.group(1))
            return Receipt("OUTCOME_UNKNOWN",
                           "No exact successful receiver/item echo (exit %s, output %r)" % (code, detail[:200]))
        except NotStarted as error:
            if not default_runner:
                raise Refused("Custom runner no-start is unverified") from error
            raise


@dataclass(frozen=True)
class Adapter:
    """Trusted local binding; capabilities alone do not prove receiver isolation."""
    transport: object
    capabilities: frozenset
    receipt_status: str = "STORED"

    def __post_init__(self):
        if (not callable(self.transport) or type(self.capabilities) is not frozenset or
                any(type(item) is not str for item in self.capabilities) or
                not self.capabilities <= {"notification", "data", "data_only"} or
                self.receipt_status not in {"STORED", "QUEUED", "THREAD_ACCEPTED"}):
            raise Refused("Invalid adapter binding")


class DataInbox(Inbox):
    """Opaque bytes with the existing cursor discipline; never emits hook context."""
    record_limit = MAX_DATA_RECORD

    def __init__(self, state):
        self.root = Path(state) / "data-inbox"

    def paths(self, target):
        key = hashlib.sha256(json.dumps([target.client, target.identity],
            ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {part: self.root / (key + "." + part)
                for part in ("jsonl", "offset", "identity")}

    @staticmethod
    def valid_row(row, target):
        if (type(row) is not dict or set(row) != {"ts", "recipient", "attempt", "payload"} or
                row["recipient"] != {"kind": target.client, "id": target.identity} or
                type(row["attempt"]) is not str or
                re.fullmatch(r"[0-9a-f]{32}", row["attempt"]) is None or
                type(row["ts"]) is not str or type(row["payload"]) is not str):
            return False
        try:
            payload = base64.b64decode(row["payload"], validate=True)
        except (ValueError, UnicodeError):
            return False
        return 0 < len(payload) <= MAX_DATA

    def __call__(self, target, payload, requested, attempt):
        if type(payload) is not bytes or not 0 < len(payload) <= MAX_DATA:
            raise Refused("Data payload must contain 1..6144 bytes")
        row = dict(ts=now(), recipient={"kind": target.client, "id": target.identity},
                   attempt=attempt, payload=base64.b64encode(payload).decode("ascii"))
        encoded_record(row, MAX_DATA_RECORD)
        requested()
        append_record(self.paths(target)["jsonl"], row, MAX_DATA_RECORD)
        return Receipt("STORED", "Opaque data stored; receiver handling is separate",
                       target.identity, recipient_kind=target.client, attempt_id=attempt)

    def show(self, target, out=None):
        out = sys.stdout if out is None else out
        paths = self.paths(target)
        with file_lock(paths["offset"]):
            rows, end, identity = self._unread(target)
            if not rows:
                return 0
            out.write(json.dumps({"messages": rows}, ensure_ascii=True) + "\n")
            out.flush()
            self.save_cursor(paths["identity"], json.dumps(identity))
            self.save_cursor(paths["offset"], str(end))
            return len(rows)


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON key")
        result[name] = value
    return result


class CommandAdapter:
    """Configured argv and bounded stdin; exact full recipient/attempt echo required."""
    def __init__(self, command, timeout=30, runner=None):
        validated = CodexQueue(command, timeout)
        self.command, self.timeout = validated.command, validated.timeout
        self.runner = run_command if runner is None else runner

    def __call__(self, target, payload, requested, attempt):
        if type(payload) is not bytes or not 0 < len(payload) <= MAX_DATA:
            raise Refused("Data payload must contain 1..6144 bytes")
        recipient = {"kind": target.client, "id": target.identity}
        wire = json.dumps(dict(recipient=recipient, attempt=attempt,
                         payload=base64.b64encode(payload).decode("ascii")),
                         ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(wire) > MAX_DATA_RECORD:
            raise Refused("Structured command input exceeds limit")
        command = (command_prefix(self.command) if self.runner is run_command
                   else list(self.command))
        requested()
        result = self.runner(command, self.timeout, wire)
        if (type(result) not in (tuple, list) or len(result) != 3 or
                type(result[0]) is not int or type(result[1]) is not bytes or
                type(result[2]) is not bytes):
            raise ValueError("Command runner requires int, bytes, bytes")
        code, stdout, stderr = result
        if code != 0 or max(len(stdout), len(stderr)) > MAX_RECORD:
            return Receipt("OUTCOME_UNKNOWN", "No bounded successful command response")
        try:
            reply = json.loads(stdout.decode("utf-8", "strict"), object_pairs_hook=unique_object)
        except (ValueError, UnicodeError, RecursionError):
            return Receipt("OUTCOME_UNKNOWN", "Malformed command response")
        if (type(reply) is not dict or set(reply) != {"status", "recipient", "attempt"} or
                reply["recipient"] != recipient or reply["attempt"] != attempt or
                reply["status"] not in {"STORED", "QUEUED", "THREAD_ACCEPTED"}):
            return Receipt("OUTCOME_UNKNOWN", "Missing exact full recipient/attempt echo")
        return Receipt(reply["status"], "Command acknowledged this recipient and attempt",
                       target.identity, recipient_kind=target.client, attempt_id=attempt)


class Courier:
    def __init__(self, state, recipients, queue=None, urgent=None, adapters=None):
        self.state = Path(state).resolve()
        self.recipients = dict(recipients)
        if not all(isinstance(k, str) and k and ":" not in k and
                   isinstance(v, Address) for k, v in self.recipients.items()):
            raise Refused("Invalid recipient registry")
        self.inbox, self.queue, self.urgent = Inbox(self.state), queue, urgent
        self.adapters = dict(adapters or {})
        if any(type(kind) is not str or not KIND.fullmatch(kind) or
               kind in ("claude", "codex") or type(binding) is not Adapter
               for kind, binding in self.adapters.items()):
            raise Refused("Configured transports cannot override built-in kinds")
        if any(x.client not in ("claude", "codex") and x.client not in self.adapters
               for x in self.recipients.values()):
            raise Refused("Every new endpoint kind needs a configured transport")

    @classmethod
    def from_config(cls, path):
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or set(data) - {
                "state_dir", "recipients", "codex_queue_command", "transports"}:
            raise Refused("Unknown configuration fields")
        if not isinstance(data.get("state_dir"), str) or not data["state_dir"]:
            raise Refused("Explicit state_dir required")
        registry = data.get("recipients")
        if not isinstance(registry, dict):
            raise Refused("recipients must be an object")
        recipients = {}
        for alias, entry in registry.items():
            if not isinstance(entry, dict) or set(entry) != {"client", "id"}:
                raise Refused("Recipient needs exactly client and id")
            recipients[alias] = Address(entry["client"], entry["id"])
        state = Path(data["state_dir"])
        if not state.is_absolute():
            state = path.parent / state
        bindings = {}
        transports = data.get("transports", {})
        if type(transports) is not dict:
            raise Refused("transports must be an object")
        for kind, definition in transports.items():
            if type(definition) is not dict:
                raise Refused("Transport definition must be an object")
            if definition == {"type": "inbox"}:
                bindings[kind] = Adapter(DataInbox(state), frozenset({"notification", "data"}))
            elif (definition.get("type") == "command" and set(definition) <=
                  {"type", "command", "timeout", "receipt_status", "data_only"}):
                modes = {"notification", "data"}
                if type(definition.get("data_only", False)) is not bool:
                    raise Refused("data_only must be a boolean")
                if definition.get("data_only", False):
                    modes.add("data_only")
                bindings[kind] = Adapter(CommandAdapter(definition.get("command"),
                    definition.get("timeout", 30)), frozenset(modes),
                    definition.get("receipt_status", "STORED"))
            else:
                raise Refused("Unsupported transport configuration")
        command = data.get("codex_queue_command")
        return cls(state, recipients, queue=CodexQueue(command) if command else None,
                   adapters=bindings)

    def resolve(self, recipient):
        if recipient in self.recipients:
            return self.recipients[recipient]
        client, _, prefix = (recipient.partition(":") if ":" in recipient
                             else ("", "", recipient))
        if client and client not in ("claude", "codex"):
            hits = {x for x in self.recipients.values()
                    if x.client == client and x.identity == prefix}
            if len(hits) != 1:
                raise Refused("Unknown configured address")
            return hits.pop()
        if not PREFIX.fullmatch(prefix):
            raise Refused("Unknown address")
        hits = {x for x in self.recipients.values()
                if x.client in ("claude", "codex") and
                (not client or x.client == client) and x.identity.startswith(prefix)}
        if len(hits) != 1:
            raise Refused("Address is missing or ambiguous")
        return hits.pop()

    def send(self, recipient, key, sender, note="", delivery=None, caller=None):
        target = self.resolve(recipient)
        if caller is not None and not isinstance(caller, Address):
            raise Refused("caller must be a canonical runtime address")
        if same_address(target, caller):
            return Receipt("SELF", "Same client and full identity")
        if not all(isinstance(x, str) for x in (key, sender, note)):
            raise Refused("Notification fields must be strings")
        if not key or not sender:
            raise Refused("Notification key and sender must not be empty")
        if len(key) > 256 or len(sender) > 100 or len(note) > 1024:
            raise Refused("Notification fields exceed declared limits")
        if any("\x00" in x for x in (key, sender, note)):
            raise Refused("Notification fields must not contain NUL")
        if delivery not in (None, "queue", "urgent"):
            return Receipt("REFUSED", "Unsupported delivery mode")
        if target.client == "claude":
            if delivery is not None:
                return Receipt("REFUSED", "Explicit Claude queue/urgent is unsupported")
        return self._dispatch(target, key, sender, note, delivery)

    def send_data(self, recipient, key, carrier, caller=None, data_only=False):
        """Dispatch opaque bounded bytes; no notice wrapping or implied authority."""
        target = self.resolve(recipient)
        if caller is not None and not isinstance(caller, Address):
            raise Refused("caller must be a canonical runtime address")
        if same_address(target, caller):
            return Receipt("SELF", "Same client and full identity")
        if type(key) is not str or not key or len(key) > 256 or "\x00" in key:
            raise Refused("Data key must be nonempty, NUL-free and at most 256 characters")
        if type(carrier) is not bytes or not 0 < len(carrier) <= MAX_DATA:
            raise Refused("Data carrier must contain 1..6144 bytes")
        if type(data_only) is not bool:
            raise Refused("data_only must be a boolean")
        return self._dispatch(target, key, None, None, None, carrier, data_only)

    def _dispatch(self, target, key, sender, note, delivery, carrier=None, data_only=False):
        lock_key = hashlib.sha256(json.dumps(
            [target.client, target.identity, key], ensure_ascii=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        with file_lock(self.state / "send" / lock_key):
            return self._send_locked(target, key, sender, note, delivery, carrier, data_only)

    def _send_locked(self, target, key, sender, note, delivery, carrier=None, data_only=False):
        journal = self.state / "journal.jsonl"
        held = held_outcome(records(journal), target, key)
        if held is not None:
            return Receipt("SKIPPED", "Held " + held["status"] + ": " +
                           str(held.get("detail", ""))[:200], held_status=held["status"])
        base = dict(client=target.client, id=target.identity, key=key,
                    attempt=uuid.uuid4().hex)
        requested_count = 0
        adapter_called = False
        not_started = False
        line = ("Check %s from %s." % (key, sender) + (" " + note if note else "")
                if carrier is None else carrier)
        # Validate serialized bytes before any reservation or transport call.
        if carrier is None:
            encoded_record(dict(ts=now(), line=line))
        encoded_record(dict(base, ts=now(), status="REQUESTED"))

        def requested():
            nonlocal requested_count
            if requested_count:
                raise ValueError("Transport requested twice")
            append_record(journal, dict(base, ts=now(), status="REQUESTED"))
            requested_count += 1

        adapter = None

        def default_queue():
            return type(adapter) is CodexQueue and adapter.runner is run_command

        try:
            configured = target.client not in ("claude", "codex")
            if configured:
                # No supplied binding lookup or call precedes the durable hold.
                append_record(journal, dict(base, ts=now(), status="RESERVED"))
                binding = self.adapters.get(target.client)
                if binding is None:
                    raise Refused("No configured transport for this kind")
                mode = "notification" if carrier is None else "data"
                if delivery is not None or mode not in binding.capabilities:
                    raise Refused("Adapter does not support this delivery mode")
                if data_only and "data_only" not in binding.capabilities:
                    raise Refused("Receiver has no configured data-only capability")
                adapter, expected_status = binding.transport, binding.receipt_status
                payload = line.encode("utf-8") if carrier is None else carrier
                if len(payload) > MAX_DATA:
                    raise Refused("Configured transport payload exceeds 6144 bytes")
            else:
                if carrier is not None:
                    raise Refused("Legacy prompt transport cannot carry data-only requests")
                if target.client == "claude":
                    adapter = self.inbox
                else:
                    adapter = self.urgent if delivery == "urgent" else self.queue
                    if adapter is None:
                        raise Refused("No configured transport for this mode")
                expected_status = ("STORED" if target.client == "claude" else
                                   "THREAD_ACCEPTED" if delivery == "urgent" else "QUEUED")
                # Exact built-ins reserve before effects; supplied code needs an earlier hold.
                if type(adapter) is not Inbox and type(adapter) is not CodexQueue:
                    append_record(journal, dict(base, ts=now(), status="RESERVED"))
            adapter_called = True
            receipt = (adapter(target, payload, requested, base["attempt"]) if configured else
                       adapter(target, line, requested))
            if type(receipt) is not Receipt:
                raise ValueError("Invalid transport receipt type")
            values = (receipt.status, receipt.detail, receipt.recipient_id,
                      receipt.queue_item_id, receipt.held_status,
                      receipt.recipient_kind, receipt.attempt_id)
            if (type(values[0]) is not str or type(values[1]) is not str or
                    any(value is not None and type(value) is not str for value in values[2:])):
                raise ValueError("Invalid transport receipt fields")
            receipt = Receipt(*values)
            if receipt.status not in TERMINAL:
                raise ValueError("Invalid transport receipt status")
            try:
                encoded_record(dict(base, ts=now(), dispatch="POSSIBLE", **receipt.as_dict()))
            except Refused:
                raise ValueError("Invalid transport receipt size") from None
            if receipt.status in {"STORED", "QUEUED", "THREAD_ACCEPTED"} and (
                    requested_count != 1 or receipt.recipient_id != target.identity or
                    receipt.status != expected_status):
                raise ValueError("Missing reservation, receiver echo or compatible mode")
            if configured and receipt.status in {"STORED", "QUEUED", "THREAD_ACCEPTED"} and (
                    receipt.recipient_kind != target.client or receipt.attempt_id != base["attempt"]):
                raise ValueError("Missing exact configured address or attempt echo")
        except NotStarted as error:
            not_started = type(adapter) is CodexQueue and isinstance(error.__cause__, OSError)
            receipt = Receipt("REFUSED", failure_detail(error))
        except Refused as error:
            not_started = not adapter_called or (
                requested_count == 0 and (default_queue() or type(adapter) is Inbox))
            receipt = Receipt("REFUSED", failure_detail(error))
        except Exception as error:
            receipt = Receipt("OUTCOME_UNKNOWN" if adapter_called or requested_count else "REFUSED",
                              failure_detail(error))
        try:
            append_record(journal, dict(base, ts=now(), dispatch="NOT_STARTED" if not_started else "POSSIBLE",
                                        **receipt.as_dict()))
        except Exception as error:
            return Receipt("REFUSED" if receipt.status == "REFUSED" else "OUTCOME_UNKNOWN",
                           "Final receipt not recorded: " + failure_detail(error)[:200])
        return receipt


    def reader(self, target):
        if target.client == "claude":
            return self.inbox
        binding = self.adapters.get(target.client)
        if binding is None or type(binding.transport) is not DataInbox:
            raise Refused("This endpoint has no local readable inbox")
        return binding.transport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send")
    send.add_argument("recipient")
    send.add_argument("--key", required=True)
    send.add_argument("--sender", required=True)
    send.add_argument("--note", default="")
    send.add_argument("--delivery", choices=("queue", "urgent"))
    data = commands.add_parser("send-data")
    data.add_argument("recipient")
    data.add_argument("--key", required=True)
    data.add_argument("--file", required=True)
    data.add_argument("--data-only", action="store_true")
    show = commands.add_parser("show")
    show.add_argument("recipient")
    show.add_argument("--event", default="UserPromptSubmit")
    watch = commands.add_parser("watch")
    watch.add_argument("recipient")
    watch.add_argument("--timeout", type=float)
    watch.add_argument("--poll", type=float, default=2)
    args = parser.parse_args()
    receipt = None
    try:
        courier = Courier.from_config(args.config)
        if args.command in ("send", "send-data"):
            if args.command == "send":
                receipt = courier.send(args.recipient, args.key, args.sender,
                                       args.note, args.delivery)
            else:
                with Path(args.file).open("rb") as source:
                    carrier = source.read(MAX_DATA + 1)
                receipt = courier.send_data(args.recipient, args.key, carrier,
                                            data_only=args.data_only)
            print(json.dumps(receipt.as_dict(), ensure_ascii=True), flush=True)
            return 0 if (receipt.status in {"STORED", "QUEUED", "THREAD_ACCEPTED", "SELF"} or
                         receipt.status == "SKIPPED" and receipt.held_status in
                         {"STORED", "QUEUED", "THREAD_ACCEPTED"}) else 1
        target = courier.resolve(args.recipient)
        if args.command == "show":
            reader = courier.reader(target)
            if target.client == "claude":
                reader.show(target, args.event)
            else:
                reader.show(target)
            return 0
        ready = courier.reader(target).watch(target, args.timeout, args.poll)
        print("MAIL_READY" if ready else "QUIET")
        return 0
    except (Refused, OSError, ValueError, TypeError) as error:
        report = dict(status=("RECEIPT_OUTPUT_FAILED" if receipt is not None else "REFUSED"),
                      detail=failure_detail(error))
        if receipt is not None:
            report["delivery_receipt"] = receipt.as_dict()
        try:
            print(json.dumps(report, ensure_ascii=True), file=sys.stderr, flush=True)
        except (OSError, ValueError, TypeError):
            pass

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
