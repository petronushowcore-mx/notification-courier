"""Plain-language commands for installing and pairing Coura.

`send` and the other courier commands keep their JSON. These commands print
sentences. Doctor does not change files. Autostart only prints text.
"""
import argparse
import base64
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

import courier
import coura_pair
import coura_remote


EXPLAIN = {
    "STORED": "The message is stored for the recipient.",
    "QUEUED": "The message was accepted into a queue.",
    "THREAD_ACCEPTED": "The message was accepted for that thread.",
    "SELF": "The recipient is this same endpoint. Coura did not send a message.",
    "SKIPPED": "This key already has a result. Use a new key for a new message.",
    "REFUSED": "Coura refused this attempt. This code alone does not prove that delivery never occurred. Read the detail and journal before retrying.",
    "OUTCOME_UNKNOWN": (
        "Coura did not get a usable acknowledgement. The send may have arrived. "
        "This key is held. Look on the other device before sending again, and do not change the key."
    ),
    "REQUESTED": "A send started and has not finished.",
    "RESERVED": "A send is held in the journal and has not finished.",
    "CAPTURE_FAILED": (
        "Observation could not be confirmed. Read its detail to find the cause; "
        "check the delivery receipt separately."
    ),
    "RECEIPT_OUTPUT_FAILED": "Printing the receipt failed. Check the attached delivery receipt and the journal before retrying. This code does not prove delivery or a saved final receipt.",
    "MAIL_READY": "There is unread mail for that recipient.",
    "QUIET": "There is no unread mail yet.",
}


def explain(code):
    text = EXPLAIN.get(code)
    if text is None:
        return "Code %s has no plain-language note yet." % code
    return text


def _say(out, text):
    courier.print_text(out, text + "\n")


def _node_path(state):
    return Path(state) / "node.json"


def _valid_kind(kind):
    if type(kind) is not str or courier.KIND.fullmatch(kind) is None:
        raise courier.Refused("Kind must be a short lowercase name such as computer or worker")
    return kind


def _valid_id(identity):
    if type(identity) is not str or courier.ENDPOINT_ID.fullmatch(identity) is None:
        raise courier.Refused("Id must be letters, numbers, and . _ - and must not be empty")
    return identity


def _valid_alias(alias):
    if type(alias) is not str or not alias or ":" in alias or courier.ENDPOINT_ID.fullmatch(alias) is None:
        raise courier.Refused("Alias must be one short name without a colon")
    return alias


def _parse_recipient(text):
    if type(text) is not str or text.count("=") != 1 or text.count(":") != 1:
        raise courier.Refused("A recipient looks like writer=worker:writer")
    alias, rest = text.split("=", 1)
    kind, identity = rest.split(":", 1)
    return _valid_alias(alias), _valid_kind(kind), _valid_id(identity)


