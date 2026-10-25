"""Optional Agenda observer for Coura.

Agenda records workflow facts. It is not a relay, an approval hop, or a model
call on the path from one chat to another. Base Coura labels every observer
fact unverified. Meta-Coura is a separate optional patch and is not imported
here. A later Meta-authenticated producer would need all of the following,
which this module does not do:

- verify an Ed25519 signature over the canonical event bytes
- check a receiver-owned registration, scope, expiry, revocation, and one-use
- keep the effect boundary; do not treat an observation as a permit
- never prepend a lens or header to a signed carrier
- leave ledger admission as an exact-effect check, not a routing hop
- leave Spectrum and OTCS as separate optional integrations

Attribution in this file is unverified. STORED and QUEUED are transport facts
only. They are not READ, ACCEPTED, EXECUTED, or DONE.
"""

import hashlib
import json
import os
import signal
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from contextvars import copy_context


SCHEMA = "coura.observer.event.v1"
PROJECTION_SCHEMA = "coura.observer.projection.v1"
TERMINAL = {"STORED", "QUEUED", "THREAD_ACCEPTED", "REFUSED", "OUTCOME_UNKNOWN"}
TRANSPORT_STATUSES = TERMINAL | {"REQUESTED", "RESERVED"}
PUBLISHED = {"STORED", "QUEUED", "THREAD_ACCEPTED"}
MAX_BATCH = 64
MIN_INTERVAL = 0.2
MAX_SOURCE_ROWS = 32
_THREAD_GUARD = threading.Lock()
_THREAD_LOCKS = {}


_BOUND = None


def bind(module):
    """Use the courier module that loaded this file, including a __main__ script."""
    global _BOUND
    _BOUND = module


def _courier():
    if _BOUND is not None:
        return _BOUND
    import courier
    return courier


def _runtime(instance):
    """Module that defined this Courier. A script run is __main__, not the import."""
    import sys
    return sys.modules[type(instance).__module__]


def _private(path):
    _courier().ensure_private_dir(path)


def _thread_lock(path):
    key = str(path)
    with _THREAD_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _read_producer(path):
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    if _courier().ENDPOINT_ID.fullmatch(text) is None:
        raise _courier().Refused("Invalid stored observer producer id")
    return text


def producer_id(courier):
    """Stable local producer id. Not a host session id and not authenticated.

    The first creator wins. A later caller re-reads that value under the same
    inter-process lock and does not mint a second id.
    """
    configured = courier.observer.get("producer_id")
    if configured:
        return configured
    directory = courier.state / "observer"
    _private(directory)
    path = directory / "producer-id"
    with _thread_lock(path), _courier().file_lock(directory / "producer-id.gate"):
        current = _read_producer(path)
        if current:
            return current
        value = "coura-" + uuid.uuid4().hex[:16]
        _write_private(path, value + "\n")
        winner = _read_producer(path)
        if winner is None:
            raise _courier().Refused("Observer producer id was not stored")
        return winner


def _producer_if_ready(courier):
    """Producer id without waiting. None means the send path must defer capture."""
    configured = courier.observer.get("producer_id")
    if configured:
        return configured
    path = courier.state / "observer" / "producer-id"
    try:
        return _read_producer(path)
    except _courier().Refused:
        return None


