"""Local notification courier with explicit transport receipts.

The file inbox and command adapter are runnable. A native urgent transport is
an optional host-supplied callable; no application IPC code is bundled.
"""
import argparse
import errno
import hashlib
import math
from contextlib import contextmanager
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
        if self.client not in ("claude", "codex") or not ID.fullmatch(self.identity):
            raise Refused("Address requires a supported client and a full lowercase UUID")


@dataclass(frozen=True)
class Receipt:
    status: str
    detail: str
    recipient_id: str = None
    queue_item_id: str = None
    held_status: str = None

    def as_dict(self):
        return dict(status=self.status, detail=self.detail,
                    recipient_id=self.recipient_id, queue_item_id=self.queue_item_id,
                    held_status=self.held_status, read="UNMEASURED")


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


def encoded_record(row):
    encoded = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_RECORD:
        raise Refused("Record exceeds 8192 bytes")
    return encoded


def append_record(path, row):
    encoded = encoded_record(row)
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

    def __init__(self, state):
        self.root = Path(state) / "inbox"

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
                raw = stream.readline(MAX_RECORD + 1)
                if len(raw) > MAX_RECORD:
                    raise Refused("Oversized inbox record")
                if not raw or not raw.endswith(b"\n"):
                    break
                row = json.loads(raw.decode("utf-8"))
                if not isinstance(row, dict) or not isinstance(row.get("line"), str):
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


def run_command(command, timeout):
    """Wait for the direct child, with file output rather than descendant pipes."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
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
            executable = shutil.which(command[0]) or command[0]
            name = Path(executable).name.lower().rstrip(" .")
            if os.name == "nt" and name != Path(executable).name.lower():
                raise Refused("Ambiguous Windows executable name")
            command[0] = str(Path(executable).resolve())
            if name in {"cmd", "cmd.exe", "powershell", "powershell.exe",
                        "pwsh", "pwsh.exe", "bash", "bash.exe", "sh", "sh.exe"}:
                raise Refused("Shell interpreters need a separately authorised adapter")
            if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"):
                raise Refused("Windows batch wrappers need a separately authorised adapter")
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


class Courier:
    def __init__(self, state, recipients, queue=None, urgent=None):
        self.state = Path(state).resolve()
        self.recipients = dict(recipients)
        if not all(isinstance(k, str) and k and ":" not in k and
                   isinstance(v, Address) for k, v in self.recipients.items()):
            raise Refused("Invalid recipient registry")
        self.inbox, self.queue, self.urgent = Inbox(self.state), queue, urgent

    @classmethod
    def from_config(cls, path):
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or set(data) - {
                "state_dir", "recipients", "codex_queue_command"}:
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
        command = data.get("codex_queue_command")
        return cls(state, recipients, queue=CodexQueue(command) if command else None)

    def resolve(self, recipient):
        if recipient in self.recipients:
            return self.recipients[recipient]
        client, _, prefix = (recipient.partition(":") if ":" in recipient
                             else ("", "", recipient))
        if client not in ("", "claude", "codex") or not PREFIX.fullmatch(prefix):
            raise Refused("Unknown address")
        hits = {x for x in self.recipients.values()
                if (not client or x.client == client) and x.identity.startswith(prefix)}
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
        lock_key = hashlib.sha256(json.dumps(
            [target.client, target.identity, key], ensure_ascii=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        with file_lock(self.state / "send" / lock_key):
            return self._send_locked(target, key, sender, note, delivery)

    def _send_locked(self, target, key, sender, note, delivery):
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
        line = "Check %s from %s." % (key, sender) + (" " + note if note else "")
        # Validate serialized bytes before any reservation or transport call.
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
            receipt = adapter(target, line, requested)
            if type(receipt) is not Receipt:
                raise ValueError("Invalid transport receipt type")
            values = (receipt.status, receipt.detail, receipt.recipient_id,
                      receipt.queue_item_id, receipt.held_status)
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
        if args.command == "send":
            receipt = courier.send(args.recipient, args.key, args.sender,
                                   args.note, args.delivery)
            print(json.dumps(receipt.as_dict(), ensure_ascii=True), flush=True)
            return 0 if (receipt.status in {"STORED", "QUEUED", "THREAD_ACCEPTED", "SELF"} or
                         receipt.status == "SKIPPED" and receipt.held_status in
                         {"STORED", "QUEUED", "THREAD_ACCEPTED"}) else 1
        target = courier.resolve(args.recipient)
        if args.command == "show":
            courier.inbox.show(target, args.event)
            return 0
        ready = courier.inbox.watch(target, args.timeout, args.poll)
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