def write_setup(config, state, node, kind, recipients, replace):
    """Write coura.json and a private state directory. Refuse an existing config."""
    config = Path(config)
    state = Path(state)
    if config.exists() and not replace:
        raise courier.Refused(
            "A config already exists at %s. Coura did not change it. "
            "Choose another --config path, or pass --replace to overwrite it on purpose."
            % config)
    if not state.is_absolute():
        state = config.parent / state
    node = _valid_id(node)
    kind = _valid_kind(kind)
    parsed = []
    for item in recipients:
        parsed.append(_parse_recipient(item))
    if not any(alias == node for alias, _kind, _identity in parsed):
        parsed.insert(0, (node, kind, node))
    kinds = {}
    registry = {}
    for alias, recipient_kind, identity in parsed:
        if alias in registry:
            raise courier.Refused("Recipient alias %s is listed twice" % alias)
        registry[alias] = {"client": recipient_kind, "id": identity}
        kinds[recipient_kind] = {"type": "inbox"}
    try:
        state_dir = state.resolve().relative_to(config.resolve().parent).as_posix()
    except ValueError:
        state_dir = str(state.resolve())
    document = {"state_dir": state_dir, "transports": kinds, "recipients": registry}
    state_path = config.parent / state_dir if not Path(state_dir).is_absolute() else Path(state_dir)
    courier.ensure_private_dir(state_path)
    _node_path(state_path).write_text(json.dumps(
        {"name": node, "kind": kind, "id": node}, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8")
    text = json.dumps(document, ensure_ascii=True, indent=2) + "\n"
    config.write_text(text, encoding="utf-8")
    return document


def setup_interactive(stdin, stdout, config, replace):
    if Path(config).exists() and not replace:
        raise courier.Refused(
            "A config already exists at %s. Coura did not change it." % config)
    _say(stdout, "What should this computer be called?")
    node = stdin.readline()
    if not node:
        raise courier.Refused("Setup needs a node name")
    node = node.strip()
    _say(stdout, "What kind of node is it? [computer]")
    kind = stdin.readline().strip() or "computer"
    recipients = []
    _say(stdout, "Who should it reach? Enter an alias, or an empty line to stop.")
    while True:
        _say(stdout, "Alias:")
        alias = stdin.readline()
        if not alias or not alias.strip():
            break
        alias = alias.strip()
        _say(stdout, "Kind for %s:" % alias)
        recipient_kind = stdin.readline().strip()
        _say(stdout, "Id for %s:" % alias)
        identity = stdin.readline().strip()
        recipients.append("%s=%s:%s" % (alias, recipient_kind, identity))
    return write_setup(config, Path(config).parent / "state", node, kind, recipients, replace)


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _problem(code, problem, fix):
    return {"code": code, "problem": problem, "fix": fix}


def cryptography_note():
    try:
        import cryptography
    except ImportError:
        return ("The cryptography package is not installed. Meta-Coura needs it for Ed25519. "
                "Coura setup, delivery, and pairing do not.")
    version = getattr(cryptography, "__version__", "installed")
    return "The cryptography package is installed (%s). Meta-Coura can use it for Ed25519." % version


def doctor_report(config, *, settings=None, user=False):
    """Read-only checks. This function does not create or chmod anything."""
    problems = []
    notes = []
    if sys.version_info < (3, 10):
        problems.append(_problem(
            "PYTHON_TOO_OLD",
            "This Python is older than 3.10.",
            "Install Python 3.10 or newer and run Coura with that program."))
    config_path = Path(config) if config else None
    if config_path is None or not config_path.exists():
        shown = config if config else "(none)"
        problems.append(_problem(
            "CONFIG_MISSING",
            "There is no config at %s." % shown,
            "Run: python3 coura.pyz setup --config %s" % (shown if config else "coura.json")))
        notes.append(cryptography_note())
        return {"problems": problems, "notes": notes}
    try:
        instance = courier.Courier.from_config(config_path)
    except (courier.Refused, OSError, ValueError, json.JSONDecodeError) as error:
        problems.append(_problem(
            "CONFIG_INVALID",
            "The config could not be read: %s" % courier.failure_detail(error),
            "Fix the JSON, or move this file aside and run setup again."))
        notes.append(cryptography_note())
        return {"problems": problems, "notes": notes}
    state = instance.state
    if not state.exists():
        problems.append(_problem(
            "STATE_MISSING",
            "The state directory %s does not exist yet." % state,
            "Send a message with this config. Coura creates the state directory on that send."))
    elif not state.is_dir():
        problems.append(_problem(
            "STATE_MISSING",
            "The state path %s is not a directory." % state,
            "Point state_dir at a directory."))
    elif os.name != "nt" and _mode(state) & 0o077:
        problems.append(_problem(
            "STATE_NOT_PRIVATE",
            "The state directory is readable by other accounts (mode %04o)." % _mode(state),
            "Run: chmod 700 %s" % state))
    elif not os.access(state, os.W_OK):
        problems.append(_problem(
            "STATE_NOT_WRITABLE",
            "The state directory is not writable.",
            "Give this user write access to %s" % state))
    aliases = {kind: [alias for alias, target in instance.recipients.items()
                      if target.client == kind] for kind in ("claude", "codex")}
    route_checks = []
    if aliases["claude"]:
        context = argparse.Namespace(config=config_path, settings=settings, user=user)
        courier._doctor_claude_hooks(route_checks, context, aliases["claude"])
    if aliases["codex"]:
        courier._doctor_codex(route_checks, instance, aliases["codex"], probe_version=False)
    for check in route_checks:
        if check["status"] == "fail":
            problems.append(_problem(
                "ROUTE_NOT_READY", "Route %s: %s" % (check["name"], check["detail"]),
                "Fix this configured route and run doctor again. Coura did not run it or install hooks."))
        else:
            notes.append("Route %s (%s): %s" % (check["name"], check["status"], check["detail"]))
    for kind, binding in instance.adapters.items():
        transport = binding.transport
        if type(transport) is courier.DataInbox:
            directory = transport.root
            if directory.exists() and not os.access(directory, os.W_OK):
                problems.append(_problem(
                    "TRANSPORT_NOT_WRITABLE",
                    "The inbox for %s is not writable." % kind,
                    "Give this user write access to %s" % directory))
        elif type(transport) is courier.CommandAdapter:
            binary = shutil.which(transport.command[0]) or (
                transport.command[0] if Path(transport.command[0]).is_file() else None)
            if not binary:
                problems.append(_problem(
                    "COMMAND_NOT_FOUND",
                    "The command for %s was not found: %s" % (kind, transport.command[0]),
                    "Install that program, or change the command list in the config. Coura did not run it."))
    if instance.observer is not None and instance.observer.get("error"):
        detail = instance.observer["error"]
        fix = explain("CAPTURE_FAILED")
        problems.append(_problem("CAPTURE_FAILED", detail, fix))
    secret, _public = coura_pair.identity_paths(state)
    if secret.exists() and os.name != "nt" and _mode(secret) & 0o077:
        problems.append(_problem(
            "KEY_NOT_PRIVATE",
            "The pairing secret key is readable by other accounts.",
            "Run: chmod 600 %s" % secret))
    elif not secret.exists():
        notes.append("No pairing key yet. coura pair offer creates one on this device and keeps the secret there.")
    notes.append(cryptography_note())
    return {"problems": problems, "notes": notes}


def emit_doctor(report, out):
    if not report["problems"]:
        _say(out, "No problems found.")
    for item in report["problems"]:
        _say(out, "Problem %s" % item["code"])
        _say(out, item["problem"])
        _say(out, "Fix: %s" % item["fix"])
        _say(out, "")
    for note in report["notes"]:
        _say(out, "Note: %s" % note)
    return 1 if report["problems"] else 0


def _read_rows(path):
    """Return the complete validated prefix and whether coverage is unreadable."""
    if not path.exists():
        return [], False
    rows = []
    limit = courier.MAX_RECORD
    with path.open("rb") as stream:
        while True:
            raw = stream.readline(limit + 1)
            if not raw:
                return rows, False
            if len(raw) > limit or not raw.endswith(b"\n"):
                return rows, True
            if not raw.strip():
                continue
            try:
                value = json.loads(raw.decode("utf-8"), object_pairs_hook=courier.unique_object)
                if (type(value) is not dict or type(value.get("key")) is not str
                        or not value["key"] or len(value["key"]) > 256 or "\x00" in value["key"]
                        or type(value.get("status")) is not str
                        or value["status"] not in courier.TERMINAL | {"REQUESTED", "RESERVED"}):
                    return rows, True
                courier.Address(value.get("client"), value.get("id"))
            except (UnicodeError, ValueError, courier.Refused):
                return rows, True
            rows.append(value)

def _read_object(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if type(value) is not dict:
        return None
    return value


def status_text(config):
    instance = courier.Courier.from_config(config)
    lines = []
    node_file = _node_path(instance.state)
    if node_file.exists():
        node = _read_object(node_file)
        if node is None:
            lines.append("This node: node.json is unreadable.")
        else:
            lines.append("This node: %s (%s:%s)" % (node.get("name"), node.get("kind"), node.get("id")))
    else:
        lines.append("This node: no node.json yet. Run setup.")
    peers = {}
    peers_unreadable = False
    peer_file = coura_pair._peers_path(instance.state)
    if peer_file.exists():
        loaded = _read_object(peer_file)
        if loaded is None or any(type(peer) is not dict for peer in loaded.values()):
            peers_unreadable = True
        else:
            peers = loaded
    if instance.recipients:
        lines.append("Configured recipients:")
        for alias, target in sorted(instance.recipients.items()):
            confirmed = ""
            for peer in peers.values():
                if peer.get("kind") == target.client and peer.get("id") == target.identity and peer.get("confirmed"):
                    confirmed = ", paired"
            lines.append("  %s  %s:%s%s" % (alias, target.client, target.identity, confirmed))
    else:
        lines.append("Configured recipients: none")
    if peers_unreadable:
        lines.append("Paired peers: unreadable")
    elif peers:
        lines.append("Paired peers:")
        for peer in peers.values():
            if peer.get("confirmed"):
                lines.append("  %s:%s" % (peer.get("kind"), peer.get("id")))
    else:
        lines.append("Paired peers: none")
    try:
        journal_rows, journal_unreadable = _read_rows(instance.state / "journal.jsonl")
    except (courier.Refused, OSError):
        journal_rows, journal_unreadable = [], True
    if journal_unreadable:
        lines.append("Journal: unreadable")
    grouped = {}
    for row in journal_rows:
        key = row.get("key")
        if type(key) is str:
            slot = (row.get("client"), row.get("id"), key)
            grouped.setdefault(slot, []).append(row)
    latest = {}
    for slot, rows in grouped.items():
        target = courier.Address(slot[0], slot[1])
        latest[slot] = courier.held_outcome(rows, target, slot[2]) or rows[-1]
    queued = [key for key, row in latest.items() if row.get("status") in {"REQUESTED", "RESERVED"}]
    stuck = [key for key, row in latest.items() if row.get("status") == "OUTCOME_UNKNOWN"]
    lines.append("Queued: unreadable" if journal_unreadable else
                 "Queued: %s" % (len(queued) if queued else "none"))
    if stuck:
        lines.append(("Uncertain: unreadable (known held: %s)" if journal_unreadable
                      else "Uncertain: %s") % len(stuck))
        lines.append(explain("OUTCOME_UNKNOWN"))
        for client, identity, key in stuck:
            lines.append("  %s:%s key %s" % (client, identity, key))
    else:
        lines.append("Uncertain: unreadable" if journal_unreadable else "Uncertain: none")
    if instance.observer is None:
        lines.append("Observer: not configured")
    elif instance.observer.get("error"):
        lines.append("Observer error %s" % "CAPTURE_FAILED")
        lines.append(instance.observer["error"])
        lines.append(explain("CAPTURE_FAILED"))
    else:
        lines.append("Observer: configured for %s" % instance.observer.get("destination"))
    stored = [key for key, row in latest.items() if row.get("status") == "STORED"]
    if instance.observer is not None and instance.observer.get("error") and stored:
        lines.append("Last discrepancies: observer configuration error; %s known STORED receipt(s). Observation history is not checked here." % len(stored))
        lines.append(explain("CAPTURE_FAILED"))
    elif journal_unreadable:
        lines.append("Last discrepancies: unreadable")
    elif stuck:
        lines.append("Last discrepancies: %s key(s) are held as OUTCOME_UNKNOWN." % len(stuck))
    else:
        lines.append("Last discrepancies: none")
    return "\n".join(lines)


def autostart_text(config, program):
    config_text = str(Path(config).resolve()) if config else "coura.json"
    program_text = program or "python3 coura.pyz"
    return "\n".join([
        "Coura does not install this. Copy the lines you want into your own scheduler.",
        "",
        "Computer, cron:",
        "* * * * * %s --config %s observer-sync --batch 16" % (program_text, config_text),
        "",
        "Computer, a user systemd unit you enable yourself:",
        "ExecStart=%s --config %s observer-run --interval 2 --batch 16" % (program_text, config_text),
        "",
        "Termux, a command you start yourself (not measured on a device):",
        "%s --config %s observer-run --interval 2 --batch 16" % (program_text, config_text),
        "",
    ])


def _load_node(state, kind, identity):
    if kind and identity:
        return _valid_kind(kind), _valid_id(identity)
    path = _node_path(state)
    if not path.exists():
        raise courier.Refused("Pass --kind and --id, or run setup so node.json exists")
    node = _read_object(path)
    if node is None:
        raise courier.Refused("node.json is unreadable. Pass --kind and --id.")
    return _valid_kind(node.get("kind")), _valid_id(node.get("id"))


def _pair_offer(args, out):
    instance = courier.Courier.from_config(args.config)
    kind, identity = _load_node(instance.state, args.kind, args.id)
    code = coura_pair.create_offer(instance.state, kind, identity, args.ttl, args.now)
    _say(out, "This device's public key: %s" % coura_pair.ensure_identity(instance.state)[1].hex())
    _say(out, "Pairing code: %s" % code)
    _say(out, "Type this code on the other device. It expires and works once.")
    _say(out, coura_pair.qr_text("coura:" + code))
    return 0


def _pair_join(args, out):
    instance = courier.Courier.from_config(args.config)
    kind, identity = _load_node(instance.state, args.kind, args.id)
    payload = coura_pair.join_payload(instance.state, args.code, kind, identity,
                                      instance.resolve(args.to))
    _say(out, "This device's public key: %s" % coura_pair.ensure_identity(instance.state)[1].hex())
    receipt = instance.send_data(args.to, "pair-join-" + coura_pair._code_hash(args.code)[:16],
                                 payload, data_only=True)
    if receipt.status == "SKIPPED" and receipt.held_status == "STORED":
        _say(out, "Join was already stored. Transport status: SKIPPED")
        _say(out, explain("STORED"))
    elif receipt.status == "SKIPPED" and receipt.held_status:
        _say(out, "Join was not stored again. Transport status: SKIPPED")
        _say(out, explain(receipt.held_status))
        _say(out, "This device has not trusted a peer yet.")
        return 1
    elif receipt.status != "STORED":
        _say(out, "Join has no confirmed stored result. Transport status: %s" % receipt.status)
        _say(out, explain(receipt.status))
        _say(out, "This device has not trusted a peer yet.")
        return 1
    else:
        _say(out, "Join stored. Transport status: STORED")
        _say(out, explain("STORED"))
    if not args.confirm:
        _say(out, "This device has not trusted a peer yet. After the reply arrives, run pair finish --confirm.")
    else:
        _say(out, "Waiting for the other device to confirm. Trust is saved only by pair finish --confirm.")
    return 0


def _schema_payloads(state, schema):
    found = []
    for row in coura_remote.inbox_rows(state):
        try:
            payload = base64.b64decode(row["payload_b64"], validate=True)
            document = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError, KeyError):
            continue
        if type(document) is dict and document.get("schema") == schema:
            found.append(payload)
    return found


def _first_accepted(payloads, accept):
    """Newest payload that accept() allows. Older valid messages stay reachable. When the allowed
    messages carry more than one public key, none is chosen: the person compares one key, and
    another key that arrives before or after that look must not be trusted in its place."""
    error, allowed = None, []
    for payload in reversed(payloads):
        try:
            allowed.append((payload, accept(payload)["public_key"]))
        except coura_pair.PairError as exc:
            if error is None:
                error = exc
    keys = sorted({key for _payload, key in allowed})
    if len(keys) > 1:
        raise coura_pair.PairError(
            "%d different keys answer this pairing: %s. Trust none of them; start the pairing again."
            % (len(keys), ", ".join(keys)))
    if allowed:
        return allowed[0][0]
    if error is not None:
        raise error
    return None


def _relevant_join(instance, payload, now):
    """A join for the open offer from an address this node has a route to. A join that cannot be
    answered is not counted, so it cannot refuse an honest pairing."""
    preview = coura_pair.review_join(instance.state, payload, now)
    coura_pair._matching_alias(instance, {"kind": preview["kind"], "id": preview["id"]})
    return preview


def _pair_confirm(args, out):
    instance = courier.Courier.from_config(args.config)
    payload = _first_accepted(
        _schema_payloads(instance.state, coura_pair.JOIN_SCHEMA),
        lambda item: _relevant_join(instance, item, args.now))
    if payload is None:
        raise courier.Refused("No pairing join has arrived in the local inbox")
    if not args.confirm:
        preview = coura_pair.note_join_preview(instance.state, payload, args.now)
        _say(out, "Join from %s:%s" % (preview["kind"], preview["id"]))
        _say(out, "Public key: %s" % preview["public_key"])
        _say(out, "Not trusted. Run pair confirm --confirm to trust this peer and reply.")
        return 0
    _preview, reply = coura_pair.reply_to_join(instance.state, payload, args.now)
    document = json.loads(reply.decode("utf-8"))
    joined = json.loads(payload.decode("utf-8"))
    alias = coura_pair._matching_alias(instance, joined["address"])
    attempt = coura_pair.accept_attempt(instance.state)
    key = coura_pair.accept_transport_key(document["code"], attempt)
    receipt = instance.send_data(alias, key, reply, data_only=True)
    if receipt.status == "STORED" or (
            receipt.status == "SKIPPED" and receipt.held_status == "STORED"):
        coura_pair.commit_join(instance.state, payload, args.now)
        if receipt.status == "STORED":
            _say(out, "Trusted %s and sent the reply. Transport status: STORED" % alias)
        else:
            _say(out, "Trusted %s. The reply was already stored." % alias)
        return 0
    if receipt.status == "OUTCOME_UNKNOWN" or (
            receipt.status == "SKIPPED" and receipt.held_status == "OUTCOME_UNKNOWN"):
        coura_pair.commit_join(instance.state, payload, args.now)
        _say(out, "Trusted %s on this device because you confirmed." % alias)
        _say(out, explain("OUTCOME_UNKNOWN"))
        _say(out, "Transport status: %s" % receipt.status)
        return 1
    if receipt.status == "REFUSED" or (
            receipt.status == "SKIPPED" and receipt.held_status == "REFUSED"):
        coura_pair.remember_refusal(instance.state, payload, args.now)
        _say(out, "Not trusted by this result. The reply has no confirmed stored result.")
        _say(out, explain("REFUSED"))
        _say(out, "Inspect the receiver and journal before retrying pair confirm --confirm.")
        return 1
    _say(out, "Not trusted. Transport status: %s" % receipt.status)
    _say(out, explain(receipt.status))
    return 1


def _pair_finish(args, out):
    instance = courier.Courier.from_config(args.config)
    payload = _first_accepted(
        _schema_payloads(instance.state, coura_pair.ACCEPT_SCHEMA),
        lambda item: coura_pair.review_accept(instance.state, item))
    if payload is None:
        raise courier.Refused("No pairing reply has arrived in the local inbox")
    if not args.confirm:
        preview = coura_pair.note_accept_preview(instance.state, payload)
        _say(out, "Reply from %s:%s" % (preview["kind"], preview["id"]))
        _say(out, "Public key: %s" % preview["public_key"])
        _say(out, "Not trusted. Run pair finish --confirm to save it.")
        return 0
    preview = coura_pair.confirm_accept(instance.state, payload)
    _say(out, "Trusted %s:%s" % (preview["kind"], preview["id"]))
    return 0


def _cmd_setup(args, out):
    if args.non_interactive:
        if not args.node or not args.kind:
            raise courier.Refused("Non-interactive setup needs --node and --kind")
        write_setup(args.config, args.state or str(Path(args.config).parent / "state"),
                    args.node, args.kind, args.recipient, args.replace)
        _say(out, "Wrote %s" % args.config)
        return 0
    setup_interactive(sys.stdin, out, args.config, args.replace)
    _say(out, "Wrote %s" % args.config)
    return 0


def _cmd_doctor(args, out):
    return emit_doctor(doctor_report(args.config, settings=args.settings, user=args.user), out)


def _cmd_status(args, out):
    _say(out, status_text(args.config))
    return 0


def _cmd_autostart(args, out):
    if not args.print_only:
        raise courier.Refused("Autostart only prints instructions. Pass --print. Coura does not install anything.")
    _say(out, autostart_text(args.config, args.program))
    return 0


def _cmd_explain(args, out):
    _say(out, args.code)
    _say(out, explain(args.code))
    return 0


def _cmd_pair(args, out):
    if args.pair_command == "offer":
        return _pair_offer(args, out)
    if args.pair_command == "join":
        return _pair_join(args, out)
    if args.pair_command == "confirm":
        return _pair_confirm(args, out)
    if args.pair_command == "finish":
        return _pair_finish(args, out)
    raise courier.Refused("Use pair offer, join, confirm, or finish")


USAGE = """\
Coura

  python3 coura.pyz setup
  python3 coura.pyz doctor --config coura.json
  python3 coura.pyz status --config coura.json
  python3 coura.pyz pair offer --config coura.json
  python3 coura.pyz autostart --print --config coura.json
  python3 coura.pyz explain CAPTURE_FAILED

Delivery commands such as send, show, watch, and demo still print JSON.
"""


def _parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="coura.json")
    parser = argparse.ArgumentParser(prog="coura", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", parents=[common])
    setup.add_argument("--non-interactive", action="store_true")
    setup.add_argument("--node")
    setup.add_argument("--kind")
    setup.add_argument("--recipient", action="append", default=[])
    setup.add_argument("--state")
    setup.add_argument("--replace", action="store_true")
    doctor = commands.add_parser("doctor", parents=[common])
    settings = doctor.add_mutually_exclusive_group()
    settings.add_argument("--settings", help="Claude settings JSON to inspect")
    settings.add_argument("--user", action="store_true", help="Inspect ~/.claude/settings.json")
    commands.add_parser("status", parents=[common])
    pair = commands.add_parser("pair")
    pair_commands = pair.add_subparsers(dest="pair_command", required=True)
    offer = pair_commands.add_parser("offer", parents=[common])
    offer.add_argument("--kind")
    offer.add_argument("--id")
    offer.add_argument("--ttl", type=int, default=600)
    offer.add_argument("--now", type=int)
    join = pair_commands.add_parser("join", parents=[common])
    join.add_argument("code")
    join.add_argument("--to", required=True)
    join.add_argument("--kind")
    join.add_argument("--id")
    join.add_argument("--confirm", action="store_true")
    confirm = pair_commands.add_parser("confirm", parents=[common])
    confirm.add_argument("--confirm", action="store_true")
    confirm.add_argument("--now", type=int)
    finish = pair_commands.add_parser("finish", parents=[common])
    finish.add_argument("--confirm", action="store_true")
    start = commands.add_parser("autostart", parents=[common])
    start.add_argument("--print", dest="print_only", action="store_true")
    start.add_argument("--program")
    shown = commands.add_parser("explain", parents=[common])
    shown.add_argument("code")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    if argv[0] not in {"setup", "doctor", "status", "pair", "autostart", "explain"}:
        return courier.main(argv)
    args = _parser().parse_args(argv)
    if args.command == "pair" and getattr(args, "now", None) is None:
        args.now = int(time.time())
    out = sys.stdout
    try:
        if args.command == "setup":
            return _cmd_setup(args, out)
        if args.command == "doctor":
            return _cmd_doctor(args, out)
        if args.command == "status":
            return _cmd_status(args, out)
        if args.command == "autostart":
            return _cmd_autostart(args, out)
        if args.command == "explain":
            return _cmd_explain(args, out)
        if args.command == "pair":
            return _cmd_pair(args, out)
    except coura_pair.PairError as error:
        args = error.args
        if len(args) == 1 and type(args[0]) is str:
            sys.stderr.write(args[0][:300] + "\n")
        else:
            sys.stderr.write("Pairing failed.\n")
        return 1
    except (courier.Refused, OSError, ValueError) as error:
        sys.stderr.write(courier.failure_detail(error) + "\n")
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