def _write_private(path, text):
    _private(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if os.name != "nt":
        os.chmod(path, 0o600)


def event_id_for(producer, source_log, offset, attempt, status):
    material = json.dumps(
        [producer, source_log, offset, attempt if type(attempt) is str else "", status],
        ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def canonical_bytes(document):
    return json.dumps(document, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def outbox_path(courier):
    return courier.state / "observer" / "outbox.jsonl"


def delivery_journal(courier):
    return courier.state / "observer" / "delivery.jsonl"


def claims_path(courier):
    return courier.state / "observer" / "claims.jsonl"


def gate_path(courier):
    return courier.state / "observer" / "gate"


def read_spans(path):
    """Yield (byte_offset, row) for a durable JSONL log. Offset is local append order."""
    courier = _courier()
    path = Path(path)
    if not path.exists():
        return
    with courier.file_lock(path), path.open("rb") as stream:
        while True:
            offset = stream.tell()
            raw = stream.readline(courier.MAX_RECORD + 1)
            if not raw:
                return
            if len(raw) > courier.MAX_RECORD or not raw.endswith(b"\n"):
                raise courier.Refused("Incomplete or oversized observer source record")
            row = json.loads(raw.decode("utf-8"))
            if type(row) is not dict:
                raise courier.Refused("Invalid observer source record")
            yield offset, row


def _alias_for(courier, client, identity):
    hits = [name for name, address in courier.recipients.items()
            if address.client == client and address.identity == identity]
    if len(hits) == 1:
        return hits[0]
    return None


def _logical_block(row):
    raw = row.get("logical")
    if type(raw) is not dict:
        return {"sender_supplied": {}, "sender_supplied_signed": False, "local_task": None}
    supplied = raw.get("sender_supplied")
    if type(supplied) is not dict:
        supplied = {}
    clean = {}
    for name in ("task_id", "conversation_id", "message_id", "reply_to", "cause"):
        value = supplied.get(name)
        if type(value) is str:
            clean[name] = value
    local = raw.get("local_task")
    if type(local) is not str:
        local = None
    return {
        "sender_supplied": clean,
        "sender_supplied_signed": False,
        "local_task": local,
    }


def _facts_block(row):
    raw = row.get("facts")
    if type(raw) is not dict:
        return None
    allowed = {
        "source", "attribution", "claim_kind", "task_completion", "session_termination",
        "host_event_name", "host_session_id", "host_prompt_id", "host_session_id_sha256",
        "host_prompt_id_sha256", "transcript_path_supplied", "transcript_path_sha256",
        "stop_hook_active", "session_end_reason", "deduplication",
        "local_key_identifies_host_event", "local_dispatch_id", "configured_hook",
        "last_assistant_message_present", "authenticated",
    }
    clean = {}
    for key, value in raw.items():
        if key not in allowed:
            continue
        if type(value) in (str, bool, int) or value is None:
            if type(value) is str and len(value) > 512:
                continue
            clean[key] = value
    clean["attribution"] = "unverified"
    clean["authenticated"] = False
    return clean


def build_event(courier, source_log, offset, row, body_text=None, producer=None):
    """One fact from a durable source row. Missing fields stay absent or unknown."""
    if producer is None:
        producer = producer_id(courier)
    attempt = row.get("attempt") if type(row.get("attempt")) is str else ""
    status = row.get("status") if type(row.get("status")) is str else "UNKNOWN"
    identity = event_id_for(producer, source_log, offset, attempt, status)
    client = row.get("client") if type(row.get("client")) is str else None
    recipient_id = row.get("id") if type(row.get("id")) is str else None
    facts = _facts_block(row)
    claim_kind = "transport_receipt"
    if facts is not None and type(facts.get("claim_kind")) is str:
        claim_kind = facts["claim_kind"]
    if source_log == "claims.jsonl":
        claim_kind = row.get("claim_kind") if type(row.get("claim_kind")) is str else "unverified_claim"
    body = None
    body_omitted = "not_copied"
    include = bool(courier.observer.get("include_body"))
    if include and type(body_text) is str and status in PUBLISHED:
        raw = body_text.encode("utf-8")
        if len(raw) <= 2048:
            body = body_text
            body_omitted = None
        else:
            body_omitted = "exceeds_copy_limit"
    event = {
        "schema": SCHEMA,
        "event_id": identity,
        "producer": {
            "id": producer,
            "source": "coura",
            "attribution": "unverified",
            "meta_coura": "absent",
        },
        "source_position": {
            "log": source_log,
            "offset": offset,
            "sequence": offset,
            "order": "local_append_only",
        },
        "source_timestamp": row.get("ts") if type(row.get("ts")) is str else None,
        "observed_at": _courier().now(),
        "clock": "producer_local_not_global",
        "sender": {
            "reported": row.get("sender") if type(row.get("sender")) is str else None,
            "canonical": None,
            "authenticated": False,
            "attribution": "unverified",
        },
        "recipient": {
            "client": client,
            "id": recipient_id,
            "alias": _alias_for(courier, client, recipient_id),
            "source": "local_registry",
            "authenticated": False,
        },
        "identifiers": _logical_block(row),
        "delivery": {
            "key": row.get("key") if type(row.get("key")) is str else None,
            "attempt_id": attempt or None,
            "status": status,
            "dispatch": row.get("dispatch") if type(row.get("dispatch")) is str else None,
            "detail": row.get("detail") if type(row.get("detail")) is str else None,
        },
        "disposition": None,
        "evidence_ref": None,
        "host_facts": facts,
        "claim_kind": claim_kind,
        "provenance": {
            "authentication": "unverified",
            "meta_coura": "absent",
            "authority": "none",
            "unknown": [
                "sender_authentication",
                "read_state",
                "acceptance",
                "execution",
                "task_completion",
            ],
        },
        "body_ref": {
            "log": source_log,
            "offset": offset,
            "attempt_id": attempt or None,
            "attempted_body_sha256": row.get("attempted_body_sha256")
            if type(row.get("attempted_body_sha256")) is str else None,
        },
        "body": body,
        "body_omitted": body_omitted,
        "labels": {
            "lens": row.get("lens") if type(row.get("lens")) is str else None,
            "lens_is_task_identity": False,
            "lens_is_authority": False,
        },
    }
    if source_log == "claims.jsonl":
        event["disposition"] = {
            "role": row.get("role") if type(row.get("role")) is str else None,
            "claim": row.get("claim") if type(row.get("claim")) is str else None,
            "source": "explicit_report",
            "attribution": "unverified",
            "authenticated": False,
        }
        event["evidence_ref"] = row.get("evidence_ref") if type(row.get("evidence_ref")) is str else None
        event["delivery"] = {
            "key": row.get("about_key") if type(row.get("about_key")) is str else None,
            "attempt_id": row.get("about_attempt") if type(row.get("about_attempt")) is str else None,
            "status": None,
            "dispatch": None,
            "detail": "disposition claim; not a transport receipt",
        }
    return event


def _append_outbox(courier, row):
    path = outbox_path(courier)
    _private(path.parent)
    return _courier().append_record(path, row)


def _index_path(courier):
    return courier.state / "observer" / "index.sqlite3"


def _require_observer(courier):
    if courier.observer is None:
        raise _courier().Refused("No observer configured")
    error = courier.observer.get("error")
    if type(error) is str and error:
        raise _courier().Refused(error)


def _deferred(detail=None):
    return {
        "status": "PENDING", "deferred": True,
        "detail": detail or (
            "Observer capture deferred. The participant journal holds the fact. "
            "observer-sync recovers it. Not an observer receipt."),
    }


class _Index:
    """Derived disk index. The append-only outbox remains the audit source.

    Automatic passes import at most 32 records. SQLite uses a 512 KiB page cache
    and disk temporary storage; neither disk history nor total I/O is capped.
    A missing index is rebuilt across passes before any publication.
    Stop old observer writers before using this index protocol.
    """
    def __init__(self, courier):
        self.courier = courier
        path = _index_path(courier)
        _private(path.parent)
        self.db = sqlite3.connect(str(path), timeout=1.0)
        try:
            self.db.execute("PRAGMA cache_size=-512")
            self.db.execute("PRAGMA temp_store=FILE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS positions (
                    name TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, seq INTEGER NOT NULL,
                    event TEXT, state TEXT NOT NULL, dispatch TEXT);
                CREATE INDEX IF NOT EXISTS event_state_seq ON events(state,seq);
                CREATE TABLE IF NOT EXISTS candidates (
                    attempt TEXT PRIMARY KEY, offset INTEGER NOT NULL, row TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS candidate_offset ON candidates(offset);
            """)
            if os.name != "nt":
                os.chmod(path, 0o600)
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.db.close()

    def position(self, name, default=0):
        row = self.db.execute("SELECT value FROM positions WHERE name=?", (name,)).fetchone()
        return row[0] if row is not None else default

    def set_position(self, name, value):
        self.db.execute(
            "INSERT INTO positions VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (name, value))

    def contains(self, identity):
        return self.db.execute(
            "SELECT 1 FROM events WHERE event_id=? AND event IS NOT NULL",
            (identity,)).fetchone() is not None

    def _apply(self, offset, row):
        identity = row.get("event_id")
        if type(identity) is not str:
            return
        if row.get("kind") == "capture" and type(row.get("event")) is dict:
            self.db.execute("""
                INSERT INTO events VALUES (?,?,?,'pending',NULL)
                ON CONFLICT(event_id) DO UPDATE SET seq=excluded.seq,event=excluded.event
                WHERE events.event IS NULL
            """, (identity, offset, json.dumps(row["event"], ensure_ascii=True)))
        elif row.get("kind") == "dispatch" and row.get("state") in (
                "pending", "not_started", "unknown", "held", "published", "too_large"):
            self.db.execute("""
                INSERT INTO events VALUES (?,-1,NULL,?,?)
                ON CONFLICT(event_id) DO UPDATE SET state=excluded.state,dispatch=excluded.dispatch
            """, (identity, row["state"], json.dumps(row, ensure_ascii=True)))

    def ingest(self):
        start = self.position("outbox")
        rows, consumed, partial = read_limited(outbox_path(self.courier), start, MAX_SOURCE_ROWS)
        with self.db:
            for offset, row in rows:
                self._apply(offset, row)
            self.set_position("outbox", consumed)
        path = outbox_path(self.courier)
        size = path.stat().st_size if path.exists() else 0
        return not partial and consumed == size

    def append(self, row):
        offset = _append_outbox(self.courier, row)
        with self.db:
            self._apply(offset, row)
            if offset == self.position("outbox"):
                self.set_position("outbox", offset + len(_courier().encoded_record(row)))
        return offset

    def counts(self):
        return dict(self.db.execute(
            "SELECT state,COUNT(*) FROM events WHERE event IS NOT NULL GROUP BY state"))

    def pending(self, batch):
        previous = self.position("round", -1)
        query = """SELECT seq,event FROM events
                   WHERE state IN ('pending','not_started') AND event IS NOT NULL
                   AND seq {} ? ORDER BY seq LIMIT ?"""
        rows = self.db.execute(query.format(">"), (previous, batch)).fetchall()
        if len(rows) < batch:
            rows += self.db.execute(query.format("<="), (previous, batch-len(rows))).fetchall()
        return [(seq, json.loads(event)) for seq, event in rows]


def known_ids(courier):
    """Explicit all-history diagnostic; automatic send/sync never builds this set."""
    with _Index(courier) as index:
        return {row[0] for row in index.db.execute(
            "SELECT event_id FROM events WHERE event IS NOT NULL")}


def _store_capture(courier, event, index):
    identity = event["event_id"]
    if index.contains(identity):
        return False
    stored = {
        "kind": "capture", "event_id": identity,
        "source_log": event["source_position"]["log"],
        "source_offset": event["source_position"]["offset"], "event": event,
    }
    try:
        _courier().encoded_record(stored)
    except _courier().Refused:
        event = dict(event, body=None, body_omitted="exceeds_record")
        stored["event"] = event
    index.append(stored)
    return True


def capture_row(courier, source_log, offset, row, body_text=None):
    """Explicit background capture. Ordinary sends do not call this function."""
    if courier.observer is None or courier.observer.get("error") or source_log == "delivery.jsonl":
        return None
    with _courier().file_lock(gate_path(courier)), _Index(courier) as index:
        if not index.ingest():
            return _deferred("Observer index is rebuilding; source fact retained.")
        event = build_event(courier, source_log, offset, row, body_text)
        _store_capture(courier, event, index)
    return {"status": "PENDING", "event_id": event["event_id"],
            "detail": "Captured for observer delivery. Not an observer receipt."}


def fast_capture_row(courier, source_log, offset, row, body_text=None):
    """Compatibility entry: always defer; it performs no observer I/O."""
    return _deferred()


def read_limited(path, start, max_rows):
    """Read at most max_rows complete records from start.

    A partial tail is left in place and is not an error. The returned offset
    stops before that tail.
    """
    courier = _courier()
    path = Path(path)
    if not path.exists() or max_rows <= 0:
        return [], start, False
    rows = []
    consumed = start
    partial = False
    with courier.file_lock(path), path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if start > size:
            start = size
        stream.seek(start)
        while len(rows) < max_rows:
            offset = stream.tell()
            raw = stream.readline(courier.MAX_RECORD + 1)
            if not raw:
                break
            if len(raw) > courier.MAX_RECORD:
                raise courier.Refused("Incomplete or oversized observer source record")
            if not raw.endswith(b"\n"):
                partial = True
                break
            row = json.loads(raw.decode("utf-8"))
            if type(row) is not dict:
                raise courier.Refused("Invalid observer source record")
            rows.append((offset, row))
            consumed = stream.tell()
    return rows, consumed, partial


def _recover_journal(courier, index):
    path = courier.state / "journal.jsonl"
    rows, consumed, partial = read_limited(path, index.position("journal"), MAX_SOURCE_ROWS)
    producer = producer_id(courier)
    recovered = 0
    for offset, row in rows:
        if type(row.get("status")) is not str:
            continue
        attempt = row.get("attempt") if type(row.get("attempt")) is str else "offset:%s" % offset
        if row["status"] in TERMINAL:
            body = row.get("observer_body")
            event = build_event(courier, "journal.jsonl", offset, row,
                                body if type(body) is str else None, producer=producer)
            recovered += _store_capture(courier, event, index)
            with index.db:
                index.db.execute("DELETE FROM candidates WHERE attempt=?", (attempt,))
        else:
            with index.db:
                index.db.execute("""
                    INSERT INTO candidates VALUES (?,?,?)
                    ON CONFLICT(attempt) DO UPDATE SET offset=excluded.offset,row=excluded.row
                """, (attempt, offset, json.dumps(row, ensure_ascii=True)))
    with index.db:
        index.set_position("journal", consumed)
    size = path.stat().st_size if path.exists() else 0
    if not partial and consumed == size:
        candidates = index.db.execute(
            "SELECT attempt,offset,row FROM candidates ORDER BY offset LIMIT ?",
            (MAX_SOURCE_ROWS,)).fetchall()
        for attempt, offset, text in candidates:
            row = json.loads(text)
            event = build_event(courier, "journal.jsonl", offset, row, producer=producer)
            recovered += _store_capture(courier, event, index)
            with index.db:
                index.db.execute("DELETE FROM candidates WHERE attempt=?", (attempt,))
    return recovered


def _recover_claims(courier, index):
    rows, consumed, _partial = read_limited(
        claims_path(courier), index.position("claims"), MAX_SOURCE_ROWS)
    producer = producer_id(courier)
    recovered = 0
    for offset, row in rows:
        if type(row.get("claim_kind")) is str:
            event = build_event(courier, "claims.jsonl", offset, row, producer=producer)
            recovered += _store_capture(courier, event, index)
    with index.db:
        index.set_position("claims", consumed)
    return recovered


def _reconcile(courier, index):
    return _recover_journal(courier, index) + _recover_claims(courier, index)


def reconcile(courier):
    """Explicit background recovery; at most 32 rows from each source per pass."""
    _require_observer(courier)
    with _courier().file_lock(gate_path(courier)), _Index(courier) as index:
        if not index.ingest():
            return 0
        return _reconcile(courier, index)


def fold(courier):
    """Current outbox state per event id. Last dispatch record wins. Order is source offset."""
    captures = {}
    states = {}
    for _offset, row in read_spans(outbox_path(courier)):
        identity = row.get("event_id")
        if type(identity) is not str:
            continue
        if row.get("kind") == "capture" and type(row.get("event")) is dict:
            captures[identity] = row
        elif row.get("kind") == "dispatch" and type(row.get("state")) is str:
            states[identity] = row
    items = []
    for identity, capture in captures.items():
        event = capture["event"]
        position = event.get("source_position") if type(event.get("source_position")) is dict else {}
        state = "pending"
        extra = {}
        if identity in states:
            state = states[identity]["state"]
            extra = states[identity]
        items.append({
            "event_id": identity,
            "state": state,
            "source_log": position.get("log"),
            "source_offset": position.get("offset"),
            "event": event,
            "dispatch": extra,
        })
    items.sort(key=lambda item: (
        item["source_log"] or "",
        item["source_offset"] if type(item["source_offset"]) is int else 0,
        item["event_id"]))
    return items


def classify(receipt, dispatch):
    """Map a transport receipt to observer state. Unknown is not a confirmed no-start."""
    status = receipt.status
    if status == "SKIPPED":
        held = receipt.held_status
        if held in PUBLISHED:
            return "published"
        if held == "OUTCOME_UNKNOWN":
            return "unknown"
        return "held"
    if status in PUBLISHED:
        return "published"
    if status == "OUTCOME_UNKNOWN":
        return "unknown"
    if status == "REFUSED" and dispatch == "NOT_STARTED":
        return "not_started"
    if status == "REFUSED":
        return "held"
    return "unknown"


def _latest_row(path, client, identity, key):
    found = None
    for _offset, row in read_spans(path):
        if (row.get("client"), row.get("id"), row.get("key")) == (client, identity, key):
            if type(row.get("status")) is str:
                found = row
    return found


def _fit_payload(event):
    courier = _courier()
    payload = canonical_bytes(event)
    if len(payload) <= courier.MAX_DATA:
        return event, payload
    shrunk = dict(event)
    shrunk["body"] = None
    shrunk["body_omitted"] = "exceeds_limit"
    delivery = dict(shrunk.get("delivery") or {})
    detail = delivery.get("detail")
    if type(detail) is str and len(detail) > 80:
        delivery["detail"] = detail[:80]
        shrunk["delivery"] = delivery
    payload = canonical_bytes(shrunk)
    if len(payload) <= courier.MAX_DATA:
        return shrunk, payload
    return None, payload


def publish(courier, event):
    """Deliver one event with a stable key. Never mints a fresh key after an unknown result."""
    destination = courier.observer["destination"]
    target = courier.recipients[destination]
    key = "obs-" + event["event_id"]
    fitted, payload = _fit_payload(event)
    if fitted is None:
        record = {"kind": "dispatch", "event_id": event["event_id"], "state": "too_large",
                  "transport_status": None, "transport_key": key, "transport_attempt": None,
                  "held_status": None, "dispatch": None}
        _append_outbox(courier, record)
        return record
    journal = delivery_journal(courier)
    _private(journal.parent)
    runtime = _runtime(courier)
    publication = copy_context()
    publication.run(runtime._DISPATCH.set, {"journal": str(journal), "emit_observation": False})
    publication.run(runtime._LENS.set, None)
    receipt = publication.run(courier.send_data, destination, key, payload)
    row = _latest_row(journal, target.client, target.identity, key)
    dispatch = row.get("dispatch") if type(row) is dict else None
    state = classify(receipt, dispatch)
    record = {
        "kind": "dispatch",
        "event_id": event["event_id"],
        "state": state,
        "transport_status": receipt.status,
        "transport_key": key,
        "transport_attempt": receipt.attempt_id,
        "held_status": receipt.held_status,
        "dispatch": dispatch,
    }
    _append_outbox(courier, record)
    return record


def _open_source_tail(path):
    """True when a log's last byte is not a newline. That fragment is not a record."""
    path = Path(path)
    if not path.exists():
        return False
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            return False
        stream.seek(-1, os.SEEK_END)
        return stream.read(1) != b"\n"


def sync(courier, batch):
    """Import a bounded window, recover source rows, and publish a fair batch."""
    _require_observer(courier)
    if type(batch) is not int or not 1 <= batch <= MAX_BATCH:
        raise _courier().Refused("Observer batch must be 1..64")
    with _courier().file_lock(gate_path(courier)), _Index(courier) as index:
        ready = index.ingest()
        recovered = _reconcile(courier, index) if ready else 0
        results = []
        if ready and not _open_source_tail(courier.state / "journal.jsonl"):
            for seq, event in index.pending(batch):
                published = publish(courier, event)
                # publish already appended the audit record. Its index update and
                # the next-pass cursor can be recovered independently after a crash.
                with index.db:
                    index._apply(-1, published)
                    index.set_position("round", seq)
                results.append(published)
        return {"recovered": recovered, "attempted": len(results),
                "results": results, "counts": index.counts(), "index_complete": ready}


def inspect_events(courier, limit, state):
    """Inspect an indexed prefix without delivery. Incomplete coverage is explicit."""
    _require_observer(courier)
    if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
        raise _courier().Refused("Observer limit must be 1..64")
    allowed = {"all", "pending", "not_started", "unknown", "held", "published", "too_large"}
    if state not in allowed:
        raise _courier().Refused("Invalid observer state")
    with _courier().file_lock(gate_path(courier)), _Index(courier) as index:
        ready = index.ingest()
        # Inspection may recover facts but never sends them.
        if ready:
            _reconcile(courier, index)
        clause = "" if state == "all" else " AND state=?"
        params = () if state == "all" else (state,)
        rows = index.db.execute(
            "SELECT event_id,state,event,dispatch FROM events WHERE event IS NOT NULL" +
            clause + " ORDER BY seq DESC LIMIT ?", params + (limit+1,)).fetchall()
        matched = []
        for identity, item_state, text, record in rows[:limit]:
            event = json.loads(text)
            dispatch = json.loads(record) if record else {}
            delivery = event.get("delivery") if type(event.get("delivery")) is dict else {}
            position = event.get("source_position") if type(event.get("source_position")) is dict else {}
            matched.append({
                "event_id": identity, "state": item_state,
                "source_log": position.get("log"), "source_offset": position.get("offset"),
                "claim_kind": event.get("claim_kind"), "delivery_key": delivery.get("key"),
                "delivery_status": delivery.get("status"),
                "transport_key": dispatch.get("transport_key"),
                "transport_status": dispatch.get("transport_status"),
                "dispatch": dispatch.get("dispatch"),
            })
        pending_sources = any(
            (path.stat().st_size if path.exists() else 0) > index.position(name)
            for name, path in (("journal", courier.state / "journal.jsonl"),
                               ("claims", claims_path(courier))))
        pending_sources = pending_sources or index.db.execute(
            "SELECT 1 FROM candidates LIMIT 1").fetchone() is not None
        return {
            "events": list(reversed(matched)), "truncated": not ready or len(rows) > limit,
            "counts": index.counts(), "automatic_feed": False,
            "index_complete": ready, "source_recovery_pending": pending_sources,
        }


def project(events):
    """Status projection. Arrival order cannot regress a higher source offset.

    verified_* stay unknown. Body text is ignored. Repeated event ids apply once.
    """
    chosen = {}
    for event in events:
        if type(event) is not dict or event.get("schema") != SCHEMA:
            continue
        identity = event.get("event_id")
        if type(identity) is not str:
            continue
        if identity not in chosen:
            chosen[identity] = event
    ordered = sorted(chosen.values(), key=lambda event: (
        ((event.get("source_position") or {}).get("log") or ""),
        (event.get("source_position") or {}).get("offset")
        if type((event.get("source_position") or {}).get("offset")) is int else 0,
        event.get("event_id") or ""))
    attempts = {}
    claims = []
    stopped = []
    ended = []
    for event in ordered:
        position = event.get("source_position") if type(event.get("source_position")) is dict else {}
        offset = position.get("offset") if type(position.get("offset")) is int else 0
        delivery = event.get("delivery") if type(event.get("delivery")) is dict else {}
        claim_kind = event.get("claim_kind")
        if claim_kind == "transport_receipt" and delivery.get("status") in TRANSPORT_STATUSES:
            recipient = event.get("recipient") if type(event.get("recipient")) is dict else {}
            slot = "%s|%s|%s|%s" % (
                recipient.get("client"), recipient.get("id"),
                delivery.get("key"), delivery.get("attempt_id"))
            bucket = attempts.setdefault(slot, {
                "recipient": recipient,
                "key": delivery.get("key"),
                "attempt_id": delivery.get("attempt_id"),
                "statuses_by_source_offset": [],
                "transport_status_at_highest_source_offset": None,
                "highest_source_offset": None,
                "conflict": False,
            })
            bucket["statuses_by_source_offset"].append(
                {"offset": offset, "status": delivery.get("status")})
            if (bucket["highest_source_offset"] is None or offset >= bucket["highest_source_offset"]):
                bucket["highest_source_offset"] = offset
                bucket["transport_status_at_highest_source_offset"] = delivery.get("status")
        elif claim_kind == "response_stopped":
            stopped.append(_public_host(event))
        elif claim_kind == "session_ended":
            ended.append(_public_host(event))
        else:
            claims.append({
                "event_id": event.get("event_id"),
                "claim_kind": claim_kind,
                "attribution": "unverified",
                "authenticated": False,
                "disposition": event.get("disposition"),
                "host_facts": event.get("host_facts"),
                "source_offset": offset,
                "source_log": position.get("log"),
            })
    for bucket in attempts.values():
        statuses = {item["status"] for item in bucket["statuses_by_source_offset"]}
        bucket["conflict"] = len(statuses) > 1
    return {
        "schema": PROJECTION_SCHEMA,
        "event_ids": sorted(chosen),
        "verified_task_state": "unknown",
        "verified_read_state": "unknown",
        "verified_acceptance": "unknown",
        "verified_execution": "unknown",
        "attempts": attempts,
        "unverified_claims": claims,
        "response_stopped": stopped,
        "session_ended": ended,
        "body_text_ignored": True,
    }


def _public_host(event):
    facts = event.get("host_facts") if type(event.get("host_facts")) is dict else {}
    return {
        "event_id": event.get("event_id"),
        "claim_kind": event.get("claim_kind"),
        "task_completion": facts.get("task_completion", "unknown"),
        "session_termination": facts.get("session_termination", "unknown"),
        "attribution": "unverified",
        "deduplication": facts.get("deduplication"),
    }


def record_claim(courier, role, claim, about_key, about_attempt, recipient, evidence_ref):
    """Store an explicit unverified disposition. It does not change a transport receipt."""
    _require_observer(courier)
    roles = {"receiver", "executor", "sender", "accepting_party"}
    claims = {"received", "accepted", "refused", "executed", "done", "other"}
    if role not in roles or claim not in claims:
        raise _courier().Refused("Invalid disposition role or claim")
    if type(about_key) is not str or not about_key or len(about_key) > 256:
        raise _courier().Refused("Invalid disposition key")
    if about_attempt is not None and (type(about_attempt) is not str or len(about_attempt) > 128):
        raise _courier().Refused("Invalid disposition attempt")
    if evidence_ref is not None and (type(evidence_ref) is not str or len(evidence_ref) > 256
                                     or "\n" in evidence_ref or "\x00" in evidence_ref):
        raise _courier().Refused("Invalid evidence reference")
    target = courier.resolve(recipient)
    row = {
        "ts": _courier().now(),
        "status": "CLAIM",
        "claim_kind": "receiver_disposition" if role == "receiver" else role + "_claim",
        "role": role,
        "claim": claim,
        "attribution": "unverified",
        "authenticated": False,
        "about_key": about_key,
        "about_attempt": about_attempt,
        "client": target.client,
        "id": target.identity,
        "evidence_ref": evidence_ref,
        "task_completion": "unknown",
        "attempt": uuid.uuid4().hex,
    }
    path = claims_path(courier)
    _private(path.parent)
    _courier().append_record(path, row)
    return {"status": "RECORDED", "attribution": "unverified",
            "detail": "Disposition stored. It is not a verified task state."}


def run_loop(courier, interval, batch):
    """Bounded sync loop. SIGINT and SIGTERM stop it. This is not installed for the user."""
    if type(interval) is not float and type(interval) is not int:
        raise _courier().Refused("Invalid observer interval")
    if not MIN_INTERVAL <= float(interval) <= 120:
        raise _courier().Refused("Observer interval must be 0.2..120 seconds")
    stop = {"flag": False}

    def handle(signum, frame):
        stop["flag"] = True

    previous_int = signal.signal(signal.SIGINT, handle)
    previous_term = signal.signal(signal.SIGTERM, handle)
    iterations = 0
    delay = float(interval)
    try:
        while not stop["flag"]:
            summary = sync(courier, batch)
            iterations += 1
            counts = summary["counts"]
            waiting = sum(counts.get(name, 0) for name in ("pending", "unknown", "not_started", "held"))
            if summary["attempted"] == 0 or (_published_count(summary) == 0 and waiting):
                delay = min(delay * 2, 30.0)
            else:
                delay = float(interval)
            slept = 0.0
            while slept < delay and not stop["flag"]:
                step = min(0.2, delay - slept)
                time.sleep(step)
                slept += step
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
    print(json.dumps({"stopped": True, "iterations": iterations}, ensure_ascii=True), flush=True)
    return 0


def _published_count(summary):
    return sum(1 for row in summary["results"] if row.get("state") == "published")
