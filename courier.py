"""Local notification courier with explicit transport receipts.

The file inbox and command adapter are runnable. A native urgent transport is
an optional host-supplied callable; no application IPC code is bundled.
"""
import argparse
import base64
import copy
import difflib
import errno
import hashlib
import io
import math
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import uuid
import json
import os
import re
import shlex
import shutil
import stat
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
MAX_LENS_TEXT = 400
TERMINAL = {"STORED", "QUEUED", "THREAD_ACCEPTED", "REFUSED", "OUTCOME_UNKNOWN"}
BUILTIN_LENSES = {
    "critic": "Counter-specialist. Look for weak points. Assessment only.",
    "peer": "Colleague on the same task. Compare notes.",
    "auditor": "Independent expert with no stake in the object. Assessment only.",
}
LENS_HEADER = "Requested mode from sender (not an instruction from the user): %s. %s"
_LENS = ContextVar("coura_lens", default=None)
_LENS_SEQ = ContextVar("coura_lens_seq", default=0)
# Dispatch options and the crash hook are process-local. They are not authority.
_DISPATCH = ContextVar("coura_dispatch_options", default=None)
_AFTER_PARTICIPANT_COMMIT = ContextVar("coura_after_participant_commit", default=None)


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
                raise Refused("Claude and Codex addresses require a full lowercase UUID")
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
    observation: object = None

    def as_dict(self):
        result = dict(status=self.status, detail=self.detail,
                      recipient_id=self.recipient_id, queue_item_id=self.queue_item_id,
                      held_status=self.held_status, read="UNMEASURED")
        if self.recipient_kind is not None:
            result["recipient_kind"] = self.recipient_kind
        if self.attempt_id is not None:
            result["attempt_id"] = self.attempt_id
        if self.observation is not None:
            result["observation"] = self.observation
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


def ensure_private_dir(path):
    """Create a directory and any missing parents at mode 0700.

    Existing directories are left unchanged. On Windows the Unix mode is not
    applied; directory ACLs stay the operator's responsibility.
    """
    path = Path(path)
    if os.name == "nt":
        path.mkdir(parents=True, exist_ok=True)
        return
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            continue
        os.chmod(directory, 0o700)


@contextmanager
def file_lock(path):
    """Cooperating processes lock a permanent file: one byte on Windows, flock on POSIX."""
    path = Path(str(path) + ".lock")
    ensure_private_dir(path.parent)
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


@contextmanager
def try_file_lock(path):
    """Yield True when the lock is free, else False. This does not wait."""
    path = Path(str(path) + ".lock")
    ensure_private_dir(path.parent)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
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
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            lock()
            acquired = True
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
        yield acquired
    finally:
        if acquired:
            os.lseek(fd, 0, os.SEEK_SET)
            unlock()
        os.close(fd)


ESCAPED_LINE = "[escaped] "


def print_text(out, text):
    """Write text for a person and flush. A line the stream cannot encode, and a line that
    begins with the reserved marker ESCAPED_LINE, is written as that marker followed by the
    line escaped: backslashes doubled, missing characters as \\xNN, \\uNNNN or \\UNNNNNNNN.
    Every other line is written as is. The marker alone tells the two apart, and an escaped
    line reads back exactly. Streams without an encoding take the text as is; write and flush
    errors propagate."""
    encoding = getattr(out, "encoding", None)
    if encoding:
        lines = text.split("\n")
        for index, line in enumerate(lines):
            try:
                line.encode(encoding)
                if not line.startswith(ESCAPED_LINE):
                    continue
            except UnicodeEncodeError:
                pass
            lines[index] = ESCAPED_LINE + line.replace("\\", "\\\\").encode(
                encoding, "backslashreplace").decode(encoding)
        text = "\n".join(lines)
    out.write(text)
    out.flush()


def encoded_record(row, limit=MAX_RECORD):
    encoded = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > limit:
        raise Refused("Record exceeds %s bytes" % limit)
    return encoded


def _finish_append(fd, encoded):
    size = os.fstat(fd).st_size
    if size:
        os.lseek(fd, -1, os.SEEK_END)
    if size and os.read(fd, 1) != b"\n":
        raise Refused("Incomplete append tail; inspect state before writing")
    start = os.lseek(fd, 0, os.SEEK_END)
    if os.write(fd, encoded) != len(encoded):
        raise OSError("Incomplete append; outcome needs inspection")
    os.fsync(fd)
    return start


def _open_append(path):
    return os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_APPEND |
                   getattr(os, "O_BINARY", 0), 0o600)


def append_record(path, row, limit=MAX_RECORD):
    encoded = encoded_record(row, limit)
    with file_lock(path):
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_APPEND |
                     getattr(os, "O_BINARY", 0), 0o600)
        try:
            return _finish_append(fd, encoded)
        finally:
            os.close(fd)


def append_record_while_held(path, row, limit=MAX_RECORD):
    """Append while the caller already holds file_lock(path). Does not lock again."""
    encoded = encoded_record(row, limit)
    fd = _open_append(path)
    try:
        return _finish_append(fd, encoded)
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
        # Rows written before attempt ids omit attempt. They share one bucket,
        # and a refusal in that bucket stays held.
        attempt = row.get("attempt", "unspecified")
        if (row.get("status") == "REFUSED" and row.get("dispatch") == "NOT_STARTED"
                and attempt != "unspecified"):
            attempts.pop(attempt, None)
        elif row.get("status") in TERMINAL | {"REQUESTED", "RESERVED"}:
            attempts[attempt] = row
    return next(reversed(attempts.values()), None)


class Inbox:
    """Claude session inbox: JSON-lines letters, a numeric offset and an identity sidecar.

    After each append, an empty sibling ``.ring`` file is touched so an external
    host can watch one path. ``show`` and ``watch`` read the JSON-lines file and
    the cursor. They do not read the ring, and the ring is not a second copy of
    the letter.
    """

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

    def show(self, target, event="UserPromptSubmit", out=None, text=False):
        out = sys.stdout if out is None else out
        paths = self.paths(target)
        with file_lock(paths["offset"]):
            letters, end, identity = self._unread(target)
            if not letters:
                return 0
            if text:
                print_text(out, "\n".join(row["line"] for row in letters) + "\n")
                self.save_cursor(paths["identity"], json.dumps(identity))
                self.save_cursor(paths["offset"], str(end))
                return len(letters)
            text = "Notifications:\n" + "\n".join(row["line"] for row in letters)
            out.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": event, "additionalContext": text}},
                ensure_ascii=True) + "\n")
            out.flush()
            # A crash between these writes leaves an explicit cursor mismatch.
            self.save_cursor(paths["identity"], json.dumps(identity))
            self.save_cursor(paths["offset"], str(end))
            return len(letters)

    def watch(self, target, timeout=None, poll=2.0, on_ready=None):
        if (not math.isfinite(poll) or poll <= 0 or
                timeout is not None and (not math.isfinite(timeout) or timeout < 0)):
            raise Refused("Invalid watch interval")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.unread(target)[0]:
                if on_ready is not None:
                    try:
                        on_ready()
                    except Exception:
                        pass
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
    """Trusted local binding. ``data_only`` is a receiver hint, not a checked safety mode."""
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

    @staticmethod
    def payload_text(encoded):
        """Decode one stored payload for a human reader. Invalid UTF-8 stays marked."""
        raw = base64.b64decode(encoded, validate=True)
        try:
            return raw.decode("utf-8")
        except UnicodeError:
            return "[payload is not UTF-8 text]\n" + encoded

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

    def show(self, target, out=None, text=False):
        out = sys.stdout if out is None else out
        paths = self.paths(target)
        with file_lock(paths["offset"]):
            rows, end, identity = self._unread(target)
            if not rows:
                return 0
            if text:
                print_text(out, "\n\n".join(self.payload_text(row["payload"]) for row in rows) + "\n")
            else:
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


