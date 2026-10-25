"""Usability commands, the one-file archive, and unchanged machine codes."""
import base64
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import courier
import coura_pair
import coura_remote
import coura_ux
import build_coura_pyz


BEFORE = """\
clean install before the usability commands (recorded, not a pass):
1. An empty directory has no coura command.
2. python3 courier.py with no file: can't open file.
3. python3 courier.py with no command: argparse says command is required.
4. demo works and prints raw JSON plus a lens header.
5. send and doctor without --config: --config is required.
6. A missing config file: doctor says "Exception detail not copied; inspect state".
7. Bad JSON: the same opaque doctor line.
8. An empty config object: stderr JSON {"status": "REFUSED", "detail": "Explicit state_dir required"}.
9. --help leads with hook and observer commands, not with setup.
"""


def snapshot(root):
    found = []
    for path in sorted(Path(root).rglob("*")):
        info = path.stat()
        body = path.read_bytes() if path.is_file() else None
        found.append((str(path.relative_to(root)), info.st_mode, info.st_mtime_ns, body))
    return found


def run_pyz(pyz, args, cwd, stdin=None, timeout=None):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(pyz), *args], cwd=str(cwd), env=env,
        input=stdin, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout)


def test_ed25519_matches_the_rfc_vector():
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    public = coura_pair.public_key(seed)
    signature = coura_pair.sign(seed, b"")
    if public.hex() != "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a":
        raise AssertionError("public key drifted")
    if signature.hex() != (
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"):
        raise AssertionError("signature drifted")
    if not coura_pair.verify(public, b"", signature) or coura_pair.verify(public, b"no", signature):
        raise AssertionError("verify disagreed with the vector")


def test_qr_roundtrip_carries_the_code():
    text = "coura:ABCD-EFGH"
    picture = coura_pair.qr_text(text)
    if coura_pair.qr_decode(picture) != text:
        raise AssertionError("QR did not carry the code")
    if coura_pair._format_bits(0) != 0b111011111000100:
        raise AssertionError("QR format bits drifted")


def test_pyz_build_is_reproducible():
    with tempfile.TemporaryDirectory() as folder:
        first = Path(folder) / "a.pyz"
        second = Path(folder) / "b.pyz"
        build_coura_pyz.build(first)
        build_coura_pyz.build(second)
        if first.read_bytes() != second.read_bytes():
            raise AssertionError("two builds of coura.pyz differed")
        if not first.read_bytes().startswith(b"#!/usr/bin/env python3\n"):
            raise AssertionError("pyz is missing the portable shebang")


def test_pyz_clean_dir_first_run():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        archive = build_coura_pyz.build(root / "build" / "coura.pyz")
        clean = root / "clean"
        clean.mkdir()
        target = clean / "coura.pyz"
        target.write_bytes(archive.read_bytes())
        bare = run_pyz(target, [], clean)
        if bare.returncode != 2 or "setup" not in bare.stdout:
            raise AssertionError("a bare pyz did not point at setup: %s" % bare.stderr)
        setup = run_pyz(target, [
            "setup", "--non-interactive", "--node", "desk", "--kind", "computer",
            "--recipient", "writer=worker:writer", "--config", "coura.json"], clean)
        if setup.returncode != 0:
            raise AssertionError(setup.stderr)
        if (clean / "courier.py").exists():
            raise AssertionError("the clean directory gained a source file")
        doctor = run_pyz(target, ["doctor", "--config", "coura.json"], clean)
        status = run_pyz(target, ["status", "--config", "coura.json"], clean)
        demo = run_pyz(target, ["demo"], clean)
        if doctor.returncode != 0 or "No problems found." not in doctor.stdout:
            raise AssertionError(doctor.stdout + doctor.stderr)
        if "This node: desk" not in status.stdout:
            raise AssertionError(status.stdout)
        if demo.returncode != 0 or "MAIL_READY" not in demo.stdout:
            raise AssertionError(demo.stdout + demo.stderr)
        instance = courier.Courier.from_config(clean / "coura.json")
        if instance.recipients["writer"].identity != "writer":
            raise AssertionError("setup config did not load")
        print("clean install after: copy coura.pyz, setup, doctor, status, demo")


def test_setup_refuses_to_overwrite():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = root / "coura.json"
        first = coura_ux.write_setup(config, root / "state", "desk", "computer",
                                     ["writer=worker:writer"], False)
        original = config.read_bytes()
        try:
            coura_ux.write_setup(config, root / "state", "other", "computer", [], False)
        except courier.Refused as error:
            if "did not change" not in str(error):
                raise AssertionError(error)
        else:
            raise AssertionError("setup overwrote an existing config")
        if config.read_bytes() != original or "other" in config.read_text(encoding="utf-8"):
            raise AssertionError("config bytes changed")
        if first["recipients"]["desk"]["id"] != "desk":
            raise AssertionError("node recipient missing")
        out = io.StringIO()
        coura_ux.setup_interactive(io.StringIO("phone\nphone\nwriter\nworker\nwriter\n\n"),
                                   out, root / "other.json", False)
        other = json.loads((root / "other.json").read_text(encoding="utf-8"))
        if "writer" not in other["recipients"] or other["recipients"]["phone"]["client"] != "phone":
            raise AssertionError("interactive setup did not keep the answers")


def test_doctor_makes_no_changes():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = root / "coura.json"
        coura_ux.write_setup(config, root / "state", "desk", "computer",
                             ["writer=worker:writer"], False)
        state = root / "state"
        os.chmod(state, 0o755)
        config.write_text(config.read_text(encoding="utf-8").rstrip()[:-1] +
                          ', "observer": {"destination": "missing", "include_body": false}}\n',
                          encoding="utf-8")
        before = snapshot(root)
        report = coura_ux.doctor_report(config)
        after = snapshot(root)
        if before != after:
            raise AssertionError("doctor changed the directory")
        text = "\n".join(item["fix"] for item in report["problems"])
        codes = [item["code"] for item in report["problems"]]
        if "CAPTURE_FAILED" not in codes or "STATE_NOT_PRIVATE" not in codes:
            raise AssertionError(codes)
        if coura_ux.explain("CAPTURE_FAILED") not in text:
            raise AssertionError(text)
        if stat.S_IMODE(state.stat().st_mode) != 0o755:
            raise AssertionError("doctor changed permissions")
        out = io.StringIO()
        if coura_ux.emit_doctor(report, out) == 0:
            raise AssertionError("doctor hid problems")


def _pair_configs(root):
    computer_state = root / "computer" / "state"
    phone_state = root / "phone" / "state"
    computer_state.mkdir(parents=True)
    phone_state.mkdir(parents=True)
    (computer_state / "node.json").write_text(
        json.dumps({"name": "desk", "kind": "computer", "id": "desk"}), encoding="utf-8")
    (phone_state / "node.json").write_text(
        json.dumps({"name": "handset", "kind": "phone", "id": "handset"}), encoding="utf-8")

    def receive(state, kind, identity):
        return [sys.executable, str(ROOT / "coura_remote.py"), "receive",
                "--state", str(state), "--expect-kind", kind, "--expect-id", identity]

    def write(path, state, command, alias, client, identity, self_alias, self_client, self_id):
        path.write_text(json.dumps({
            "state_dir": str(state),
            "transports": {
                self_client: {"type": "inbox"},
                client: {
                    "type": "command", "command": command, "timeout": 30,
                    "data_only": True, "receipt_status": "STORED",
                },
            },
            "recipients": {
                self_alias: {"client": self_client, "id": self_id},
                alias: {"client": client, "id": identity},
            },
        }), encoding="utf-8")

    computer = root / "computer" / "coura.json"
    phone = root / "phone" / "coura.json"
    write(computer, computer_state, receive(phone_state, "phone", "handset"),
          "handset", "phone", "handset", "desk", "computer", "desk")
    write(phone, phone_state, receive(computer_state, "computer", "desk"),
          "desk", "computer", "desk", "handset", "phone", "handset")
    return computer, phone


def test_pairing_confirms_both_sides_and_hides_the_secret():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        computer, phone = _pair_configs(root)
        out = io.StringIO()
        offer = argparse_offer(computer, out, 1_700_000_000, 600)
        code = offer
        if "Pairing code:" not in out.getvalue():
            raise AssertionError("offer did not print a code")
        picture = out.getvalue().split("Pairing code: ", 1)[1]
        printed = picture.splitlines()[0].strip()
        if coura_pair.normalize_code(printed) != code:
            raise AssertionError("printed code mismatch")
        qr_text = out.getvalue().split("works once.\n", 1)[1]
        if coura_pair.qr_decode(qr_text) != "coura:" + code:
            raise AssertionError("QR did not contain the pairing code")
        offer_file = (root / "computer" / "state" / "pairing" / "offer.json").read_text(encoding="utf-8")
        if code.replace("-", "") in offer_file.replace("-", ""):
            raise AssertionError("the offer file stored the raw code")
        joined = namespace(phone, confirm=False, now=1_700_000_000)
        joined.code = code
        joined.to = "desk"
        joined.pair_command = "join"
        if coura_ux._pair_join(joined, io.StringIO()) != 0:
            raise AssertionError("join failed")
        plant(root / "computer" / "state", "computer", "desk",
              b'{"schema":"coura.pair.join.v1","code":"ZZZZ-ZZZZ"}', "44" * 16)
        preview = coura_ux._pair_confirm(namespace(computer, confirm=False, now=1_700_000_000), io.StringIO())
        if preview != 0 or (root / "computer" / "state" / "pairing" / "peers.json").exists():
            raise AssertionError("confirm without --confirm trusted a peer")
        confirmed = io.StringIO()
        if coura_ux._pair_confirm(namespace(computer, confirm=True, now=1_700_000_000), confirmed) != 0:
            raise AssertionError(confirmed.getvalue())
        if "Not trusted" in confirmed.getvalue():
            raise AssertionError("confirmed pairing stayed untrusted")
        plant(root / "phone" / "state", "phone", "handset",
              b'{"schema":"coura.pair.accept.v1","code":"ZZZZ-ZZZZ"}', "55" * 16)
        finish_quiet = io.StringIO()
        if coura_ux._pair_finish(namespace(phone, confirm=False, now=1_700_000_000), finish_quiet) != 0:
            raise AssertionError(finish_quiet.getvalue())
        if (root / "phone" / "state" / "pairing" / "peers.json").exists():
            raise AssertionError("finish without --confirm saved a peer")
        finish = io.StringIO()
        if coura_ux._pair_finish(namespace(phone, confirm=True, now=1_700_000_000), finish) != 0:
            raise AssertionError(finish.getvalue())
        computer_secret = (root / "computer" / "state" / "identity" / "secret.key").read_bytes()
        phone_secret = (root / "phone" / "state" / "identity" / "secret.key").read_bytes()
        visible = b""
        for path in root.rglob("*"):
            if path.is_file() and path.name != "secret.key":
                visible += path.read_bytes()
        for secret in (computer_secret, phone_secret):
            if secret in visible or secret.hex().encode("ascii") in visible:
                raise AssertionError("a secret key left its file")
        peers = json.loads((root / "phone" / "state" / "pairing" / "peers.json").read_text(encoding="utf-8"))
        if peers[coura_pair.peer_key("computer", "desk")]["public_key"] != (root / "computer" / "state" / "identity" / "public.key").read_bytes().hex():
            raise AssertionError("the phone did not keep the computer public key")


def argparse_offer(config, out, now, ttl):
    args = namespace(config, confirm=False, now=now)
    args.kind = "computer"
    args.id = "desk"
    args.ttl = ttl
    args.pair_command = "offer"
    if coura_ux._pair_offer(args, out) != 0:
        raise AssertionError("offer failed")
    return out.getvalue().split("Pairing code: ", 1)[1].splitlines()[0].strip()


def namespace(config, confirm, now):
    class Args:
        pass
    args = Args()
    args.config = str(config)
    args.confirm = confirm
    args.now = now
    args.kind = None
    args.id = None
    args.pair_command = "confirm"
    return args


def run_main(argv):
    return coura_ux.main(argv)


def test_pairing_wrong_expired_and_reused_codes():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        computer, phone = _pair_configs(root)
        code = coura_pair.create_offer(root / "computer" / "state", "computer", "desk", 600, 1_700_000_000)
        wrong = "ZZZZ-ZZZZ" if code != "ZZZZ-ZZZZ" else "YYYY-YYYY"
        payload = coura_pair.join_payload(root / "phone" / "state", wrong, "phone", "handset")
        try:
            coura_pair.review_join(root / "computer" / "state", payload, 1_700_000_000)
        except coura_pair.PairError as error:
            if "does not match" not in str(error):
                raise AssertionError(error)
        else:
            raise AssertionError("wrong code was accepted")
        offer = json.loads((root / "computer" / "state" / "pairing" / "offer.json").read_text(encoding="utf-8"))
        if offer["used"] is not False:
            raise AssertionError("a wrong code spent the offer")
        offer["expires"] = 1_700_000_000
        (root / "computer" / "state" / "pairing" / "offer.json").write_text(
            json.dumps(offer), encoding="utf-8")
        expired_code = argparse_offer(computer, io.StringIO(), 1_700_000_000, 0)
        expired = coura_pair.join_payload(root / "phone" / "state", expired_code, "phone", "handset")
        try:
            coura_pair.confirm_join(root / "computer" / "state", expired, 1_700_000_000)
        except coura_pair.PairError as error:
            if "expired" not in str(error):
                raise AssertionError(error)
        else:
            raise AssertionError("expired code was accepted")
        if json.loads((root / "computer" / "state" / "pairing" / "offer.json").read_text(encoding="utf-8"))["used"]:
            raise AssertionError("an expired code was spent")
        live = coura_pair.create_offer(root / "computer" / "state", "computer", "desk", 600, 1_700_000_000)
        good = coura_pair.join_payload(root / "phone" / "state", live, "phone", "handset")
        coura_pair.confirm_join(root / "computer" / "state", good, 1_700_000_000)
        try:
            coura_pair.confirm_join(root / "computer" / "state", good, 1_700_000_000)
        except coura_pair.PairError as error:
            if "already used" not in str(error):
                raise AssertionError(error)
        else:
            raise AssertionError("a used code worked twice")


def test_status_and_autostart():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = root / "coura.json"
        coura_ux.write_setup(config, root / "state", "desk", "computer",
                             ["writer=worker:writer"], False)
        body = json.loads(config.read_text(encoding="utf-8"))
        body["observer"] = {"destination": "missing", "include_body": False}
        config.write_text(json.dumps(body), encoding="utf-8")
        script = root / "drop.py"
        script.write_text("import sys\nraise SystemExit(1)\n", encoding="utf-8")
        # A normal inbox send records CAPTURE_FAILED beside a stored delivery.
        before = snapshot(root)
        text = coura_ux.status_text(config)
        if snapshot(root) != before:
            raise AssertionError("status changed the directory")
        if "This node: desk" not in text or "CAPTURE_FAILED" not in text:
            raise AssertionError(text)
        if coura_ux.explain("CAPTURE_FAILED") not in text:
            raise AssertionError("status omitted the plain observer sentence")
        printed = coura_ux.autostart_text(config, "python3 coura.pyz")
        if "does not install" not in printed or "observer-run" not in printed:
            raise AssertionError(printed)
        if "not measured" not in printed:
            raise AssertionError("autostart marked Termux as measured")
        again = snapshot(root)
        previous = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            printed_code = coura_ux.main(
                ["autostart", "--print", "--config", str(config), "--program", "python3 coura.pyz"])
            refused = coura_ux.main(["autostart", "--config", str(config)])
            refused_text = sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr = previous
        if printed_code != 0:
            raise AssertionError("autostart --print failed")
        if snapshot(root) != again:
            raise AssertionError("autostart wrote a file")
        if refused == 0 or "does not install" not in refused_text:
            raise AssertionError("autostart without --print did not refuse")


def test_machine_codes_and_json_stay_unchanged():
    if courier.TERMINAL != {"STORED", "QUEUED", "THREAD_ACCEPTED", "REFUSED", "OUTCOME_UNKNOWN"}:
        raise AssertionError("terminal codes changed")
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = root / "coura.json"
        coura_ux.write_setup(config, root / "state", "desk", "computer",
                             ["writer=worker:writer"], False)
        stdout, stderr = io.StringIO(), io.StringIO()
        previous = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        try:
            code = courier.main(["--config", str(config), "send", "writer",
                                 "--key", "hello-1", "--sender", "desk", "--note", "Draft saved."])
        finally:
            sys.stdout, sys.stderr = previous
        if code != 0 or stderr.getvalue():
            raise AssertionError(stderr.getvalue())
        document = json.loads(stdout.getvalue())
        if document["status"] != "STORED" or "observation skipped" in stdout.getvalue():
            raise AssertionError(stdout.getvalue())
        if set(document) != {
                "status", "detail", "recipient_id", "queue_item_id", "held_status",
                "read", "recipient_kind", "attempt_id"}:
            raise AssertionError(sorted(document))
        empty = root / "empty.json"
        empty.write_text("{}\n", encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        sys.stdout, sys.stderr = stdout, stderr
        try:
            refused = courier.main(["--config", str(empty), "send", "writer",
                                    "--key", "hello-1", "--sender", "desk", "--note", "Draft saved."])
        finally:
            sys.stdout, sys.stderr = previous
        if refused != 1 or stdout.getvalue():
            raise AssertionError("refusal wrote stdout")
        if stderr.getvalue() != '{"status": "REFUSED", "detail": "Explicit state_dir required"}\n':
            raise AssertionError(stderr.getvalue())


def plant(state, kind, identity, payload, attempt):
    wire = coura_remote.canonical({
        "recipient": {"kind": kind, "id": identity},
        "attempt": attempt,
        "payload": base64.b64encode(payload).decode("ascii"),
    })
    ack = coura_remote.receive_bytes(state, wire, kind, identity)
    if ack["status"] != "STORED":
        raise AssertionError(ack)


def test_second_offer_is_refused_and_the_error_is_readable():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        computer, phone = _pair_configs(root)
        out = io.StringIO()
        code = argparse_offer(computer, out, 1_700_000_000, 600)
        offer_path = root / "computer" / "state" / "pairing" / "offer.json"
        digest = json.loads(offer_path.read_text(encoding="utf-8"))["code_sha256"]
        previous = sys.stderr
        sys.stderr = io.StringIO()
        try:
            refused = coura_ux.main([
                "pair", "offer", "--config", str(computer), "--now", "1700000000",
                "--ttl", "600", "--kind", "computer", "--id", "desk"])
            message = sys.stderr.getvalue()
        finally:
            sys.stderr = previous
        if refused == 0 or "not replaced" not in message:
            raise AssertionError(message)
        if "Exception detail not copied" in message:
            raise AssertionError(message)
        if json.loads(offer_path.read_text(encoding="utf-8"))["code_sha256"] != digest:
            raise AssertionError("a refused offer replaced the open code")
        payload = coura_pair.join_payload(root / "phone" / "state", code, "phone", "handset")
        preview = coura_pair.review_join(root / "computer" / "state", payload, 1_700_000_000)
        if preview["id"] != "handset":
            raise AssertionError("the first code no longer matches")
        wrong = namespace(phone, confirm=False, now=1_700_000_000)
        wrong.code = "ZZZZ-ZZZZ" if code != "ZZZZ-ZZZZ" else "YYYY-YYYY"
        wrong.to = "desk"
        wrong.pair_command = "join"
        if coura_ux._pair_join(wrong, io.StringIO()) != 0:
            raise AssertionError("wrong join was not stored")
        previous = sys.stderr
        sys.stderr = io.StringIO()
        try:
            confirmed = coura_ux.main([
                "pair", "confirm", "--confirm", "--config", str(computer),
                "--now", "1700000000"])
            detail = sys.stderr.getvalue()
        finally:
            sys.stderr = previous
        if confirmed == 0 or "does not match" not in detail:
            raise AssertionError(detail)
        if "Exception detail not copied" in detail:
            raise AssertionError(detail)


def test_confirm_does_not_trust_a_refused_or_unacknowledged_reply():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        computer, phone = _pair_configs(root)
        config = json.loads(computer.read_text(encoding="utf-8"))
        del config["transports"]["phone"]["data_only"]
        computer.write_text(json.dumps(config), encoding="utf-8")
        code = argparse_offer(computer, io.StringIO(), 1_700_000_000, 600)
        joined = namespace(phone, confirm=False, now=1_700_000_000)
        joined.code = code
        joined.to = "desk"
        joined.pair_command = "join"
        if coura_ux._pair_join(joined, io.StringIO()) != 0:
            raise AssertionError("join failed")
        refused = io.StringIO()
        if coura_ux._pair_confirm(namespace(computer, confirm=True, now=1_700_000_000), refused) == 0:
            raise AssertionError(refused.getvalue())
        if "Not trusted" not in refused.getvalue() or "sent the reply" in refused.getvalue():
            raise AssertionError(refused.getvalue())
        peers = root / "computer" / "state" / "pairing" / "peers.json"
        offer_path = root / "computer" / "state" / "pairing" / "offer.json"
        if peers.exists():
            raise AssertionError("a refused reply saved a peer")
        offer = json.loads(offer_path.read_text(encoding="utf-8"))
        if offer["used"] is not False or offer.get("accept_attempt") != 1:
            raise AssertionError("a refused reply spent the code: %r" % offer)
        config["transports"]["phone"]["data_only"] = True
        computer.write_text(json.dumps(config), encoding="utf-8")
        accepted = io.StringIO()
        if coura_ux._pair_confirm(namespace(computer, confirm=True, now=1_700_000_000), accepted) != 0:
            raise AssertionError(accepted.getvalue())
        if "sent the reply" not in accepted.getvalue():
            raise AssertionError(accepted.getvalue())
        saved = json.loads(peers.read_text(encoding="utf-8"))
        if saved[coura_pair.peer_key("phone", "handset")]["confirmed"] is not True:
            raise AssertionError("the retried reply was not trusted")

        root2 = root / "unknown"
        computer2, phone2 = _pair_configs(root2)
        code2 = argparse_offer(computer2, io.StringIO(), 1_700_000_000, 600)
        joined2 = namespace(phone2, confirm=False, now=1_700_000_000)
        joined2.code = code2
        joined2.to = "desk"
        joined2.pair_command = "join"
        if coura_ux._pair_join(joined2, io.StringIO()) != 0:
            raise AssertionError("second join failed")
        broken = json.loads(computer2.read_text(encoding="utf-8"))
        broken["transports"]["phone"]["command"] = [sys.executable, "-c", "import sys; raise SystemExit(1)"]
        computer2.write_text(json.dumps(broken), encoding="utf-8")
        unknown = io.StringIO()
        if coura_ux._pair_confirm(namespace(computer2, confirm=True, now=1_700_000_000), unknown) == 0:
            raise AssertionError(unknown.getvalue())
        if "sent the reply" in unknown.getvalue() or coura_ux.explain("OUTCOME_UNKNOWN") not in unknown.getvalue():
            raise AssertionError(unknown.getvalue())
        trusted = json.loads((root2 / "computer" / "state" / "pairing" / "peers.json").read_text(encoding="utf-8"))
        if trusted[coura_pair.peer_key("phone", "handset")]["confirmed"] is not True:
            raise AssertionError("a held reply did not record the confirm")
        phone_text = b"".join(
            base64.b64decode(row["payload_b64"]) for row in coura_remote.inbox_rows(root2 / "phone" / "state"))
        if coura_pair.ACCEPT_SCHEMA.encode("ascii") in phone_text:
            raise AssertionError("the failing command still stored a reply")
        fresh = coura_pair.create_offer(
            root2 / "computer" / "state", "computer", "desk", 600, 1_700_000_000)
        phone_config = json.loads(phone2.read_text(encoding="utf-8"))
        phone_config["transports"]["computer"]["command"] = [
            sys.executable, "-c", "import sys; raise SystemExit(1)"]
        phone2.write_text(json.dumps(phone_config), encoding="utf-8")
        missed = io.StringIO()
        again = namespace(phone2, confirm=False, now=1_700_000_000)
        again.code = fresh
        again.to = "desk"
        again.pair_command = "join"
        if coura_ux._pair_join(again, missed) == 0 or "Join has no confirmed stored result" not in missed.getvalue():
            raise AssertionError(missed.getvalue())
        if "Join stored" in missed.getvalue() or "Use a new key" in missed.getvalue():
            raise AssertionError(missed.getvalue())
        retried = io.StringIO()
        if coura_ux._pair_join(again, retried) == 0:
            raise AssertionError(retried.getvalue())
        if coura_ux.explain("OUTCOME_UNKNOWN") not in retried.getvalue() or "Use a new key" in retried.getvalue():
            raise AssertionError(retried.getvalue())


def test_doctor_plain_missing_state_and_missing_command():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = root / "coura.json"
        coura_ux.write_setup(config, root / "state", "desk", "computer",
                             ["writer=worker:writer"], False)
        body = json.loads(config.read_text(encoding="utf-8"))
        body["transports"]["worker"] = {
            "type": "command",
            "command": ["coura-missing-bin"],
            "timeout": 5,
            "receipt_status": "STORED",
        }
        config.write_text(json.dumps(body), encoding="utf-8")
        shutil.rmtree(root / "state")
        before = snapshot(root)
        report = coura_ux.doctor_report(config)
        if snapshot(root) != before:
            raise AssertionError("doctor created or changed a file")
        codes = [item["code"] for item in report["problems"]]
        fixes = "\n".join(item["fix"] for item in report["problems"])
        if "STATE_MISSING" not in codes or "COMMAND_NOT_FOUND" not in codes:
            raise AssertionError(codes)
        if "python3 -c" in fixes or "Send a message" not in fixes or "did not run" not in fixes:
            raise AssertionError(fixes)
        receipt = courier.Courier.from_config(config).send(
            "writer", "make-state", "desk", "Create the directory.")
        if not (root / "state").is_dir():
            raise AssertionError("a send did not create the missing state directory: %s" % receipt.status)


def test_status_reports_uncertain_and_unreadable_state():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = root / "coura.json"
        coura_ux.write_setup(config, root / "state", "desk", "computer",
                             ["writer=worker:writer"], False)
        body = json.loads(config.read_text(encoding="utf-8"))
        body["transports"]["worker"] = {
            "type": "command",
            "command": [sys.executable, "-c", "import sys; raise SystemExit(1)"],
            "timeout": 5,
            "receipt_status": "STORED",
        }
        config.write_text(json.dumps(body), encoding="utf-8")
        receipt = courier.Courier.from_config(config).send("writer", "held-1", "desk", "Draft saved.")
        if receipt.status != "OUTCOME_UNKNOWN":
            raise AssertionError("setup send was %s" % receipt.status)
        text = coura_ux.status_text(config)
        if "Uncertain: 1" not in text or "held-1" not in text:
            raise AssertionError(text)
        if coura_ux.explain("OUTCOME_UNKNOWN") not in text:
            raise AssertionError(text)
        peer_dir = root / "state" / "pairing"
        peer_dir.mkdir()
        (peer_dir / "peers.json").write_text("[]\n", encoding="utf-8")
        before = snapshot(root)
        text = coura_ux.status_text(config)
        if snapshot(root) != before:
            raise AssertionError("status changed the directory")
        if "Paired peers: unreadable" not in text or "This node: desk" not in text:
            raise AssertionError(text)
        (root / "state" / "node.json").write_text("[]\n", encoding="utf-8")
        text = coura_ux.status_text(config)
        if "node.json is unreadable" not in text:
            raise AssertionError(text)
        previous_out, previous_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            code = coura_ux.main(["status", "--config", str(config)])
            detail = sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr = previous_out, previous_err
        if code != 0 or "Traceback" in detail:
            raise AssertionError(detail)
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            offered = coura_ux.main(["pair", "offer", "--config", str(config), "--now", "1700000000"])
            offer_detail = sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr = previous_out, previous_err
        if offered == 0 or "unreadable" not in offer_detail or "Traceback" in offer_detail:
            raise AssertionError(offer_detail)


def test_cryptography_note_reports_a_failed_import():
    import builtins
    real_import = builtins.__import__

    def blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "cryptography" or (type(name) is str and name.startswith("cryptography.")):
            raise ImportError("blocked")
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = blocked
    try:
        missing = coura_ux.cryptography_note()
    finally:
        builtins.__import__ = real_import
    if "not installed" not in missing or "Meta-Coura" not in missing:
        raise AssertionError(missing)
    present = coura_ux.cryptography_note()
    if "cryptography package" not in present:
        raise AssertionError(present)


def test_pyz_observer_sync_uses_the_archive():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        archive = build_coura_pyz.build(root / "build" / "coura.pyz")
        clean = root / "clean"
        clean.mkdir()
        target = clean / "coura.pyz"
        target.write_bytes(archive.read_bytes())
        setup = run_pyz(target, [
            "setup", "--non-interactive", "--node", "desk", "--kind", "computer",
            "--recipient", "writer=worker:writer", "--config", "coura.json"], clean)
        if setup.returncode != 0:
            raise AssertionError(setup.stderr)
        config = clean / "coura.json"
        body = json.loads(config.read_text(encoding="utf-8"))
        body["observer"] = {"destination": "writer", "include_body": False}
        config.write_text(json.dumps(body), encoding="utf-8")
        sent = run_pyz(target, [
            "--config", "coura.json", "send", "writer", "--key", "obs-1",
            "--sender", "desk", "--note", "Saved."], clean)
        if sent.returncode != 0:
            raise AssertionError(sent.stdout + sent.stderr)
        receipt = json.loads(sent.stdout)
        observation = receipt.get("observation") or {}
        if receipt.get("status") != "STORED" or observation.get("status") == "CAPTURE_FAILED":
            raise AssertionError(sent.stdout + sent.stderr)
        synced = run_pyz(target, ["--config", "coura.json", "observer-sync", "--batch", "16"], clean)
        if synced.returncode != 0:
            raise AssertionError(synced.stdout + synced.stderr)
        document = json.loads(synced.stdout)
        if "recovered" not in document or "attempted" not in document:
            raise AssertionError(synced.stdout)
        if (clean / "courier.py").exists() or (clean / "observer.py").exists():
            raise AssertionError("the archive unpacked a source file")


def test_status_does_not_load_an_oversized_journal_line():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = root / "coura.json"
        state = root / "state"
        coura_ux.write_setup(config, state, "desk", "computer", ["writer=worker:writer"], False)
        filler = "x" * (courier.MAX_RECORD + 128)
        row = {
            "client": "worker",
            "id": "writer",
            "key": "huge-key",
            "status": "OUTCOME_UNKNOWN",
            "detail": filler,
        }
        line = (json.dumps(row) + "\n").encode("utf-8")
        if len(line) <= courier.MAX_RECORD:
            raise AssertionError("fixture line is not oversized")
        journal = state / "journal.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_bytes(line)
        before = snapshot(root)
        text = coura_ux.status_text(config)
        if snapshot(root) != before:
            raise AssertionError("status changed the directory")
        if "Journal: unreadable" not in text or "huge-key" in text:
            raise AssertionError("status loaded an oversized journal line")
        kept = json.dumps({
            "client": "worker", "id": "writer", "key": "kept-key",
            "status": "OUTCOME_UNKNOWN", "detail": "visible",
        }) + "\n"
        journal.write_bytes(kept.encode("utf-8") + b'{"status":')
        text = coura_ux.status_text(config)
        if "kept-key" not in text or "Journal: unreadable" not in text:
            raise AssertionError("a short torn tail hid the retained row or its coverage warning")



# Each process builds its own candidate archive once; mutated copies use fresh processes.
_DOCTOR_BUILD = None
_DOCTOR_ARCHIVE = None


def _doctor_programs():
    global _DOCTOR_BUILD, _DOCTOR_ARCHIVE
    if _DOCTOR_ARCHIVE is None:
        _DOCTOR_BUILD = tempfile.TemporaryDirectory()
        directory = Path(_DOCTOR_BUILD.name)
        _DOCTOR_ARCHIVE = build_coura_pyz.build(directory / "coura.pyz")
        (directory / "renamed coura.pyz").write_bytes(_DOCTOR_ARCHIVE.read_bytes())
    return [("source", ROOT / "coura_ux.py"), ("archive", _DOCTOR_ARCHIVE),
            ("renamed", _DOCTOR_ARCHIVE.with_name("renamed coura.pyz"))]


def _doctor_cli(program, config, root, settings=None):
    args = ["doctor", "--config", str(config)]
    if settings is not None:
        args += ["--settings", str(settings)]
    if program.suffix == ".pyz":
        return run_pyz(program, args, root, timeout=20)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run([sys.executable, "-B", str(program), *args],
                          cwd=str(root), env=env, capture_output=True, text=True, timeout=20)


def _doctor_config(root, kind, command=None):
    state = root / "state"
    state.mkdir(mode=0o700)
    identity = "00000000-0000-0000-0000-000000000001" if kind == "claude" else (
        "00000000-0000-0000-0000-000000000002" if kind == "codex" else "writer")
    alias = "reviewer" if kind == "codex" else "writer"
    body = {"state_dir": str(state), "recipients": {alias: {"client": kind, "id": identity}}}
    if kind == "worker":
        body["transports"] = {"worker": {"type": "inbox"}}
    if command is not None:
        body["codex_queue_command"] = list(command)
    config = root / "coura.json"
    config.write_text(json.dumps(body), encoding="utf-8")
    return config, state


def _doctor_executable(root):
    # Linux execution fixture: argv[0] itself records ANY invocation, including --version.
    # A later script argument to python would not observe the doctor's version probe.
    executable = root / "fixture-codex"
    sentinel = root / "invoked.txt"
    executable.write_text("#!" + sys.executable + "\nfrom pathlib import Path\n" +
                          "Path(" + repr(str(sentinel)) + ").write_text('invoked', encoding='utf-8')\n" +
                          "print('fixture-codex 1')\n", encoding="utf-8")
    executable.chmod(0o700)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise AssertionError("executable fixture is not executable; native Windows is not covered")
    control = subprocess.run([str(executable), "--version"], capture_output=True,
                             text=True, timeout=5)
    if control.returncode != 0 or not sentinel.exists() or sentinel.read_text() != "invoked":
        raise AssertionError("executable fixture positive control failed: " + control.stderr)
    sentinel.unlink()
    return executable, sentinel


def _doctor_hook(root, config, program, alias="writer", foreign=False):
    launcher = ROOT / "courier.py" if program.suffix != ".pyz" else program
    if foreign:
        launcher = root / "another install" / launcher.name
    # Independent fixture argv: do not ask show_hook_spec, program_args or init for it.
    hook = {"type": "command", "command": sys.executable,
            "args": [str(launcher), "--config", str(config), "show", alias], "timeout": 10}
    return hook


def _doctor_settings(root, hook):
    settings = root / ".claude" / "settings.local.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [hook]}]}}),
                        encoding="utf-8")
    return settings


def _doctor_failure(done, detail):
    if (done.returncode != 1 or "Problem ROUTE_NOT_READY" not in done.stdout
            or detail not in done.stdout or "CONFIG_INVALID" in done.stdout or done.stderr):
        raise AssertionError(done.stdout + done.stderr)


def test_doctor_builtin_codex_no_queue():
    for _kind, program in _doctor_programs():
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, _state = _doctor_config(root, "codex")
            _doctor_failure(_doctor_cli(program, config, root),
                            "Route codex: route reviewer has no codex_queue_command")


def test_doctor_builtin_codex_missing_binary():
    for _kind, program in _doctor_programs():
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable, _sentinel = _doctor_executable(root)
            missing = root / "absent-codex"
            if missing.exists() or not executable.is_file():
                raise AssertionError("missing-binary fixture is not in its declared class")
            config, _state = _doctor_config(root, "codex", [str(missing)])
            _doctor_failure(_doctor_cli(program, config, root),
                            "Route codex: executable not found: " + str(missing))


def test_doctor_builtin_codex_no_execution():
    for _kind, program in _doctor_programs():
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            executable, sentinel = _doctor_executable(root)
            config, _state = _doctor_config(root, "codex", [str(executable)])
            done = _doctor_cli(program, config, root)
            if (done.returncode != 0 or sentinel.exists() or done.stderr
                    or "Route codex (warn): executable found; not invoked; CLI compatibility unmeasured"
                    not in done.stdout):
                raise AssertionError(done.stdout + done.stderr + " command invoked=" + str(sentinel.exists()))


def test_doctor_builtin_claude_conflict():
    for _kind, program in _doctor_programs():
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, _state = _doctor_config(root, "claude")
            # Canonical and currently renamed archive tokens must both be recognised.
            names = list(dict.fromkeys(["courier.py", "coura.pyz", program.name]))
            for name in names:
                if name == "coura_ux.py":
                    continue
                foreign = _doctor_hook(root, config, program, foreign=True)
                foreign["args"][0] = str(root / "another install" / name)
                settings = _doctor_settings(root, foreign)
                _doctor_failure(_doctor_cli(program, config, root, settings),
                                "Route hook:writer: not an exact Coura show hook:")
            malformed = {"type": "command", "command":
                         '"' + str(program if program.suffix == ".pyz" else ROOT / "courier.py") +
                         '" --config "' + str(config) + '" show writer && echo ignored'}
            settings = _doctor_settings(root, malformed)
            _doctor_failure(_doctor_cli(program, config, root, settings), "Route hook:writer:")
            unrelated = _doctor_hook(root, config, program, alias="another-recipient", foreign=True)
            settings = _doctor_settings(root, unrelated)
            done = _doctor_cli(program, config, root, settings)
            if done.returncode != 0 or "Route hook:writer (warn): not installed" not in done.stdout:
                raise AssertionError("unrelated hook was treated as a conflict: " + done.stdout + done.stderr)


def test_doctor_builtin_claude_exact_entrypoints():
    for _kind, program in _doctor_programs():
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, _state = _doctor_config(root, "claude")
            settings = _doctor_settings(root, _doctor_hook(root, config, program))
            for selected in (None, settings):
                done = _doctor_cli(program, config, root, selected)
                if (done.returncode != 0 or "Route hook:writer (pass): exact exec form" not in done.stdout
                        or "Route hook:writer (warn): not installed" in done.stdout or done.stderr):
                    raise AssertionError(done.stdout + done.stderr)
            settings.unlink()
            absent = _doctor_cli(program, config, root, settings)
            if (absent.returncode != 0 or "Route hook:writer (warn): settings file is absent" not in absent.stdout
                    or "exact exec form" in absent.stdout):
                raise AssertionError(absent.stdout + absent.stderr)


def test_doctor_builtin_no_writes():
    for _kind, program in _doctor_programs():
        for missing_state in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                executable, sentinel = _doctor_executable(root)
                config, state = _doctor_config(root, "codex", [str(executable)])
                body = json.loads(config.read_text(encoding="utf-8"))
                body["recipients"]["writer"] = {
                    "client": "claude", "id": "00000000-0000-0000-0000-000000000001"}
                config.write_text(json.dumps(body), encoding="utf-8")
                settings = _doctor_settings(root, _doctor_hook(root, config, program))
                if missing_state:
                    state.rmdir()
                before = snapshot(root)
                done = _doctor_cli(program, config, root, settings)
                if snapshot(root) != before or sentinel.exists():
                    raise AssertionError("builtin doctor changed fixture paths, modes, mtimes or bytes")
                expected = 1 if missing_state else 0
                if (done.returncode != expected or done.stderr or
                        (missing_state and (state.exists() or "Problem STATE_MISSING" not in done.stdout))):
                    raise AssertionError(done.stdout + done.stderr)


def test_doctor_builtin_generic_unchanged():
    for _kind, program in _doctor_programs():
        for command_route in (False, True):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                config, _state = _doctor_config(root, "worker")
                _executable, sentinel = _doctor_executable(root)
                if command_route:
                    body = json.loads(config.read_text(encoding="utf-8"))
                    body["transports"]["worker"] = {"type": "command", "command": [str(_executable)],
                                                     "timeout": 5, "receipt_status": "STORED"}
                    config.write_text(json.dumps(body), encoding="utf-8")
                before = snapshot(root)
                done = _doctor_cli(program, config, root)
                if (done.returncode != 0 or "No problems found." not in done.stdout or done.stderr
                        or "Route codex" in done.stdout or "Route hook:" in done.stdout
                        or sentinel.exists() or snapshot(root) != before):
                    raise AssertionError(done.stdout + done.stderr)


DOCTOR_TESTS = (
    test_doctor_builtin_codex_no_queue,
    test_doctor_builtin_codex_missing_binary,
    test_doctor_builtin_codex_no_execution,
    test_doctor_builtin_claude_conflict,
    test_doctor_builtin_claude_exact_entrypoints,
    test_doctor_builtin_no_writes,
    test_doctor_builtin_generic_unchanged,
)

def main():
    print(BEFORE)
    tests = [
        *DOCTOR_TESTS,
        test_ed25519_matches_the_rfc_vector,
        test_qr_roundtrip_carries_the_code,
        test_pyz_build_is_reproducible,
        test_pyz_clean_dir_first_run,
        test_setup_refuses_to_overwrite,
        test_doctor_makes_no_changes,
        test_pairing_confirms_both_sides_and_hides_the_secret,
        test_pairing_wrong_expired_and_reused_codes,
        test_status_and_autostart,
        test_machine_codes_and_json_stay_unchanged,
        test_second_offer_is_refused_and_the_error_is_readable,
        test_confirm_does_not_trust_a_refused_or_unacknowledged_reply,
        test_doctor_plain_missing_state_and_missing_command,
        test_status_reports_uncertain_and_unreadable_state,
        test_status_does_not_load_an_oversized_journal_line,
        test_cryptography_note_reports_a_failed_import,
        test_pyz_observer_sync_uses_the_archive,
    ]
    if sys.argv[1:] == ['--doctor-only']:
        tests = list(DOCTOR_TESTS)
    if Path(courier.__file__).resolve().parent != ROOT:
        raise RuntimeError('courier import is outside the candidate')
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as error:
            import traceback
            traceback.print_exc()
            print("FAIL %s %s" % (test.__name__, error))
            failed += 1
        else:
            print("PASS %s" % test.__name__)
    print("UNRUN Termux: not run on an Android device.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
