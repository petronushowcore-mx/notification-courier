"""MCP stdio adapter checks. Live registration in a host application is not measured."""
import base64
import contextlib
import contextvars
import io
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "coura_mcp.py"


def write_config(directory, state):
    body = {
        "state_dir": str(state),
        "transports": {"worker": {"type": "inbox"}},
        "recipients": {
            "alpha": {"client": "worker", "id": "alpha"},
            "beta": {"client": "worker", "id": "beta"},
        },
    }
    path = Path(directory) / "coura.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def payloads(state, client, identity):
    key = hashlib.sha256(json.dumps(
        [client, identity], ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    path = Path(state) / "data-inbox" / (key + ".jsonl")
    if not path.exists():
        return []
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            row = json.loads(line)
            found.append(base64.b64decode(row["payload"], validate=True))
    return found


class Pipe:
    def __init__(self, config, extra=()):
        self.proc = subprocess.Popen(
            [sys.executable, "-B", str(ADAPTER), "--config", str(config), *extra],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        return self._read()

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def write_raw(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _send(self, message):
        self.write_raw((json.dumps(message) + "\n").encode("utf-8"))

    def _read(self):
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read().decode("utf-8", "replace")
            raise AssertionError("server closed stdout: %s" % stderr)
        return json.loads(line)

    def start(self):
        done = self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "coura-mcp-test", "version": "0"},
        })
        if "result" not in done:
            raise AssertionError(done)
        self.notify("notifications/initialized")
        return done

    def call(self, name, arguments=None):
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate(timeout=5)


class McpTests(unittest.TestCase):
    def test_core_does_not_import_the_adapter(self):
        text = (ROOT / "courier.py").read_text(encoding="utf-8")
        text += "\n" + (ROOT / "observer.py").read_text(encoding="utf-8")
        self.assertNotIn("coura_mcp", text)
        self.assertNotIn("import mcp", text)

    def test_stdio_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            state = directory / "state"
            config = write_config(directory, state)
            sender = Pipe(config)
            reader = Pipe(config)
            try:
                sender.write_raw(b"{bad\n")
                broken = sender._read()
                self.assertEqual(broken["error"]["code"], -32700)
                early = sender.request("tools/list")
                self.assertEqual(early["error"]["code"], -32600)
                started = sender.start()
                self.assertEqual(started["result"]["protocolVersion"], "2025-06-18")
                self.assertIn("tools", started["result"]["capabilities"])
                listed = sender.request("tools/list")
                names = [tool["name"] for tool in listed["result"]["tools"]]
                self.assertEqual(names, ["send", "read_inbox", "delivery_status", "list_recipients"])
                unknown = sender.call("missing-tool")
                self.assertEqual(unknown["error"]["code"], -32602)
                self.assertIn("Unknown tool: missing-tool", unknown["error"]["message"])
                sender.write_raw(b"x" * 70000 + b"\n")
                oversized = sender._read()
                self.assertEqual(oversized["error"]["code"], -32700)
                again = sender.request("tools/list")
                self.assertIn("result", again)
                recipients = sender.call("list_recipients")
                aliases = [item["alias"] for item in recipients["result"]["structuredContent"]["recipients"]]
                self.assertEqual(aliases, ["alpha", "beta"])
                note = "round-trip-token"
                sent = sender.call("send", {
                    "recipient": "beta", "key": "round", "sender": "alpha", "note": note,
                })
                body = sent["result"]["structuredContent"]
                self.assertEqual(body["status"], "STORED")
                self.assertFalse(sent["result"]["isError"])
                status = sender.call("delivery_status", {"recipient": "beta", "key": "round"})
                self.assertEqual(status["result"]["structuredContent"]["status"], "STORED")
                reader.start()
                inbox = reader.call("read_inbox", {"recipient": "beta"})
                messages = inbox["result"]["structuredContent"]["messages"]
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0]["body"], "Check round from alpha. " + note)
                self.assertEqual(payloads(state, "worker", "beta"), [
                    b"Check round from alpha. " + note.encode("ascii")])
            finally:
                sender.close()
                reader.close()

    def test_annotations_present_in_tools_list(self):
        sys.path.insert(0, str(ROOT))
        import coura_mcp
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            config = write_config(directory, directory / "state")
            pipe = Pipe(config)
            try:
                pipe.start()
                listed = pipe.request("tools/list")
                tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
                send = tools["send"]["annotations"]
                self.assertIs(send["readOnlyHint"], False)
                self.assertIs(send["destructiveHint"], False)
                self.assertIs(send["idempotentHint"], True)
                self.assertIs(send["openWorldHint"], False)
                for name in ("read_inbox", "delivery_status", "list_recipients"):
                    annotations = tools[name]["annotations"]
                    self.assertIs(annotations["readOnlyHint"], name != "read_inbox")
                    self.assertIs(annotations["openWorldHint"], False)
                    self.assertNotIn("destructiveHint", annotations)
                    self.assertNotIn("idempotentHint", annotations)
            finally:
                pipe.close()

    def test_duplicate_key_does_not_deliver_again_and_conflict_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            state = directory / "state"
            config = write_config(directory, state)
            pipe = Pipe(config)
            try:
                pipe.start()
                first = pipe.call("send", {
                    "recipient": "beta", "key": "same", "sender": "alpha", "note": "one",
                })
                first_body = first["result"]["structuredContent"]
                self.assertEqual(first_body["status"], "STORED")
                attempt = first_body["attempt_id"]
                second = pipe.call("send", {
                    "recipient": "beta", "key": "same", "sender": "alpha", "note": "one",
                })
                replay = second["result"]["structuredContent"]
                self.assertFalse(second["result"]["isError"])
                self.assertIs(replay["idempotent_replay"], True)
                self.assertEqual(replay["status"], "STORED")
                self.assertEqual(replay["attempt_id"], attempt)
                self.assertEqual(payloads(state, "worker", "beta"), [b"Check same from alpha. one"])
                conflict = pipe.call("send", {
                    "recipient": "beta", "key": "same", "sender": "alpha", "note": "two",
                })
                self.assertTrue(conflict["result"]["isError"])
                detail = conflict["result"]["structuredContent"]["detail"]
                self.assertIn("different payload", detail)
                self.assertIn("did not mint a new key", detail)
                self.assertEqual(payloads(state, "worker", "beta"), [b"Check same from alpha. one"])
            finally:
                pipe.close()

    def test_inbox_marks_unsigned_untrusted_and_verified_signatures(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            state = directory / "state"
            config = write_config(directory, state)
            store = directory / "store.sqlite"
            token = "UNSIGNED-BODY-TOKEN"
            signed_token = "SIGNED-BODY-TOKEN"
            pipe = Pipe(config, ("--meta-store", str(store)))
            try:
                pipe.start()
                listed = json.dumps(pipe.request("tools/list")["result"])
                self.assertNotIn(token, listed)
                self.assertNotIn(signed_token, listed)
                sent = pipe.call("send", {
                    "recipient": "beta", "key": "plain", "sender": "alpha", "note": token,
                })
                self.assertEqual(sent["result"]["structuredContent"]["status"], "STORED")
                carrier = _signed_carrier(store, signed_token)
                if carrier is None:
                    inbox = pipe.call("read_inbox", {"recipient": "beta"})
                    message = inbox["result"]["structuredContent"]["messages"][0]
                    self.assertEqual(message["body"], "Check plain from alpha. " + token)
                    self.assertEqual(message["provenance"]["sender"], "alpha")
                    self.assertEqual(message["provenance"]["transport"], "worker")
                    self.assertEqual(message["provenance"]["trust"], "untrusted")
                    self.assertEqual(message["provenance"]["meta_coura"]["signature"], "not_a_carrier")
                    described = json.dumps(pipe.request("tools/list")["result"])
                    self.assertNotIn(token, described)
                    print("UNRUN verified-signed inbox provenance: Meta-Coura verification was not available")
                    return
                sys.path.insert(0, str(ROOT))
                import courier
                instance = courier.Courier.from_config(config)
                stored = instance.send_data("beta", "signed-key", carrier)
                self.assertEqual(stored.status, "STORED")
                inbox = pipe.call("read_inbox", {"recipient": "beta"})
                messages = inbox["result"]["structuredContent"]["messages"]
                self.assertEqual(len(messages), 2)
                unsigned = messages[0]
                verified = messages[1]
                self.assertEqual(unsigned["body"], "Check plain from alpha. " + token)
                self.assertEqual(unsigned["provenance"]["sender"], "alpha")
                self.assertEqual(unsigned["provenance"]["transport"], "worker")
                self.assertEqual(unsigned["provenance"]["trust"], "untrusted")
                self.assertEqual(unsigned["provenance"]["meta_coura"]["signature"], "not_a_carrier")
                self.assertEqual(verified["body"].encode("utf-8"), carrier)
                inner = base64.b64decode(json.loads(verified["body"])["body"])
                self.assertIn(signed_token.encode("ascii"), inner)
                self.assertEqual(verified["provenance"]["trust"], "signature_verified")
                self.assertEqual(verified["provenance"]["meta_coura"]["signature"], "verified")
                self.assertNotIn("authentic", json.dumps(verified).lower())
                self.assertIsNone(verified["provenance"]["sender"])
                described = json.dumps(pipe.request("tools/list")["result"])
                self.assertNotIn(token, described)
                self.assertNotIn(signed_token, described)
                import meta_coura
                receiver = meta_coura.Receiver(store, {"kind": "worker", "id": "beta"}, ())
                self.assertEqual(receiver.effects(), [])
            finally:
                pipe.close()

    def test_size_and_rate_limits_refuse_without_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            state = directory / "state"
            config = write_config(directory, state)
            limited = Pipe(config, ("--max-bytes", "32", "--send-rate", "1", "--rate-window", "120"))
            try:
                limited.start()
                long_note = "this-note-is-over-the-byte-limit"
                refused = limited.call("send", {
                    "recipient": "beta", "key": "big", "sender": "alpha", "note": long_note,
                })
                self.assertTrue(refused["result"]["isError"])
                self.assertIn("Not truncated and not sent", refused["result"]["structuredContent"]["detail"])
                self.assertEqual(payloads(state, "worker", "beta"), [])
                accepted = limited.call("send", {
                    "recipient": "beta", "key": "small", "sender": "alpha", "note": "ok",
                })
                self.assertEqual(accepted["result"]["structuredContent"]["status"], "STORED")
                self.assertEqual(payloads(state, "worker", "beta"), [b"Check small from alpha. ok"])
                self.assertNotIn(long_note.encode("ascii"), b"".join(payloads(state, "worker", "beta")))
                rated = limited.call("send", {
                    "recipient": "beta", "key": "later", "sender": "alpha", "note": "ok",
                })
                self.assertTrue(rated["result"]["isError"])
                self.assertIn("Send rate limit exceeded", rated["result"]["structuredContent"]["detail"])
                self.assertEqual(payloads(state, "worker", "beta"), [b"Check small from alpha. ok"])
            finally:
                limited.close()

    def test_duplicate_json_key_is_not_delivered(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            state = directory / "state"
            config = write_config(directory, state)
            pipe = Pipe(config)
            try:
                pipe.start()
                pipe.write_raw(
                    b'{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"send",'
                    b'"arguments":{"recipient":"beta","key":"dup","sender":"alpha",'
                    b'"note":"safe","note":"evil"}}}\n')
                response = pipe._read()
                self.assertIn("error", response, "duplicate JSON key was accepted")
                self.assertEqual(response["error"]["code"], -32700)
                self.assertEqual(payloads(state, "worker", "beta"), [])
                listed = pipe.request("tools/list")
                self.assertIn("result", listed)
            finally:
                pipe.close()

    def test_send_follows_the_held_outcome(self):
        sys.path.insert(0, str(ROOT))
        import coura_mcp
        import courier
        line = "Check held from alpha. one"
        digest = hashlib.sha256(line.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            state = directory / "state"
            config = write_config(directory, state)
            journal = state / "journal.jsonl"
            journal.parent.mkdir()
            refused = {
                "client": "worker", "id": "beta", "key": "held", "sender": "alpha",
                "attempt": "aa" * 16, "attempted_body_sha256": digest,
                "status": "REFUSED", "dispatch": "NOT_STARTED", "detail": "child failed",
            }
            requested = {
                "client": "worker", "id": "beta", "key": "held", "sender": "alpha",
                "attempt": "bb" * 16, "attempted_body_sha256": digest,
                "status": "REQUESTED", "dispatch": "POSSIBLE", "detail": "",
            }
            journal.write_text(
                json.dumps(refused) + "\n" + json.dumps(requested) + "\n", encoding="utf-8")
            instance = courier.Courier.from_config(config)
            adapter = coura_mcp.Adapter(instance, 4096, 30, 60, None)
            replay = adapter.send({
                "recipient": "beta", "key": "held", "sender": "alpha", "note": "one",
            })
            body = replay["structuredContent"]
            self.assertEqual(body["status"], "REQUESTED")
            self.assertEqual(body["attempt_id"], "bb" * 16)
            self.assertIs(body["idempotent_replay"], True)
            self.assertEqual(payloads(state, "worker", "beta"), [])
            status = adapter.delivery_status({"recipient": "beta", "key": "held"})
            self.assertEqual(status["structuredContent"]["status"], "REQUESTED")
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            state = directory / "state"
            config = write_config(directory, state)
            journal = state / "journal.jsonl"
            journal.parent.mkdir()
            journal.write_text(json.dumps({
                "client": "worker", "id": "beta", "key": "retry", "sender": "alpha",
                "attempt": "cc" * 16, "attempted_body_sha256": hashlib.sha256(
                    b"Check retry from alpha. one").hexdigest(),
                "status": "REFUSED", "dispatch": "NOT_STARTED", "detail": "child failed",
            }) + "\n", encoding="utf-8")
            instance = courier.Courier.from_config(config)
            adapter = coura_mcp.Adapter(instance, 4096, 30, 60, None)
            sent = adapter.send({
                "recipient": "beta", "key": "retry", "sender": "alpha", "note": "one",
            })
            self.assertEqual(sent["structuredContent"]["status"], "STORED")
            self.assertEqual(payloads(state, "worker", "beta"), [b"Check retry from alpha. one"])

    def _race(self, first_note, second_note):
        """Two adapters over one state, as two MCP processes. The second reads the journal (its check outside the
        core lock), then the first sends the same key to the end, then the second reaches the core: a fixed
        interleaving of the race, not a timing one. -> (first result, second result, delivered payloads)."""
        sys.path.insert(0, str(ROOT))
        import coura_mcp
        import courier
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            state = directory / "state"
            config = write_config(directory, state)
            first = coura_mcp.Adapter(courier.Courier.from_config(config), 4096, 30, 60, None)
            second = coura_mcp.Adapter(courier.Courier.from_config(config), 4096, 30, 60, None)
            read_journal, raced = second._prior, []

            def prior_then_the_other_process(target, key, digest):
                seen = read_journal(target, key, digest)
                if not raced:
                    raced.append(first.send({
                        "recipient": "beta", "key": "race", "sender": "alpha", "note": first_note,
                    }))
                return seen

            second._prior = prior_then_the_other_process
            result = second.send({"recipient": "beta", "key": "race", "sender": "alpha", "note": second_note})
            return raced[0], result, payloads(state, "worker", "beta")

    def test_send_race_with_another_payload_is_refused(self):
        won, lost, delivered = self._race("one", "two")
        self.assertEqual(won["structuredContent"]["status"], "STORED")
        self.assertTrue(lost["isError"])
        self.assertEqual(lost["structuredContent"]["status"], "REFUSED")
        self.assertIn("different payload", lost["structuredContent"]["detail"])
        self.assertEqual(delivered, [b"Check race from alpha. one"])

    def test_send_race_with_the_same_payload_replays(self):
        won, lost, delivered = self._race("one", "one")
        self.assertEqual(won["structuredContent"]["status"], "STORED")
        self.assertFalse(lost["isError"])
        self.assertIs(lost["structuredContent"]["idempotent_replay"], True)
        self.assertEqual(lost["structuredContent"]["attempt_id"], won["structuredContent"]["attempt_id"])
        self.assertEqual(delivered, [b"Check race from alpha. one"])

    def test_rate_table_drops_expired_senders_and_caps_distinct_senders(self):
        sys.path.insert(0, str(ROOT))
        import coura_mcp
        stale = coura_mcp.SendRate(1, 60)
        stale.hits["expired"] = [time.monotonic() - 120]
        self.assertTrue(stale.allow("fresh"))
        self.assertNotIn("expired", stale.hits)
        self.assertEqual(set(stale.hits), {"fresh"})
        capped = coura_mcp.SendRate(1, 60)
        for index in range(coura_mcp.MAX_RATE_SENDERS):
            self.assertTrue(capped.allow("sender-%s" % index))
        self.assertFalse(capped.allow("one-more"))
        self.assertNotIn("one-more", capped.hits)
        self.assertLessEqual(len(capped.hits), coura_mcp.MAX_RATE_SENDERS)
        self.assertFalse(capped.allow("sender-0"))


    def _cursor_fixture(self):
        sys.path.insert(0, str(ROOT))
        import courier
        import coura_mcp
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        config = write_config(directory, directory / "state")
        instance = courier.Courier.from_config(config)
        sent = instance.send("beta", "cursor", "alpha", "retained-body")
        self.assertEqual(sent.status, "STORED", "fixture must store a real message")
        adapter = coura_mcp.Adapter(instance, 4096, 30, 60, None)
        session = coura_mcp.Session(adapter)
        started = session.dispatch({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "cursor-test", "version": "0"}},
        })
        self.assertIn("result", started)
        session.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        paths = instance.reader(instance.resolve("beta")).paths(instance.resolve("beta"))
        self.assertTrue(paths["jsonl"].is_file(), "fixture inbox must exist")
        return coura_mcp, instance, adapter, session, paths

    @staticmethod
    def _cursor_bytes(paths):
        return {name: paths[name].read_bytes() if paths[name].exists() else None
                for name in ("offset", "identity")}

    @staticmethod
    def _read_cursor_call(session):
        return session.dispatch({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "read_inbox", "arguments": {"recipient": "beta"}},
        })

    def _assert_cursor_body_once(self, session):
        first = self._read_cursor_call(session)
        self.assertFalse(first["result"]["isError"])
        body = first["result"]["structuredContent"]
        self.assertTrue(body["cursor_advanced"])
        self.assertEqual([row["body"] for row in body["messages"]],
                         ["Check cursor from alpha. retained-body"])
        second = self._read_cursor_call(session)["result"]["structuredContent"]
        self.assertFalse(second["cursor_advanced"])
        self.assertEqual(second["messages"], [])

    def test_read_cursor_0_success(self):
        _mcp, _instance, _adapter, session, paths = self._cursor_fixture()
        before = self._cursor_bytes(paths)
        self._assert_cursor_body_once(session)
        self.assertNotEqual(self._cursor_bytes(paths), before)

    def test_read_cursor_1_journal_failure(self):
        _mcp, instance, _adapter, session, paths = self._cursor_fixture()
        before = self._cursor_bytes(paths)
        journal = instance.state / "journal.jsonl"
        original = journal.read_bytes()
        with journal.open("ab") as stream:
            stream.write(b'{"status":"TORN"')
        response = self._read_cursor_call(session)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["status"], "REFUSED")
        self.assertIn("Incomplete or oversized journal record",
                      response["result"]["structuredContent"]["detail"])
        self.assertEqual(self._cursor_bytes(paths), before)
        journal.write_bytes(original)
        self._assert_cursor_body_once(session)

    def test_read_cursor_2_message_failure(self):
        mcp, _instance, adapter, session, paths = self._cursor_fixture()
        before = self._cursor_bytes(paths)
        with patch.object(adapter, "_message", side_effect=RuntimeError("message unavailable")):
            with contextlib.redirect_stderr(io.StringIO()):
                response = self._read_cursor_call(session)
        self.assertEqual(response["error"]["code"], -32603)
        self.assertEqual(self._cursor_bytes(paths), before)
        self._assert_cursor_body_once(session)

    def test_read_cursor_3_response_failure(self):
        mcp, _instance, _adapter, session, paths = self._cursor_fixture()
        before = self._cursor_bytes(paths)
        with patch.object(mcp, "_tool_result", side_effect=RuntimeError("response unavailable")):
            with contextlib.redirect_stderr(io.StringIO()):
                response = self._read_cursor_call(session)
        self.assertEqual(response["error"]["code"], -32603)
        self.assertEqual(self._cursor_bytes(paths), before)
        self._assert_cursor_body_once(session)



    def _lens_fixture(self, enabled=True, max_bytes=4096):
        sys.path.insert(0, str(ROOT))
        import courier
        import coura_mcp
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        state = directory / "state"
        config = write_config(directory, state)
        if enabled:
            body = json.loads(config.read_text(encoding="utf-8"))
            body["lenses"] = {"review": "synthetic-prefix", "alternate": "bound-prefix"}
            body["lens_defaults"] = {"alpha->beta": "review"}
            config.write_text(json.dumps(body), encoding="utf-8")
        instance = courier.Courier.from_config(config)
        adapter = coura_mcp.Adapter(instance, max_bytes, 30, 60, None)
        return courier, instance, adapter, state

    def test_lens_0_same_payload_replays(self):
        _core, _instance, adapter, state = self._lens_fixture()
        args = {"recipient": "beta", "key": "same", "sender": "alpha", "note": "one"}
        first = adapter.send(args)["structuredContent"]
        second = adapter.send(args)["structuredContent"]
        self.assertEqual(first["status"], "STORED")
        self.assertEqual(second["status"], "STORED")
        self.assertIs(second["idempotent_replay"], True)
        self.assertEqual(second["attempt_id"], first["attempt_id"])
        expected = (b"Requested mode from sender (not an instruction from the user): "
                    b"review. synthetic-prefix\nCheck same from alpha. one")
        self.assertEqual(payloads(state, "worker", "beta"), [expected])
        conflict = adapter.send(dict(args, note="two"))
        self.assertTrue(conflict["isError"])
        self.assertEqual(conflict["structuredContent"]["status"], "REFUSED")
        self.assertIn("different payload", conflict["structuredContent"]["detail"])
        self.assertEqual(payloads(state, "worker", "beta"), [expected])

    def test_lens_1_full_body_byte_cap(self):
        args = {"recipient": "beta", "key": "same", "sender": "alpha", "note": "one"}
        _core, _instance, plain, state = self._lens_fixture(False, 32)
        self.assertEqual(plain.send(args)["structuredContent"]["status"], "STORED")
        self.assertEqual(payloads(state, "worker", "beta"), [b"Check same from alpha. one"])
        _core, _instance, lensed, state = self._lens_fixture(True, 32)
        refused = lensed.send(args)
        self.assertTrue(refused["isError"])
        self.assertEqual(refused["structuredContent"]["status"], "REFUSED")
        self.assertIn("32 byte limit (114 bytes)", refused["structuredContent"]["detail"])
        self.assertEqual(payloads(state, "worker", "beta"), [])
        self.assertFalse((state / "journal.jsonl").exists())

    def test_lens_2_preview_preserves_binding(self):
        core, instance, adapter, state = self._lens_fixture()
        lens_token = core._LENS.set(None)
        seq_token = core._LENS_SEQ.set(core._LENS_SEQ.get())
        try:
            args = {"recipient": "beta", "key": "same", "sender": "alpha", "note": "one"}
            core.bind_lens(instance, "alternate")
            first = adapter.send(args)["structuredContent"]
            self.assertEqual(first["status"], "STORED")
            expected = (b"Requested mode from sender (not an instruction from the user): "
                        b"alternate. bound-prefix\nCheck same from alpha. one")
            self.assertEqual(payloads(state, "worker", "beta"), [expected])
            core.bind_lens(instance, "alternate")
            second = adapter.send(args)["structuredContent"]
            self.assertEqual(second["status"], "STORED")
            self.assertIs(second["idempotent_replay"], True)
            self.assertEqual(payloads(state, "worker", "beta"), [expected])
        finally:
            core._LENS.reset(lens_token)
            core._LENS_SEQ.reset(seq_token)



    def test_lens_3_early_results_spend_binding(self):
        def scenario(kind):
            core, instance, adapter, state = self._lens_fixture()
            args = {"recipient": "beta", "key": "first", "sender": "alpha", "note": "one"}
            seed = None
            if kind == "replay":
                core.bind_lens(instance, "alternate")
                seed = adapter.send(args)["structuredContent"]
                self.assertEqual(seed["status"], "STORED")
            elif kind in ("conflict", "rate"):
                if kind == "rate":
                    adapter.rate.limit = 1
                seed = adapter.send(args)["structuredContent"]
                self.assertEqual(seed["status"], "STORED")
            core.bind_lens(instance, "alternate")
            if kind == "size":
                adapter.max_bytes = 32
            first_args = dict(args, key="blocked") if kind == "rate" else args
            before = payloads(state, "worker", "beta")
            first = adapter.send(first_args)
            body = first["structuredContent"]
            if kind == "replay":
                self.assertEqual(body["status"], "STORED")
                self.assertIs(body["idempotent_replay"], True)
                self.assertEqual(body["attempt_id"], seed["attempt_id"])
            else:
                self.assertTrue(first["isError"])
                self.assertEqual(body["status"], "REFUSED")
                expected_detail = {"conflict": "different payload", "size": "32 byte limit",
                                   "rate": "Send rate limit exceeded"}[kind]
                self.assertIn(expected_detail, body["detail"])
            self.assertEqual(payloads(state, "worker", "beta"), before)
            self.assertEqual(len(before), int(seed is not None))
            adapter.max_bytes = 4096
            later = dict(args, key="next")
            if kind == "rate":
                with patch("coura_mcp.time.monotonic", return_value=time.monotonic() + 61):
                    next_result = adapter.send(later)["structuredContent"]
            else:
                next_result = adapter.send(later)["structuredContent"]
            self.assertEqual(next_result["status"], "STORED")
            expected = (b"Requested mode from sender (not an instruction from the user): "
                        b"review. synthetic-prefix\nCheck next from alpha. one")
            self.assertEqual(payloads(state, "worker", "beta")[-1], expected)
            self.assertEqual(len(payloads(state, "worker", "beta")), int(seed is not None) + 1)
        for kind in ("replay", "conflict", "size", "rate"):
            contextvars.Context().run(scenario, kind)



def _signed_carrier(store, text):
    try:
        sys.path.insert(0, str(ROOT))
        import meta_coura
        meta_coura.primitive()
    except Exception as error:
        print("UNRUN verified-signed inbox provenance: %s" % type(error).__name__)
        return None
    private, public = meta_coura.generate_keypair()
    now = int(time.time())
    receiver = meta_coura.Receiver(store, {"kind": "worker", "id": "beta"}, ())
    receiver.register(
        "principal-a", "delegate-a", "key-1", public, ["note"], [],
        now - 10, now + 500, "rev-1")
    body = {
        "profile": meta_coura.PROFILE,
        "principal": "principal-a",
        "delegate": "delegate-a",
        "key_id": "key-1",
        "recipient": {"kind": "worker", "id": "beta"},
        "request_id": str(uuid.uuid4()),
        "envelope_id": str(uuid.uuid4()),
        "issued_at": now,
        "expires_at": now + 100,
        "policy_revision": "rev-1",
        "request_kind": "note",
        "payload": {"text": text},
    }
    return meta_coura.sign(body, private)


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