def lens_header(name, text):
    """One labeled line. Sender-controlled text, not a user or system instruction."""
    return LENS_HEADER % (name, text)


def apply_lens_header(line, carrier, lens):
    """Prefix the delivered payload for every transport.

    Notification text gains a header line. Opaque bytes keep their exact suffix;
    the header is a separate leading line and the total must still fit the
    6144-byte data limit. No lens leaves both values unchanged.
    """
    header = lens_header(lens["name"], lens["text"])
    if carrier is None:
        return header + "\n" + line, carrier
    prefix = (header + "\n").encode("utf-8")
    if len(prefix) + len(carrier) > MAX_DATA:
        raise Refused("Lens header plus data exceeds 6144 bytes")
    merged = prefix + carrier
    return merged, merged


def _valid_lens_text(text):
    return (type(text) is str and text and len(text) <= MAX_LENS_TEXT and
            "\x00" not in text and "\n" not in text and "\r" not in text)


def load_lenses(data):
    """Built-ins plus config overrides. Defaults are sender->recipient aliases."""
    lenses = dict(BUILTIN_LENSES)
    raw = data.get("lenses", {})
    if type(raw) is not dict:
        raise Refused("lenses must be an object")
    for name, text in raw.items():
        if type(name) is not str or KIND.fullmatch(name) is None:
            raise Refused("Invalid lens name")
        if not _valid_lens_text(text):
            raise Refused("Lens text must be one line of at most %s characters" % MAX_LENS_TEXT)
        lenses[name] = text
    defaults = {}
    raw_defaults = data.get("lens_defaults", {})
    if type(raw_defaults) is not dict:
        raise Refused("lens_defaults must be an object")
    for key, name in raw_defaults.items():
        if (type(key) is not str or key.count("->") != 1 or
                any(part == "" or "\n" in part or "\r" in part or "\x00" in part
                    for part in key.split("->"))):
            raise Refused("Lens default key must be sender->recipient")
        if type(name) is not str or name not in lenses:
            raise Refused("Unknown lens")
        defaults[key] = name
    return lenses, defaults


def _lens_record(courier, name, origin, seq):
    if type(name) is not str or KIND.fullmatch(name) is None:
        raise Refused("Invalid lens name")
    text = courier.lenses.get(name)
    if not _valid_lens_text(text):
        raise Refused("Unknown lens")
    return {"name": name, "text": text, "origin": origin, "seq": seq}


def bind_lens(courier, name):
    """Arm an explicit lens for the next send or send-data in this context."""
    seq = _LENS_SEQ.get() + 1
    _LENS_SEQ.set(seq)
    _LENS.set(_lens_record(courier, name, "bind", seq))


def _prepare_lens_for_call(courier, sender=None, recipient=None, allow_default=False):
    """Keep a lens bound for this call. Drop one left behind by an earlier call.

    A default applies only to send, and only when this call did not bind one.
    """
    current = _LENS.get()
    seq = _LENS_SEQ.get() + 1
    _LENS_SEQ.set(seq)
    chosen = None
    if (current is not None and current.get("origin") == "bind" and
            current.get("seq") == seq - 1):
        chosen = current
    elif allow_default and sender is not None and recipient is not None:
        name = courier.lens_defaults.get("%s->%s" % (sender, recipient))
        if name:
            chosen = _lens_record(courier, name, "default", seq)
    _LENS.set(chosen)


def _observer_problem(observer, recipients):
    """Return an observer-config error, or None when the observer can be used.

    These errors are not delivery refusals. The participant send still runs.
    """
    if type(observer) is not dict:
        return "Invalid observer configuration"
    if set(observer) - {"destination", "include_body", "producer_id"}:
        return "Unknown observer fields"
    destination = observer.get("destination")
    if type(destination) is not str or destination not in recipients:
        return "Observer destination must be a configured recipient"
    if recipients[destination].client in ("claude", "codex"):
        return "Observer destination must be a configured data transport"
    include_body = observer.get("include_body", False)
    if type(include_body) is not bool:
        return "include_body must be a boolean"
    producer_id = observer.get("producer_id", None)
    if producer_id is not None and (type(producer_id) is not str or
                                    ENDPOINT_ID.fullmatch(producer_id) is None):
        return "Invalid observer producer_id"
    return None


def _checked_observer(observer, recipients):
    """None keeps Coura unchanged. A bad observer is reported, not a send refusal."""
    if observer is None:
        return None
    # from_config checks once, then Courier.__init__ checks again. A normalized
    # record must stay a report, not a second validation failure.
    if (type(observer) is dict and set(observer) ==
            {"destination", "include_body", "producer_id", "error"} and
            (observer.get("error") is None or type(observer.get("error")) is str) and
            type(observer.get("include_body")) is bool):
        return {"destination": observer.get("destination"),
                "include_body": observer.get("include_body"),
                "producer_id": observer.get("producer_id", None),
                "error": observer.get("error")}
    problem = _observer_problem(observer, recipients)
    if problem is not None:
        return {"destination": None, "include_body": False, "producer_id": None,
                "error": problem}
    return {"destination": observer.get("destination"),
            "include_body": observer.get("include_body", False),
            "producer_id": observer.get("producer_id", None),
            "error": None}


def _observer_config(data, recipients):
    if "observer" not in data:
        return None
    return _checked_observer(data.get("observer"), recipients)


def load_observer_module():
    """Load observer.py beside this file. Ordinary sends do not call this."""
    import importlib.util
    path = Path(__file__).with_name("observer.py")
    archive = str(path).replace("\\", "/")
    if not path.is_file() and ".pyz/" in archive:
        import observer as module
        module.bind(sys.modules[__name__])
        return module
    if not path.is_file():
        raise Refused("Observer support file is missing")
    name = "coura_observer_support"
    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        cached.bind(sys.modules[__name__])
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    module.bind(sys.modules[__name__])
    return module


def _attach_observation(courier, journal, offset, base, receipt, not_started, options, line):
    """Return a deferred observer status after the participant journal commit.

    Ordinary sends perform no observer import, file access or lock acquisition.
    A background observer recovers the committed fact. The test crash hook is
    process-local and does not alter the transport outcome.
    """
    emit = courier.observer is not None
    if type(options) is dict and options.get("emit_observation") is False:
        emit = False
    if Path(journal).name == "delivery.jsonl":
        emit = False
    if not emit:
        return receipt
    if courier.observer.get("error"):
        observation = {"status": "CAPTURE_FAILED", "detail": courier.observer["error"]}
    else:
        try:
            hook = _AFTER_PARTICIPANT_COMMIT.get()
            if hook is not None:
                hook()
            observation = {
                "status": "PENDING", "deferred": True,
                "detail": "Participant fact retained. observer-sync captures it; not an observer receipt.",
            }
        except Exception as error:
            observation = {"status": "CAPTURE_FAILED", "detail": failure_detail(error)}
    return Receipt(receipt.status, receipt.detail, receipt.recipient_id,
                   receipt.queue_item_id, receipt.held_status,
                   receipt.recipient_kind, receipt.attempt_id, observation)


class Courier:
    def __init__(self, state, recipients, queue=None, urgent=None, adapters=None,
                 lenses=None, lens_defaults=None, observer=None):
        self.state = Path(state).resolve()
        self.recipients = dict(recipients)
        if not all(isinstance(k, str) and k and ":" not in k and
                   isinstance(v, Address) for k, v in self.recipients.items()):
            raise Refused("Invalid recipient registry")
        self.inbox, self.queue, self.urgent = Inbox(self.state), queue, urgent
        self.lenses = dict(BUILTIN_LENSES if lenses is None else lenses)
        self.lens_defaults = dict(lens_defaults or {})
        self.adapters = dict(adapters or {})
        self.observer = _checked_observer(observer, self.recipients)
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
                "state_dir", "recipients", "codex_queue_command", "transports",
                "lenses", "lens_defaults", "observer"}:
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
                  {"type", "command", "timeout", "receipt_status", "data_only",
                   "receiver_hint"}):
                modes = {"notification", "data"}
                if type(definition.get("data_only", False)) is not bool:
                    raise Refused("data_only must be a boolean")
                if type(definition.get("receiver_hint", False)) is not bool:
                    raise Refused("receiver_hint must be a boolean")
                if ("data_only" in definition and "receiver_hint" in definition and
                        definition["data_only"] != definition["receiver_hint"]):
                    raise Refused("receiver_hint and data_only disagree")
                # Same operator hint under either name. Nothing here inspects the receiver.
                if definition.get("receiver_hint", definition.get("data_only", False)):
                    modes.add("data_only")
                bindings[kind] = Adapter(CommandAdapter(definition.get("command"),
                    definition.get("timeout", 30)), frozenset(modes),
                    definition.get("receipt_status", "STORED"))
            else:
                raise Refused("Unsupported transport configuration")
        command = data.get("codex_queue_command")
        lenses, lens_defaults = load_lenses(data)
        observer = _observer_config(data, recipients)
        return cls(state, recipients, queue=CodexQueue(command) if command else None,
                   adapters=bindings, lenses=lenses, lens_defaults=lens_defaults,
                   observer=observer)

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
        _prepare_lens_for_call(self, sender, recipient, allow_default=True)
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
        _prepare_lens_for_call(self)
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
        active_lens = _LENS.get()
        _LENS.set(None)
        journal = self.state / "journal.jsonl"
        held = held_outcome(records(journal), target, key)
        if held is not None:
            return Receipt("SKIPPED", "Held " + held["status"] + ": " +
                           str(held.get("detail", ""))[:200], held_status=held["status"])
        options = _DISPATCH.get()
        if type(options) is dict and options.get("journal"):
            journal = Path(options["journal"])
            held = held_outcome(records(journal), target, key)
            if held is not None:
                return Receipt("SKIPPED", "Held " + held["status"] + ": " +
                               str(held.get("detail", ""))[:200], held_status=held["status"])
        base = dict(client=target.client, id=target.identity, key=key,
                    attempt=uuid.uuid4().hex)
        if sender is not None:
            base["sender"] = sender
        if active_lens is not None:
            base["lens"] = active_lens["name"]
            base["lens_text"] = active_lens["text"]
        requested_count = 0
        adapter_called = False
        not_started = False
        line = ("Check %s from %s." % (key, sender) + (" " + note if note else "")
                if carrier is None else carrier)
        if active_lens is not None:
            line, carrier = apply_lens_header(line, carrier, active_lens)
        delivered = line.encode("utf-8") if type(line) is str else line
        if type(delivered) is bytes:
            base["attempted_body_sha256"] = hashlib.sha256(delivered).hexdigest()
        if type(options) is dict and type(options.get("facts")) is dict:
            base["facts"] = options["facts"]
        if type(options) is dict and type(options.get("logical")) is dict:
            base["logical"] = options["logical"]
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
                    raise Refused("This transport has no data_only receiver hint")
                adapter, expected_status = binding.transport, binding.receipt_status
                payload = line.encode("utf-8") if carrier is None else carrier
                if len(payload) > MAX_DATA:
                    raise Refused("Configured transport payload exceeds 6144 bytes")
            else:
                if carrier is not None:
                    raise Refused("Built-in Claude and Codex routes cannot carry a data payload")
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
        final_row = dict(base, ts=now(), dispatch="NOT_STARTED" if not_started else "POSSIBLE",
                         **receipt.as_dict())
        if (self.observer is not None and not self.observer.get("error")
                and self.observer.get("include_body") and receipt.status in {
                    "STORED", "QUEUED", "THREAD_ACCEPTED"} and type(line) is str
                and len(line.encode("utf-8")) <= 2048):
            copied = dict(final_row, observer_body=line)
            try:
                encoded_record(copied)
            except Refused:
                pass  # Optional copy must not exceed the participant record limit.
            else:
                final_row = copied
        try:
            committed = append_record(journal, final_row)
        except Exception as error:
            return Receipt("REFUSED" if receipt.status == "REFUSED" else "OUTCOME_UNKNOWN",
                           "Final receipt not recorded: " + failure_detail(error)[:200])
        receipt = _attach_observation(
            self, journal, committed, base, receipt, not_started, options, line)
        return receipt


    def reader(self, target):
        if target.client == "claude":
            return self.inbox
        binding = self.adapters.get(target.client)
        if binding is None or type(binding.transport) is not DataInbox:
            raise Refused("This endpoint has no local readable inbox")
        return binding.transport


def _quote_cmd_body(text):
    """Quote one argument the way CommandLineToArgvW reads quotes."""
    if text and not any(char in text for char in " \t\"&|<>()^"):
        return text
    out = ['"']
    backslashes = 0
    for char in text:
        if char == "\\":
            backslashes += 1
        else:
            if char == '"':
                out.append("\\" * (backslashes + 1))
            backslashes = 0
        out.append(char)
    out.append("\\" * backslashes)
    out.append('"')
    return "".join(out)


def quote_cmd_arg(text):
    """Quote one argument for cmd.exe, which then hands it to the child.

    cmd expands %VAR% even inside quotes, so each percent is left outside the
    quotes and escaped with ^. &, |, <, >, ^ and parentheses stay inside
    quotes, where cmd treats them as text. A value that itself contains "
    is caret-escaped throughout: cmd does not treat a backslash as a quote
    escape, so a raw quote would end the quoted span and run what follows.
    """
    text = str(text)
    quoted = _quote_cmd_body(text)
    if '"' not in text:
        if not quoted.startswith('"'):
            return quoted.replace("%", "^%")
        parts = []
        for segment in text.split("%"):
            trailing = len(segment) - len(segment.rstrip("\\"))
            parts.append(segment + ("\\" * trailing))
        return '"' + '"^%"'.join(parts) + '"'
    escaped = []
    for char in quoted:
        if char in "()%!^\"<>&|":
            escaped.append("^")
        escaped.append(char)
    return "".join(escaped)


def quote_arg(text, windows=None):
    """Quote one argument for the shell that runs a Claude Code hook."""
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return quote_cmd_arg(text)
    return shlex.quote(str(text))


def program_args():
    """Argv prefix for the source file, console command, or running zip archive."""
    archive = getattr(globals().get("__loader__"), "archive", None)
    if archive:
        return [sys.executable, str(Path(archive).resolve())]
    invoked = Path(sys.argv[0])
    if invoked.name.lower() in {"coura", "coura.exe"}:
        if invoked.is_file():
            return [str(invoked.resolve())]
        found = shutil.which(invoked.name)
        return [found if found else invoked.name]
    return [sys.executable, str(Path(__file__).resolve())]


def exec_form(argv, timeout=10):
    """Claude exec-form hook: one executable plus an argv, with no shell.

    The host documents shell form as sh -c, Git Bash, or PowerShell. Exec form
    passes each argument unchanged, which is the round-trip this installer uses.
    """
    if (type(argv) is not list or not argv or
            any(type(item) is not str or not item or "\x00" in item for item in argv)):
        raise Refused("Hook argv must be nonempty NUL-free strings")
    return {"type": "command", "command": argv[0], "args": list(argv[1:]), "timeout": timeout}


def show_hook_spec(config, recipient):
    return exec_form(program_args() + ["--config", str(Path(config).resolve()), "show", recipient])


def host_event_hook_spec(config, recipient, expect):
    """Stop records response-stopped. SessionEnd is a separate hook."""
    return exec_form(program_args() + [
        "--config", str(Path(config).resolve()),
        "host-event", recipient, "--expect", expect,
    ])


def quote_powershell_arg(text):
    """Literal PowerShell single quotes. A quote inside the value is doubled."""
    return "'" + str(text).replace("'", "''") + "'"


def show_hook_command(config, recipient):
    """Shell-form text for a POSIX sh -c hook. Not the installed exec form."""
    parts = program_args() + ["--config", str(Path(config).resolve()), "show", recipient]
    return " ".join(quote_arg(part, windows=False) for part in parts)


def claude_settings_path(user, settings, home=None):
    if settings and user:
        raise Refused("Pass either --user or --settings")
    if settings:
        return Path(settings)
    if user:
        base = Path.home() if home is None else Path(home)
        return base / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.local.json"


class HookConflict(Refused):
    """A settings hook is a Coura command for this recipient but is not ours."""

    def __init__(self, found, verb="show"):
        self.found = list(found)
        self.verb = verb
        Refused.__init__(self, "Conflicting Coura %s hook was left unchanged" % verb)


def _coura_program_names():
    names = {"coura", "courier.py", "coura.pyz"}
    archive = getattr(globals().get("__loader__"), "archive", None)
    if archive:
        names.add(Path(archive).name.lower())
    return names


def _is_coura_token(token):
    base = str(token).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base in _coura_program_names()


def _recipient_after_verb(tokens, verb):
    for index, token in enumerate(tokens):
        if token == verb and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _recipient_after_show(tokens):
    return _recipient_after_verb(tokens, "show")


def _split_posix(command):
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def _split_cmd(command):
    """Split a cmd.exe command line. None when a double quote is left open."""
    tokens, buf, quote, index = [], [], False, 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == '"':
                quote = False
            else:
                buf.append(char)
            index += 1
            continue
        if char == '"':
            quote = True
            index += 1
            continue
        if char in " \t":
            if buf:
                tokens.append("".join(buf))
                buf = []
            index += 1
            continue
        if char == "^" and index + 1 < len(command):
            buf.append(command[index + 1])
            index += 2
            continue
        buf.append(char)
        index += 1
    if quote:
        return None
    if buf:
        tokens.append("".join(buf))
    return tokens


def _looks_like_coura_show(command, recipient, verb="show"):
    names = "|".join(re.escape(name) for name in sorted(_coura_program_names()))
    if re.search(r"(^|[\s/\\])(?:" + names + r")(?:\.exe)?(?=[\s\"']|$)",
                 command, re.IGNORECASE) is None:
        return False
    return re.search(r"(?:^|\s)" + re.escape(verb) + r"\s+" + re.escape(recipient) +
                     r"(?:\s|$)", command) is not None


def _hook_relation(command, recipient, verb="show"):
    """Classify a hook command against the recipient we would install.

    'conflict' and 'ambiguous' must not be edited. 'other' is left untouched
    and does not block a new hook. Exact text is handled by the caller.
    The verb is `show` for a prompt hook. A Stop hook uses `host-event`.
    An older `send` for that same Stop recipient is still a conflict.
    """
    if type(command) is not str:
        return "other"
    parses = [tokens for tokens in (_split_posix(command), _split_cmd(command))
              if tokens is not None]
    if not parses:
        return "ambiguous" if _looks_like_coura_show(command, recipient, verb) else "other"
    saw_coura = False
    for tokens in parses:
        if not any(_is_coura_token(token) for token in tokens):
            continue
        saw_coura = True
        if _recipient_after_verb(tokens, verb) == recipient:
            return "conflict"
    if saw_coura or not _looks_like_coura_show(command, recipient, verb):
        return "other"
    return "ambiguous"


def _worst_relation(command, recipient, verbs):
    worst = "other"
    for verb in verbs:
        relation = _hook_relation(command, recipient, verb)
        if relation == "conflict":
            return "conflict"
        if relation == "ambiguous":
            worst = "ambiguous"
    return worst


def _argv_relation(argv, recipient, verbs):
    if not argv or not any(_is_coura_token(token) for token in argv):
        return "other"
    for verb in verbs:
        if _recipient_after_verb(argv, verb) == recipient:
            return "conflict"
    return "other"


def _hook_is_exact(hook, spec):
    return hook.get("command") == spec["command"] and hook.get("args") == spec["args"]


def merge_prompt_hook(settings, spec, recipient, event="UserPromptSubmit", verb="show", also_verbs=()):
    """Return a copied settings object and whether a new exact hook was added.

    A hook is ours only when its executable and args are exactly the exec-form
    hook we would write, including the absolute config path. Anything else that
    is a Coura command for this recipient and verb is a conflict: it is not
    replaced or removed. Shell-form commands are still detected.
    """
    if type(spec) is not dict or type(spec.get("command")) is not str or type(spec.get("args")) is not list:
        raise Refused("Hook spec must be exec form")
    verbs = (verb,) + tuple(also_verbs)
    if type(settings) is not dict:
        raise Refused("Claude settings must be a JSON object")
    settings = copy.deepcopy(settings)
    hooks = settings.get("hooks")
    if hooks is None:
        hooks = {}
        settings["hooks"] = hooks
    if type(hooks) is not dict:
        raise Refused("settings hooks must be an object")
    groups = hooks.get(event)
    if groups is None:
        groups = []
        hooks[event] = groups
    if type(groups) is not list:
        raise Refused("%s hooks must be a list" % event)
    exact = False
    conflicts = []
    for group in groups:
        if type(group) is not dict or type(group.get("hooks")) is not list:
            continue
        for hook in group["hooks"]:
            if type(hook) is not dict or hook.get("type") != "command":
                continue
            if _hook_is_exact(hook, spec):
                exact = True
                continue
            args = hook.get("args")
            if type(args) is list and all(type(item) is str for item in args) and type(hook.get("command")) is str:
                relation = _argv_relation([hook["command"], *args], recipient, verbs)
                shown = " ".join([hook["command"], *args])
            else:
                relation = _worst_relation(hook.get("command"), recipient, verbs)
                shown = hook.get("command") if type(hook.get("command")) is str else repr(hook.get("command"))
            if relation in ("conflict", "ambiguous"):
                conflicts.append((relation, shown))
    if conflicts:
        raise HookConflict(conflicts, verb)
    if exact:
        return settings, False
    groups.append({"hooks": [dict(spec)]})
    return settings, True


def render_settings(settings):
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def read_settings(path):
    if not path.exists():
        return {}, ""
    raw = path.read_text(encoding="utf-8-sig")
    if raw.strip() == "":
        return {}, raw
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise Refused("Claude settings are not JSON") from error
    if type(data) is not dict:
        raise Refused("Claude settings must be a JSON object")
    return data, raw


def settings_diff(path, before, after, existed):
    fromfile = "/dev/null" if not existed else str(path)
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=fromfile, tofile=str(path)))


def write_settings(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    previous = stat.S_IMODE(path.stat().st_mode) if existed and os.name != "nt" else None
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, os.fspath(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if os.name != "nt":
        os.chmod(path, previous if previous is not None else 0o600)


def _require_claude_hook_route(courier, recipient):
    """init installs a UserPromptSubmit hook. Only a Claude inbox show emits that JSON."""
    target = courier.resolve(recipient)
    if target.client != "claude":
        raise Refused(
            "Recipient %s is a %s route. init only installs a hook for a Claude "
            "session, because only that show output is Claude hook JSON."
            % (recipient, target.client))
    return target


def init_claude_hook(args):
    courier = Courier.from_config(args.config)
    _require_claude_hook_route(courier, args.recipient)
    if getattr(args, "on_stop", None):
        courier.resolve(args.on_stop)
    if getattr(args, "on_session_end", None):
        courier.resolve(args.on_session_end)
    path = claude_settings_path(args.user, args.settings)
    current, raw = read_settings(path)
    spec = show_hook_spec(args.config, args.recipient)
    try:
        updated, changed = merge_prompt_hook(current, spec, args.recipient)
        if getattr(args, "on_stop", None):
            stop_spec = host_event_hook_spec(args.config, args.on_stop, "Stop")
            updated, stop_changed = merge_prompt_hook(
                updated, stop_spec, args.on_stop, event="Stop", verb="host-event",
                also_verbs=("send",))
            changed = changed or stop_changed
        if getattr(args, "on_session_end", None):
            end_spec = host_event_hook_spec(args.config, args.on_session_end, "SessionEnd")
            updated, end_changed = merge_prompt_hook(
                updated, end_spec, args.on_session_end, event="SessionEnd",
                verb="host-event")
            changed = changed or end_changed
    except HookConflict as conflict:
        print_text(sys.stdout, "# conflict: %s\n" % path)
        print_text(sys.stdout, "# config: %s\n" % Path(args.config).resolve())
        print_text(sys.stdout, "# state_dir: %s\n" % courier.state)
        for kind, existing in conflict.found:
            print_text(sys.stdout, "# %s hook left unchanged:\n" % kind)
            print_text(sys.stdout, "%s\n" % (existing,))
        raise Refused("Conflicting Coura %s hook was left unchanged" % conflict.verb) from conflict
    if args.dry_run:
        if not changed:
            print_text(sys.stdout, "# unchanged: %s\n" % path)
            return
        diff = settings_diff(path, raw if path.exists() else "", render_settings(updated),
                             path.exists())
        print_text(sys.stdout, diff if diff else "# unchanged: %s\n" % path)
        return
    if not changed:
        print_text(sys.stdout, "unchanged %s\n" % path)
        return
    write_settings(path, render_settings(updated))
    print_text(sys.stdout, "updated %s\n" % path)


def clip_notify(text, limit):
    text = str(text).replace("\x00", "").replace("\r", " ").replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit]


def notify_plans(system, which, title, body):
    """Argv plans. Message text is an argument or an environment value, never a script fragment."""
    if system == "linux":
        binary = which("notify-send")
        if not binary:
            return []
        return [(["notify-send", "--", title, body], None)]
    if system == "darwin":
        binary = which("osascript")
        if not binary:
            return []
        script = (
            "on run argv\n"
            "display notification (item 2 of argv) with title (item 1 of argv)\n"
            "end run"
        )
        return [([binary, "-e", script, title, body], None)]
    if system == "win32":
        binary = which("powershell") or which("powershell.exe")
        if not binary:
            return []
        # Title and body are read from the environment. The script text is fixed.
        script = (
            "$t = $env:COURA_NOTIFY_TITLE\r\n"
            "$b = $env:COURA_NOTIFY_BODY\r\n"
            "try {\r\n"
            "  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\r\n"
            "  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\r\n"
            "  $xml = New-Object Windows.Data.Xml.Dom.XmlDocument\r\n"
            "  $safeT = [System.Security.SecurityElement]::Escape($t)\r\n"
            "  $safeB = [System.Security.SecurityElement]::Escape($b)\r\n"
            "  $xml.LoadXml(\"<toast><visual><binding template='ToastText02'><text id='1'>$safeT</text><text id='2'>$safeB</text></binding></visual></toast>\")\r\n"
            "  $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)\r\n"
            "  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Coura').Show($toast)\r\n"
            "} catch {\r\n"
            "  Add-Type -AssemblyName System.Windows.Forms\r\n"
            "  Add-Type -AssemblyName System.Drawing\r\n"
            "  $icon = New-Object System.Windows.Forms.NotifyIcon\r\n"
            "  $icon.Icon = [System.Drawing.SystemIcons]::Information\r\n"
            "  $icon.Visible = $true\r\n"
            "  $icon.ShowBalloonTip(5000, $t, $b, [System.Windows.Forms.ToolTipIcon]::Info)\r\n"
            "  Start-Sleep -Seconds 2\r\n"
            "  $icon.Dispose()\r\n"
            "}\r\n"
        )
        env = os.environ.copy()
        env["COURA_NOTIFY_TITLE"] = title
        env["COURA_NOTIFY_BODY"] = body
        return [([binary, "-NoProfile", "-NonInteractive", "-Command", script], env)]
    return []


def desktop_notify(title, body, runner=None, system=None, which=None):
    """Best-effort desktop notice. Failure is ignored by the caller."""
    title = clip_notify(title, 80)
    body = clip_notify(body, 240)
    system = sys.platform if system is None else system
    which = shutil.which if which is None else which
    plans = notify_plans(system, which, title, body)
    if not plans:
        return False

    def default_runner(argv, env):
        subprocess.run(argv, env=env, shell=False, check=False, timeout=8,
                       capture_output=True)

    run = default_runner if runner is None else runner
    for argv, env in plans:
        try:
            run(argv, env)
            return True
        except Exception:
            continue
    return False


def notify_unread(reader, target):
    letters = reader.unread(target)[0]
    chunks = []
    for row in letters[:5]:
        if type(row) is dict and type(row.get("line")) is str:
            chunks.append(row["line"])
        elif type(row) is dict and type(row.get("payload")) is str:
            chunks.append(DataInbox.payload_text(row["payload"]))
    desktop_notify("Coura", " | ".join(chunks) if chunks else "New mail")


def _paint(stream, code, text):
    if not hasattr(stream, "isatty") or not stream.isatty():
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def run_demo(out=None):
    """One temporary generic cycle: send, watch, show, duplicate send, delete."""
    out = sys.stdout if out is None else out
    holding = tempfile.TemporaryDirectory(prefix="coura-demo-")
    try:
        root = Path(holding.name)
        state = root / "state"
        config = root / "coura.json"
        config.write_text(json.dumps({
            "state_dir": str(state),
            "transports": {"worker": {"type": "inbox"}},
            "recipients": {
                "alpha": {"client": "worker", "id": "alpha"},
                "beta": {"client": "worker", "id": "beta"},
            },
            "lenses": {},
        }), encoding="utf-8")
        print_text(out, _paint(out, "1", "coura demo") + "\n")
        print_text(out, "generic inbox  alpha -> beta\n")
        print_text(out, "temporary state  %s\n" % state)
        note = "Hello from the demo."

        def step(argv):
            buffer = io.StringIO()
            previous = sys.stdout
            sys.stdout = buffer
            try:
                code = main(["--config", str(config), *argv])
            finally:
                sys.stdout = previous
            text = buffer.getvalue()
            if code != 0:
                raise Refused("demo step failed")
            return text

        sent = step(["send", "beta", "--key", "demo/hello", "--sender", "alpha",
                     "--note", note, "--lens", "peer"])
        print_text(out, _paint(out, "32", "send") + "  " + sent.strip() + "\n")
        watched = step(["watch", "beta", "--timeout", "0"])
        print_text(out, _paint(out, "32", "watch") + "  " + watched.strip() + "\n")
        shown = step(["show", "beta", "--text"])
        print_text(out, _paint(out, "32", "show --text") + "\n" + shown)
        again = step(["send", "beta", "--key", "demo/hello", "--sender", "alpha",
                      "--note", note])
        print_text(out, _paint(out, "33", "send again") + "  " + again.strip() + "\n")
        if "SKIPPED" not in again or "MAIL_READY" not in watched or note not in shown:
            raise Refused("demo cycle did not complete")
        print_text(out, "removed temporary state\n")
        return 0
    finally:
        holding.cleanup()


def _doctor_check(status, name, detail):
    return {"status": status, "name": name,
            "detail": " ".join(str(detail).split())[:300]}


def _hook_command_list(settings, event):
    found = []
    hooks = settings.get("hooks") if type(settings) is dict else None
    groups = hooks.get(event) if type(hooks) is dict else None
    if type(groups) is not list:
        return found
    for group in groups:
        if type(group) is not dict or type(group.get("hooks")) is not list:
            continue
        for hook in group["hooks"]:
            if type(hook) is dict and hook.get("type") == "command":
                found.append(hook.get("command"))
    return found


def _directory_writable(path):
    path = Path(path)
    if path.exists():
        return path.is_dir() and os.access(path, os.W_OK)
    parent = path.parent
    return parent.is_dir() and os.access(parent, os.W_OK)


def _first_executable(command):
    if type(command) is not list or not command or type(command[0]) is not str:
        return None
    return shutil.which(command[0])


def _probe_version(binary):
    try:
        done = subprocess.run([binary, "--version"], shell=False, check=False,
                              timeout=5, capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (done.stdout or done.stderr or b"").decode("utf-8", "replace").strip()
    line = text.splitlines()[0] if text else ""
    if done.returncode != 0 or not line:
        return None
    return line[:120]


def doctor(args):
    """Report pass, warn, and fail. Exit nonzero only when a check fails."""
    checks = []
    try:
        courier = Courier.from_config(args.config)
    except (Refused, OSError, ValueError) as error:
        checks.append(_doctor_check("fail", "config", failure_detail(error)))
        return _emit_doctor(args, checks)
    checks.append(_doctor_check("pass", "config", "parsed %s" % Path(args.config).resolve()))
    state = courier.state
    if not state.exists():
        checks.append(_doctor_check("warn", "state", "directory does not exist yet"))
    elif not state.is_dir():
        checks.append(_doctor_check("fail", "state", "not a directory"))
    elif os.name != "nt" and stat.S_IMODE(state.stat().st_mode) & 0o077:
        checks.append(_doctor_check(
            "fail", "state", "mode %04o is not private" % stat.S_IMODE(state.stat().st_mode)))
    elif not os.access(state, os.W_OK):
        checks.append(_doctor_check("fail", "state", "not writable"))
    elif os.name == "nt":
        checks.append(_doctor_check("pass", "state", "present; permission bits are not checked on Windows"))
    else:
        checks.append(_doctor_check("pass", "state", "private directory"))
    claude_aliases = []
    codex_aliases = []
    for alias, target in courier.recipients.items():
        try:
            resolved = courier.resolve(alias)
        except Refused as error:
            checks.append(_doctor_check("fail", "recipient:" + alias, failure_detail(error)))
            continue
        checks.append(_doctor_check(
            "pass", "recipient:" + alias, "%s:%s" % (resolved.client, resolved.identity)))
        if resolved.client == "claude":
            claude_aliases.append(alias)
            _doctor_inbox(checks, alias, state, state / "inbox", courier, resolved)
        elif resolved.client == "codex":
            codex_aliases.append(alias)
        else:
            binding = courier.adapters.get(resolved.client)
            transport = None if binding is None else binding.transport
            if type(transport) is DataInbox:
                _doctor_inbox(checks, alias, state, transport.root, courier, resolved)
            elif type(transport) is CommandAdapter:
                _doctor_command(checks, alias, transport)
            else:
                checks.append(_doctor_check(
                    "fail", "recipient:" + alias, "no configured transport"))
    if claude_aliases:
        _doctor_claude_hooks(checks, args, claude_aliases)
    if codex_aliases:
        _doctor_codex(checks, courier, codex_aliases)
    return _emit_doctor(args, checks)


def _doctor_inbox(checks, alias, state, directory, courier, target):
    if not state.exists():
        checks.append(_doctor_check("warn", "inbox:" + alias, "skipped because the state directory is absent"))
        checks.append(_doctor_check("warn", "watch:" + alias, "skipped because the state directory is absent"))
        return
    writable = _directory_writable(directory)
    if writable:
        checks.append(_doctor_check("pass", "inbox:" + alias, "writable"))
    else:
        checks.append(_doctor_check("fail", "inbox:" + alias, "not writable"))
        checks.append(_doctor_check("warn", "watch:" + alias, "skipped because the inbox is not writable"))
        return
    try:
        courier.reader(target).watch(target, 0, 0.2)
    except Exception as error:
        detail = failure_detail(error) if isinstance(error, (Refused, OSError, ValueError)) else "watch did not start"
        checks.append(_doctor_check("fail", "watch:" + alias, detail))
    else:
        checks.append(_doctor_check("pass", "watch:" + alias, "started"))


def _doctor_command(checks, alias, transport):
    binary = _first_executable(transport.command)
    if not binary:
        checks.append(_doctor_check(
            "fail", "command:" + alias, "executable not found: %s" % transport.command[0]))
        return
    checks.append(_doctor_check("pass", "command:" + alias, "executable found"))
    checks.append(_doctor_check(
        "warn", "handshake:" + alias,
        "command not invoked; a live handshake would deliver mail"))


def _doctor_claude_hooks(checks, args, aliases):
    try:
        path = claude_settings_path(args.user, args.settings)
    except Refused as error:
        checks.append(_doctor_check("fail", "hook", failure_detail(error)))
        return
    if not path.exists():
        for alias in aliases:
            checks.append(_doctor_check("warn", "hook:" + alias, "settings file is absent"))
        return
    try:
        settings, _raw = read_settings(path)
    except Refused as error:
        checks.append(_doctor_check("fail", "hook", failure_detail(error)))
        return
    entries = []
    groups = settings.get("hooks", {}).get("UserPromptSubmit") if type(settings.get("hooks")) is dict else None
    if type(groups) is list:
        for group in groups:
            if type(group) is dict and type(group.get("hooks")) is list:
                entries.extend(hook for hook in group["hooks"] if type(hook) is dict)
    for alias in aliases:
        expected = show_hook_spec(args.config, alias)
        conflict = None
        exact = False
        for hook in entries:
            if hook.get("type") != "command":
                continue
            if _hook_is_exact(hook, expected):
                exact = True
                continue
            hook_args = hook.get("args")
            if (type(hook_args) is list and all(type(item) is str for item in hook_args)
                    and type(hook.get("command")) is str):
                relation = _argv_relation([hook["command"], *hook_args], alias, ("show",))
                shown = " ".join([hook["command"], *hook_args])
            else:
                relation = _hook_relation(hook.get("command"), alias, "show")
                shown = hook.get("command") if type(hook.get("command")) is str else ""
            if relation in ("conflict", "ambiguous") and conflict is None:
                conflict = shown
        if conflict:
            checks.append(_doctor_check(
                "fail", "hook:" + alias, "not an exact Coura show hook: %s" % conflict))
        elif exact:
            checks.append(_doctor_check("pass", "hook:" + alias, "exact exec form"))
        else:
            checks.append(_doctor_check("warn", "hook:" + alias, "not installed"))


def _doctor_codex(checks, courier, aliases, *, probe_version=True):
    command = None if courier.queue is None else courier.queue.command
    label = ",".join(aliases)
    if not command:
        checks.append(_doctor_check(
            "fail", "codex", "route %s has no codex_queue_command" % label))
        return
    binary = _first_executable(command)
    if not binary:
        checks.append(_doctor_check(
            "fail", "codex", "executable not found: %s" % command[0]))
        return
    if not probe_version:
        checks.append(_doctor_check(
            "warn", "codex", "executable found; not invoked; CLI compatibility unmeasured"))
        return
    version = _probe_version(binary)
    if version is None:
        checks.append(_doctor_check("warn", "codex", "executable found; version probe failed"))
    else:
        checks.append(_doctor_check("pass", "codex", version))


def _emit_doctor(args, checks):
    failed = any(item["status"] == "fail" for item in checks)
    if getattr(args, "json", False):
        print(json.dumps({"ok": not failed, "checks": checks}, ensure_ascii=True, indent=2),
              flush=True)
    else:
        for item in checks:
            print_text(sys.stdout, "%-4s  %-18s  %s\n" % (item["status"], item["name"], item["detail"]))
    return 1 if failed else 0


def _log_rows(courier):
    rows = []
    for row in records(courier.state / "journal.jsonl"):
        if type(row) is not dict or type(row.get("status")) is not str:
            continue
        client = row.get("client")
        identity = row.get("id")
        hits = [name for name, address in courier.recipients.items()
                if address.client == client and address.identity == identity]
        target = hits[0] if len(hits) == 1 else "%s:%s" % (client, identity)
        rows.append({
            "time": row.get("ts") if type(row.get("ts")) is str else "",
            "from": row.get("sender") if type(row.get("sender")) is str else "-",
            "to": target,
            "status": row["status"],
            "key": row.get("key") if type(row.get("key")) is str else "",
            "lens": row.get("lens") if type(row.get("lens")) is str else "",
        })
    return rows


def _since_ok(value):
    return re.fullmatch(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}Z)?", value) is not None


def _log_matches_to(row, wanted, courier):
    if wanted in courier.recipients:
        address = courier.recipients[wanted]
        names = {wanted, "%s:%s" % (address.client, address.identity), address.identity}
        return row["to"] in names
    return row["to"] == wanted or row["to"].endswith(":" + wanted)


def print_log(courier, args):
    rows = _log_rows(courier)
    if args.to:
        rows = [row for row in rows if _log_matches_to(row, args.to, courier)]
    if args.status:
        rows = [row for row in rows if row["status"] == args.status]
    if args.since:
        if not _since_ok(args.since):
            raise Refused("Invalid --since value")
        rows = [row for row in rows if row["time"] >= args.since]
    if args.limit is not None:
        if type(args.limit) is not int or args.limit < 0:
            raise Refused("Invalid --limit value")
        rows = rows[-args.limit:]
    if args.json:
        print(json.dumps(rows, ensure_ascii=True, indent=2), flush=True)
        return 0
    for row in rows:
        lens = row["lens"] if row["lens"] else "-"
        print_text(sys.stdout, "%s  %s  %s  %s  %s  %s\n" % (
            row["time"], row["from"], row["to"], row["status"], row["key"], lens))
    return 0


HOST_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}")
MAX_HOOK_STDIN = 65536
_HOST_EVENTS = {"Stop", "SessionEnd", "StopFailure", "SubagentStop"}


def read_hook_stdin(stream=None):
    """Read one bounded hook JSON object. A terminal or empty stdin is unknown."""
    stream = sys.stdin if stream is None else stream
    if hasattr(stream, "isatty") and stream.isatty():
        return None
    raw = stream.buffer.read(MAX_HOOK_STDIN + 1)
    if not raw or not raw.strip():
        return None
    if len(raw) > MAX_HOOK_STDIN:
        return {"_truncated": True}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return {"_unparsed": True}
    if type(data) is not dict:
        return {"_unparsed": True}
    return data


def _usable_host_token(value):
    if type(value) is str and HOST_TOKEN.fullmatch(value):
        return value
    return None


def host_event_facts(payload, expect):
    """Bind identity only to host tokens that were actually supplied.

    A random local key is not a host session or turn id. Stop is not session end
    and is not task completion. SessionEnd is its own source.
    """
    payload = payload if type(payload) is dict else {}
    unusable = bool(payload.get("_truncated") or payload.get("_unparsed") or not payload)
    observed = None
    if not unusable:
        name = payload.get("hook_event_name")
        if type(name) is str and name in _HOST_EVENTS:
            observed = name
    session = None if unusable else _usable_host_token(payload.get("session_id"))
    prompt = None if unusable else _usable_host_token(payload.get("prompt_id"))
    session_sha = None
    prompt_sha = None
    if not unusable and session is None and type(payload.get("session_id")) is str:
        session_sha = hashlib.sha256(payload["session_id"].encode("utf-8")).hexdigest()
    if not unusable and prompt is None and type(payload.get("prompt_id")) is str:
        prompt_sha = hashlib.sha256(payload["prompt_id"].encode("utf-8")).hexdigest()
    if observed == "Stop":
        claim, session_termination = "response_stopped", "unknown"
    elif observed == "SessionEnd":
        claim, session_termination = "session_ended", "host_reported"
    elif observed == "StopFailure":
        claim, session_termination = "turn_failed", "unknown"
    elif observed == "SubagentStop":
        claim, session_termination = "subagent_response_stopped", "unknown"
    else:
        claim, session_termination = "host_event_unknown", "unknown"
    identifies = False
    local_id = None
    if observed in ("Stop", "StopFailure", "SubagentStop") and session and prompt:
        key = "host/%s/%s/%s" % (observed, session, prompt)
        dedup = "host_session_and_prompt"
        identifies = True
    elif observed == "SessionEnd" and session:
        key = "host/SessionEnd/%s" % session
        dedup = "host_session"
        identifies = True
    else:
        local_id = uuid.uuid4().hex
        key = "host/unscoped/" + local_id
        dedup = "unavailable"
    reason = None
    if observed == "SessionEnd" and not unusable:
        raw_reason = payload.get("reason")
        if type(raw_reason) is str and re.fullmatch(r"[a-z0-9_]{1,64}", raw_reason):
            reason = raw_reason
    stop_active = None
    if not unusable and type(payload.get("stop_hook_active")) is bool:
        stop_active = payload["stop_hook_active"]
    transcript_sha = None
    if not unusable and type(payload.get("transcript_path")) is str and payload["transcript_path"]:
        transcript_sha = hashlib.sha256(payload["transcript_path"].encode("utf-8")).hexdigest()
    facts = {
        "source": "claude_hook_stdin",
        "attribution": "unverified",
        "claim_kind": claim,
        "task_completion": "unknown",
        "session_termination": session_termination,
        "host_event_name": observed,
        "host_session_id": session,
        "host_prompt_id": prompt,
        "host_session_id_sha256": session_sha,
        "host_prompt_id_sha256": prompt_sha,
        "transcript_path_supplied": transcript_sha is not None,
        "transcript_path_sha256": transcript_sha,
        "stop_hook_active": stop_active,
        "session_end_reason": reason,
        "deduplication": dedup,
        "local_key_identifies_host_event": identifies,
        "local_dispatch_id": local_id,
        "configured_hook": expect if type(expect) is str else None,
        "last_assistant_message_present": (
            not unusable and type(payload.get("last_assistant_message")) is str),
        "authenticated": False,
    }
    return key, facts


def host_note(facts):
    leads = {
        "response_stopped": "response stopped",
        "session_ended": "session ended",
        "turn_failed": "turn failed",
        "subagent_response_stopped": "subagent response stopped",
    }
    lead = leads.get(facts["claim_kind"], "host event unknown")
    note = "%s. claim=%s. session_termination=%s. task_completion=unknown. deduplication=%s." % (
        lead, facts["claim_kind"], facts["session_termination"], facts["deduplication"])
    if facts.get("session_end_reason"):
        note += " reason=%s." % facts["session_end_reason"]
    return note


def run_host_event(courier, args):
    key, facts = host_event_facts(read_hook_stdin(), getattr(args, "expect", None))
    token = _DISPATCH.set({"facts": facts})
    try:
        receipt = courier.send(args.recipient, key, "claude-hook", host_note(facts))
    finally:
        _DISPATCH.reset(token)
    sys.stdout.write(json.dumps(receipt.as_dict(), ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 0 if (receipt.status in {"STORED", "QUEUED", "THREAD_ACCEPTED", "SELF"} or
                 receipt.status == "SKIPPED" and receipt.held_status in
                 {"STORED", "QUEUED", "THREAD_ACCEPTED"}) else 1


def _bounded_label(value, label):
    if value is None:
        return None
    if (type(value) is not str or not value or len(value) > 256 or
            any(char in value for char in "\x00\n\r")):
        raise Refused("Invalid " + label)
    return value


def _send_options_from_args(args):
    supplied = {}
    for name in ("task_id", "conversation_id", "message_id", "reply_to", "cause"):
        value = _bounded_label(getattr(args, name, None), name)
        if value is not None:
            supplied[name] = value
    local_task = _bounded_label(getattr(args, "local_task", None), "local_task")
    if not supplied and local_task is None:
        return None
    return {"logical": {
        "sender_supplied": supplied,
        "sender_supplied_signed": False,
        "local_task": local_task,
    }}


def _print_observer(document):
    print(json.dumps(document, ensure_ascii=True, indent=2), flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send")
    send.add_argument("recipient")
    send.add_argument("--key", required=True)
    send.add_argument("--sender", required=True)
    send.add_argument("--note", default="")
    send.add_argument("--delivery", choices=("queue", "urgent"))
    send.add_argument("--lens", help="Named conversation mode. Does not change receiver permissions.")
    send.add_argument("--unique-key", action="store_true",
                      help="Append a timestamp and a short suffix so this key is new. This is not host-event deduplication.")
    send.add_argument("--task-id")
    send.add_argument("--conversation-id")
    send.add_argument("--message-id")
    send.add_argument("--reply-to")
    send.add_argument("--cause")
    send.add_argument("--local-task", help="Local association only. Not a sender-signed task id.")
    data = commands.add_parser("send-data")
    data.add_argument("recipient")
    data.add_argument("--key", required=True)
    data.add_argument("--file", required=True)
    data.add_argument("--data-only", action="store_true",
                      help="Require the data_only receiver hint. Coura does not verify it.")
    data.add_argument("--lens", help="Named conversation mode. Does not change receiver permissions.")
    show = commands.add_parser("show")
    show.add_argument("recipient")
    show.add_argument("--event", default="UserPromptSubmit")
    show.add_argument("--text", action="store_true",
                      help="Print decoded UTF-8 text. Omit this for machine JSON.")
    watch = commands.add_parser("watch")
    watch.add_argument("recipient")
    watch.add_argument("--timeout", type=float)
    watch.add_argument("--poll", type=float, default=2)
    watch.add_argument("--desktop", action="store_true",
                       help="Best-effort desktop notification when mail is ready.")
    init = commands.add_parser("init", help="Merge a Claude Code UserPromptSubmit hook")
    init.add_argument("--settings", help="Settings JSON to update")
    init.add_argument("--user", action="store_true",
                      help="Use ~/.claude/settings.json instead of .claude/settings.local.json")
    init.add_argument("--dry-run", action="store_true",
                      help="Print the settings change and do not write")
    init.add_argument("--recipient", default="writer")
    init.add_argument("--on-stop", metavar="RECIPIENT",
                      help="Also install a Stop hook. Stop means the response stopped, not session end or task completion.")
    init.add_argument("--on-session-end", metavar="RECIPIENT",
                      help="Also install a SessionEnd hook. Session termination is a separate source.")
    host = commands.add_parser(
        "host-event",
        help="Record one Claude hook JSON object from stdin. Stop is not session end.")
    host.add_argument("recipient")
    host.add_argument("--expect", choices=("Stop", "SessionEnd", "StopFailure", "SubagentStop"))
    report = commands.add_parser(
        "report",
        help="Record an unverified disposition. It does not change transport status or grant authority.")
    report.add_argument("--role", required=True)
    report.add_argument("--claim", required=True)
    report.add_argument("--about-key", required=True)
    report.add_argument("--about-attempt")
    report.add_argument("--recipient", required=True)
    report.add_argument("--evidence-ref")
    inspect = commands.add_parser(
        "observer-inspect",
        help="List bounded observer outbox state. Does not deliver.")
    inspect.add_argument("--limit", type=int, default=32)
    inspect.add_argument("--state", default="all")
    export = commands.add_parser(
        "observer-export",
        help="Print a bounded JSON export. This does not deliver and is not an automatic feed.")
    export.add_argument("--limit", type=int, default=32)
    sync = commands.add_parser(
        "observer-sync",
        help="Recover durable facts and deliver one bounded batch to the observer.")
    sync.add_argument("--batch", type=int, default=16)
    runner = commands.add_parser(
        "observer-run",
        help="Run observer-sync until SIGINT or SIGTERM, with backoff. Not installed as a service.")
    runner.add_argument("--interval", type=float, default=2.0)
    runner.add_argument("--batch", type=int, default=16)
    commands.add_parser("demo", help="Run one temporary generic delivery cycle and delete it.")
    doctor_parser = commands.add_parser("doctor", help="Check config, state, and configured routes.")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--settings", help="Claude settings JSON to inspect")
    doctor_parser.add_argument("--user", action="store_true",
                               help="Inspect ~/.claude/settings.json")
    log_parser = commands.add_parser("log", help="Print the journal timeline.")
    log_parser.add_argument("--to")
    log_parser.add_argument("--status")
    log_parser.add_argument("--since")
    log_parser.add_argument("--limit", type=int)
    log_parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    receipt = None
    try:
        if args.command == "demo":
            return run_demo()
        if not args.config:
            parser.error("--config is required")
        if args.command == "init":
            init_claude_hook(args)
            return 0
        if args.command == "doctor":
            return doctor(args)
        courier = Courier.from_config(args.config)
        if args.command == "log":
            return print_log(courier, args)
        if args.command == "host-event":
            return run_host_event(courier, args)
        module = None
        if args.command in ("report", "observer-inspect", "observer-export",
                            "observer-sync", "observer-run"):
            if courier.observer is None:
                raise Refused("No observer configured")
            module = load_observer_module()
        if args.command == "report":
            _print_observer(module.record_claim(
                courier, args.role, args.claim, args.about_key, args.about_attempt,
                args.recipient, args.evidence_ref))
            return 0
        if args.command == "observer-inspect":
            return _print_observer(module.inspect_events(courier, args.limit, args.state))
        if args.command == "observer-export":
            document = module.inspect_events(courier, args.limit, "all")
            document["note"] = "Export does not deliver events and is not an automatic feed."
            return _print_observer(document)
        if args.command == "observer-sync":
            return _print_observer(module.sync(courier, args.batch))
        if args.command == "observer-run":
            return module.run_loop(courier, args.interval, args.batch)
        if args.command in ("send", "send-data"):
            if getattr(args, "unique_key", False):
                args.key = args.key + "/" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
            if getattr(args, "lens", None):
                bind_lens(courier, args.lens)
            dispatch_token = None
            extra = _send_options_from_args(args) if args.command == "send" else None
            if extra is not None:
                dispatch_token = _DISPATCH.set(extra)
            try:
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
            finally:
                if dispatch_token is not None:
                    _DISPATCH.reset(dispatch_token)
        target = courier.resolve(args.recipient)
        if args.command == "show":
            reader = courier.reader(target)
            if target.client == "claude":
                reader.show(target, args.event, text=args.text)
            else:
                reader.show(target, text=args.text)
            return 0
        reader = courier.reader(target)
        callback = None
        if getattr(args, "desktop", False):
            def callback(reader=reader, target=target):
                notify_unread(reader, target)
        ready = reader.watch(target, args.timeout, args.poll, callback)
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
