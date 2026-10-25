# Integrating Coura

Start with the local example in [README.md](README.md). The same configuration is
shared by the sender and consumer. Set `state_dir` to a trusted local directory;
relative paths resolve beside the configuration file. Register each recipient
explicitly. Built-in Claude and Codex IDs are full lowercase UUIDs; configured
kinds use bounded exact local IDs.

## Any CLI

Coura is a local delivery step between CLI agent sessions. Claude and Codex are
built-in routes. Every other CLI is a configured transport. The adapter contract
is:

- Resolve the recipient in the explicit registry. There is no discovery.
- `send` delivers one notification string. On every transport that string is
  `Check KEY from SENDER.` plus the note. A lens, when set, is a separate first
  line and does not change permissions.
- `send-data` delivers the file bytes unchanged, unless a lens is set. A lens
  prefixes one UTF-8 header line and the original bytes follow. The total must
  still fit in 6144 bytes.
- An inbox transport stores that payload for `show` and `watch`. Coura does not
  launch the receiving CLI. A CLI that can read a file can run
  `examples/read-inbox.py` or `show RECIPIENT --text`.
- A command transport runs your argv with `shell=False`, writes one JSON object
  to stdin, and requires the exact stdout echo below. `examples/command-receiver.py`
  is a minimal receiver. Coura does not invoke it during `doctor`; a live
  handshake would deliver mail.
- Built-in `claude` appends a letter and can emit hook JSON. Built-in `codex`
  runs the configured queue command. Neither kind can be overridden by
  `transports`.
- The journal records the attempt, including `sender` and `lens` when present.
  `log` prints that timeline. `doctor` checks each configured route. Claude hook
  and Codex binary checks run only when such a route is configured.

`init --on-stop RECIPIENT` is Claude-specific because it installs a Claude Code
Stop hook. The recipient of that notice can be any configured route.

## Configured endpoints

Register a new kind explicitly, with an exact local ID:

```json
{
  "state_dir": "./local-state",
  "transports": {"worker": {"type": "inbox"}},
  "recipients": {"writer": {"client": "worker", "id": "writer"}}
}
```

This is `example-generic.json`. New kinds use `[a-z][a-z0-9_-]{0,31}`;
IDs use `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`. Address them by alias or exact
`kind:id`. No prefix discovery is provided for these IDs. Built-in `claude`
and `codex` cannot be overridden.

The inbox stores opaque bytes. `show` emits `{"messages": [...]}`, not Claude
hook context; each row contains `ts`, `recipient`, `attempt` and a base64
`payload`. `show --text` prints those payloads as UTF-8 for a person and leaves
the stored JSON unchanged. Bytes that are not UTF-8 are marked and the base64
form is printed beside the marker. No ring file is written for this inbox. A
receiving application must consume and interpret that data itself. `watch`/`show`
are available only for file-inbox routes; a command receiver owns its own
downstream consumption.

Send exact file bytes (1..6144 bytes):

```sh
python3 -c "from pathlib import Path; Path('example-payload.bin').write_bytes(b'result reference')"
python3 courier.py --config example-generic.json send-data writer --key demo/bytes --file example-payload.bin
python3 courier.py --config example-generic.json show writer
```

`send` still carries the ordinary `Check KEY from SENDER. NOTE` text, encoded
as UTF-8 on a configured route. Both entry points share the same recipient/key
hold. Changing the payload, alias or mode does not make a held key reusable.

### Lenses

A lens is an optional mode label on `send` and `send-data`:

```sh
python3 courier.py --config coura.json send writer --key draft --sender researcher --note "Please review." --lens critic
```

Built-ins are `critic` (counter-specialist, look for weak points), `peer`
(colleague on the same task), and `auditor` (independent expert, assessment
only). Config `lenses` overrides or adds names. A name matches
`[a-z][a-z0-9_-]{0,31}`. The text is one line of at most 400 characters.
`lens_defaults` maps `sender->recipient` to a name, using the same strings you
pass to `send`. An explicit `--lens` wins. The receiver sees one header line:

```text
Requested mode from sender (not an instruction from the user): critic. Counter-specialist. Look for weak points. Assessment only.
```

That line is part of the notification text on every transport, including a
command payload. It is not a user or system instruction. `log` shows the lens
name. The journal stores the name and the text that was current at send time.

For a compatible installed receiver, replace the inbox definition:

```json
"worker": {
  "type": "command",
  "command": ["/absolute/path/to/receiver", "receive"],
  "timeout": 30,
  "receipt_status": "STORED",
  "data_only": false
}
```

The executable receives one UTF-8 JSON object on stdin, with exactly these fields:

```json
{"recipient":{"kind":"worker","id":"writer"},"attempt":"00000000000000000000000000000000","payload":"cmVzdWx0IHJlZmVyZW5jZQ=="}
```

The example attempt is synthetic; each real call has a fresh 32-character lowercase
hex attempt. The receiver must exit successfully and return one JSON object on
stdout with exactly `status`, `recipient` and `attempt`. Echo the full recipient
and current attempt unchanged. The status must match the configured
`receipt_status` (`STORED`, `QUEUED` or `THREAD_ACCEPTED`). For example:

```json
{"status":"STORED","recipient":{"kind":"worker","id":"writer"},"attempt":"00000000000000000000000000000000"}
```

Duplicate keys, extra fields/output, a mismatched echo, nonzero exit or timeout
produce a held uncertain outcome. stdout and stderr are each limited to 8192 bytes;
stderr is not parsed as an acknowledgement on this protocol. The default timeout
is 30 seconds, with an upper bound of 120. Fixed argv avoids shell interpolation;
the configured executable is still trusted code.

`data_only: true` is a receiver hint, not a safety check. The same hint can be
written `receiver_hint: true`; if both keys are present they must agree.
`data_only` remains the name accepted by existing configurations and by
`send-data --data-only`. Setting the hint permits that flag. It does not inspect
the host, prove a separate data path, or enforce isolation. The ordinary
file-inbox example does not declare this hint. Never set it merely to make a
check pass. Coura alone does not authenticate messages or make text safe to
place in a model prompt.

## Claude Code

Coura's `show` command emits Claude Code hook JSON. A `UserPromptSubmit` hook can
read the inbox when you submit a prompt. `show --text` prints the letter lines
instead; do not put `--text` on the hook, or Claude will not receive hook JSON.

For a dedicated project with **one Claude session consuming the registered
`writer` inbox**, let `init` merge the hook. The recipient must be a built-in
Claude route. `show` for any other route is not Claude hook JSON, and `init`
refuses it. It updates `.claude/settings.local.json` in the current directory
and keeps every hook whose command is not exactly the one it would write,
including the absolute config path. If that exact command is already present
it does nothing. A different Coura `show` for the same recipient, or a command
that cannot be told apart from one, is a conflict: `init` does not replace or
delete it, exits nonzero, and `--dry-run` prints the existing command instead
of a diff. `--user` writes `~/.claude/settings.json` instead. Pass `--settings`
for any other file. Do not pass `--user` and `--settings` together.

```sh
python3 courier.py --config /absolute/path/to/private/coura.json init --dry-run
python3 courier.py --config /absolute/path/to/private/coura.json init
```

`--on-stop RECIPIENT` also installs a Stop hook. Stop runs when Claude finishes
a response. It is not session termination and it is not task completion.
SessionEnd is the separate session-termination event; `--on-session-end
RECIPIENT` installs that hook and does not infer it from Stop. The show hook
is unchanged: its recipient must still be Claude. The Stop recipient may be
any configured route.

The installed hook is exec form: `command` is the executable and `args` is
the argument vector. Claude Code runs that without a shell (`sh -c` on macOS
and Linux, Git Bash on Windows, or PowerShell when Git Bash is absent, only
when `args` is omitted). Exec form avoids cmd.exe quoting. The hook runs
`host-event`, which reads the hook JSON from stdin. When `session_id` and
`prompt_id` are usable, the delivery key is `host/Stop/<session_id>/<prompt_id>`.
That deduplicates a repeated Stop for that host pair. It does not deduplicate
the whole session: a later prompt has a different `prompt_id`. If those host
tokens are missing, the key is `host/unscoped/<local id>`, `deduplication` is
`unavailable`, and the local id is not a host session or turn id. Re-running
Stop with a random key is not session deduplication. `--unique-key` on `send`
is a local journal key only.

The notice says `response stopped` and `task_completion=unknown`. It does not
say the session finished. `last_assistant_message` is not copied into the
notice or treated as a completion claim. A different Coura `host-event` or
`send` for the same Stop recipient is a conflict and is left unchanged.

After `pipx install` or `uvx`, the same subcommand is `coura --config ... init`.
The command recorded in settings is the interpreter or `coura` executable that
ran `init`, plus the absolute config path, so a hook still works when `python`
is absent and only `python3` exists. Configure `writer` with that session's
actual ID before relying on the hook.

The merged entry has this shape. Preserve any existing hooks if you edit the
file by hand. Replace both absolute example paths:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3",
            "args": [
              "/absolute/path/to/notification-courier/courier.py",
              "--config",
              "/absolute/path/to/private/coura.json",
              "show",
              "writer"
            ],
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Use paths valid on your host. On Windows, JSON paths can use forward slashes,
for example `C:/path/to/courier.py`. `init` writes exec form, so each path is
one argument and the host shell does not parse it. A shell-form command is
still recognised as a conflict. POSIX `sh -c` quoting is available for a
shell-form command you build yourself. PowerShell quoting is a separate
function and is not what `init` writes. cmd.exe is not the documented Claude
hook shell.

This example deliberately fixes one recipient. Do not install it globally across
sessions: every session with that hook would try to consume the same inbox.
Use one consumer per inbox. A host integrating several recipients must bind each
consumer to its actual runtime session rather than blindly copying this example.

The hook consumes up to 64 complete letters and prints them as
`hookSpecificOutput.additionalContext`. It advances its cursor after flushing
stdout. That establishes output, not host admission, model reading or action.
A crash or failure after output can show a letter again.

The hook **does not wake an idle session**. For a host that can react to a completed
background task, start:

```sh
python3 courier.py --config /absolute/path/to/private/coura.json watch writer
```

It returns `MAIL_READY` when complete unread content exists. The host must then
run `show writer` and rearm `watch`. Polling defaults to two seconds. Coura
does not install a background service or promise that your host supports this wake
pattern. See the [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
for hook configuration and context delivery.

`watch --desktop` asks the host for a best-effort notification when mail is
ready: `notify-send` on Linux, `osascript` on macOS, and a PowerShell toast or
balloon fallback on Windows. The message is an argument or an environment value,
never a shell fragment. If the notification fails, `watch` still returns
`MAIL_READY` and the journal is unchanged.

`doctor` prints `pass`, `warn`, or `fail` for the config, the state directory,
and every recipient. Inbox routes must be writable and `watch` must be able to
start. Command routes must resolve an executable; Coura does not run them,
because that would deliver mail. A loose state directory on Unix is a failure.
A missing Codex binary is reported only when a Codex route is configured, and
a Claude hook is checked only when a Claude route is configured. `--json` prints
the same checks. The process exits nonzero when any check fails.

`log` prints one journal row per line: time, from, to, status, key, and lens.
`--to`, `--status`, `--since`, and `--limit` filter that timeline. `--json`
prints the same fields.

## Background watch

`watch` returns as soon as unread mail exists. It does not consume that mail.
Start it again only after `show` has run, or the next `watch` returns
immediately and a restart loop spins. The loop below is the consumer for a
configured inbox. If a Claude `UserPromptSubmit` hook is the consumer, do not
also call `show` from this loop; run one `watch`, let the hook call `show`, then
start `watch` again.

Save the loop as `/absolute/path/to/coura-watch-loop.sh`:

```sh
#!/bin/sh
set -eu
CONFIG=/absolute/path/to/private/coura.json
RECIPIENT=writer
while true; do
  python3 /absolute/path/to/courier.py --config "$CONFIG" watch "$RECIPIENT"
  python3 /absolute/path/to/courier.py --config "$CONFIG" show "$RECIPIENT" --text
done
```

Shell, in the background:

```sh
chmod 700 /absolute/path/to/coura-watch-loop.sh
/bin/sh /absolute/path/to/coura-watch-loop.sh >>"$HOME/coura-watch.log" 2>&1 &
echo $! >"$HOME/coura-watch.pid"
```

systemd user service, `~/.config/systemd/user/coura-watch.service`:

```ini
[Unit]
Description=Coura watch loop

[Service]
Type=simple
ExecStart=/bin/sh /absolute/path/to/coura-watch-loop.sh
Restart=on-failure

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now coura-watch.service
```

launchd agent, `~/Library/LaunchAgents/local.coura.watch.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>local.coura.watch</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>/absolute/path/to/coura-watch-loop.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.coura.watch.plist
```

`KeepAlive` and `Restart=on-failure` start the loop again if the shell exits.
They are not a substitute for calling `show` before the next `watch`. Replace
`python3 .../courier.py` with `coura` when the console script is on `PATH`.

Only file inbox delivery is bundled for Claude; explicit `queue` and `urgent`
modes for a Claude recipient are refused.

## Codex

Add an executable argument list to your configuration:

```json
"codex_queue_command": ["codex"]
```

The command must actually support:

```text
EXECUTABLE queue --thread FULL_ID --message NOTICE
```

Coura requires a successful exit plus this exact reply, with UUIDs for both IDs:

```text
Queued message ITEM_ID for thread FULL_ID.
```

Do not assume that every upstream Codex release has this command or reply.
Check your installed executable and running host before enabling the adapter.
The shipped example and tests use synthetic transports and do not measure a live
Codex installation.

The default runner passes separate arguments with `shell=False`, refuses a
named set of common shells and rejects Windows batch wrappers. A Windows npm
`.cmd` shim is not supported: configure a compatible native executable instead.
A changed reply, warning text, receiver mismatch or timeout leaves an uncertain
outcome held for inspection. Do not automatically resend that key.

A queue acknowledgement is not proof that the receiver read or followed the note.
The host owns wake, permissions and the meaning of queued input.

## Python

The source module can be imported directly:

```python
from courier import Courier

courier = Courier.from_config("courier.json")
receipt = courier.send("writer", "results/draft-1", "researcher",
                       "Draft saved; please review.")
print(receipt.as_dict())
```

Installed with pipx or uv, the import name is still `courier`. The console script
is `coura`. A host may supply `queue` or `urgent` callables to `Courier`. There
is no bundled native IPC implementation or stable native API promise.

A supplied `queue`/`urgent` callable receives `(target, notice, requested)`. It must authenticate
through its own host, verify current runtime authority, call `requested()`
exactly once immediately before dispatch, and return an exact `Receipt` with
plain string fields and the exact receiver ID. The core checks the receipt
contract; it cannot establish that trusted external code respected the ordering.

Before entering supplied code, the core retains a hold. A process exit,
inconsistent receipt or missing acknowledgement does not release it. Read
[LIMITS.md](LIMITS.md) before writing adapters or investigating uncertain attempts.
Never discover another session's private IPC endpoint or borrow its identity.

A new configured binding is an `Adapter(transport, frozenset(capabilities),
receipt_status)` passed in `Courier(..., adapters={"worker": binding})`. Its
callable receives `(target, payload_bytes, requested, attempt)` and returns a
`Receipt` with the exact recipient ID, `recipient_kind` and `attempt_id`.
Use ordinary strings for capabilities (`notification`, `data`, `data_only`).
`data_only` means the operator set the receiver hint. It is not evidence that
the transport isolated the payload. These are trusted local bindings, not a
stable plugin API or a sandbox.

## Addresses and local state

Built-in recipients may be addressed by alias, exact `client:UUID`, or an unambiguous
lowercase UUID prefix of at least eight characters. An optional `claude:` or
`codex:` prefix selects the client. Self suppression in a Python host compares
both the client and full identity supplied by that host.

The built-in Claude inbox uses JSON-lines letters, a numeric offset and an identity sidecar.
Each successful append also touches an empty `<session-id>.ring` file next to
the inbox. That file is a marker for an external host that wants to watch one
path. It does not contain the letter. `show` and `watch` never read it, and
deleting it does not remove mail. Configured data inboxes do not create a ring.
Do not delete companion lock files while a producer or consumer is running.

A nonzero cursor left without its identity sidecar requires explicit operator
recovery. Do not delete cursors to repair an uncertain send: that can show
retained letters again. No automatic migration or history cleanup is provided.

## Optional observer

Chats deliver directly:

```text
Chat A <-> Coura <-> Chat B
             |
             +-> durable workflow events -> Agenda
```

Agenda is an observer. It is not a relay, a required approval hop, a model
call, or an acknowledgement on the ordinary A-to-B path. No observer in the
config means Coura behaves as before. One optional destination is enough.
It must be a configured data transport (file inbox or command), not a Claude
or Codex notification route, because the observer feed is structured data.

```json
"observer": {
  "destination": "agenda",
  "include_body": false,
  "producer_id": "coura-local"
}
```

`include_body` defaults to false. Set it only when you explicitly trust Agenda
with the delivery bytes. The default stores a reference and a SHA-256 of the
attempted bytes. It does not copy transcripts, credentials, private memory, or
arbitrary files. A lens is not task identity and is not authority.

`send` commits the participant journal and returns a separate deferred
`PENDING` observation. It performs no observer module import, file access or
lock acquisition. A misconfigured observer is reported as `CAPTURE_FAILED`
beside the original transport result. Background observer commands recover
committed facts; they do not resend the participant payload. Observer publication
keeps each event as JSON without a sender mode header. A mode armed by a Python
host remains available for its next participant send. Previously stored prefixed
events are not rewritten or automatically republished by an upgrade.

Recover and deliver a bounded batch:

```sh
python3 courier.py --config /absolute/path/to/private/coura.json observer-inspect --limit 32
python3 courier.py --config /absolute/path/to/private/coura.json observer-export --limit 32
python3 courier.py --config /absolute/path/to/private/coura.json observer-sync --batch 16
```

The outbox remains append-only. A derived SQLite index imports at most 32
outbox rows per pass before publication, then recovery reads at most 32 rows
from each source. An incomplete index is reported as `index_complete=false`;
unread source bytes are reported separately by `source_recovery_pending`.
Inspect/export may recover a bounded batch but never send it. Their counts
describe the indexed prefix until catch-up completes. Use observer-run to
continue processing a backlog. Stop older observer workers before using this
layout against an existing state directory; do not delete retained history.

`observer-export` writes JSON to stdout. It does not deliver and is not an
automatic feed. `observer-sync` recovers missing captures from `journal.jsonl`
and `observer/claims.jsonl`, then delivers at most one batch. The delivery key
is `obs-` plus the stable event id. Unknown observer dispatches retain their
keys and do not block other pending events. A confirmed `NOT_STARTED` outcome
may be retried with the same key. Coura establishes no-start only before
adapter entry or through the exact built-in routes' qualified pre-effect
failures. A generic refusal or exception from supplied transport code does
not establish that fact.

This loop is the automatic feed. It is not installed for you. Stop it with
SIGINT or SIGTERM. It backs off while the same unread or unknown records remain.

```sh
python3 courier.py --config /absolute/path/to/private/coura.json observer-run --interval 2 --batch 16
```

`examples/agenda_collector.py` reads the Agenda inbox and prints a projection.
It does not run a model. `verified_task_state` and `verified_read_state` stay
`unknown`. `STORED` and `QUEUED` are transport facts only. Message text is not
parsed into status, and it cannot install hooks or start a program.

Optional logical ids are sender-supplied and unsigned. `--local-task` is a
local association, stored separately. Missing ids stay absent. `report` stores
an explicit unverified disposition. It does not change the transport receipt
and it does not grant authority.

Attribution in base Coura is unverified. `courier.py` and `observer.py` do not
import Meta-Coura and do not authenticate observer events. Meta-Coura is an
optional module. It is not required for direct delivery or for the observer.
A Meta-authenticated producer would need to verify Ed25519 signatures over the
canonical event bytes (`sort_keys` JSON), check a receiver-owned registration,
scope, expiry, revocation, and one-use, and keep effect-boundary checks. It
must not prepend a lens to a signed carrier, must not treat an observation or
a transport receipt as a permit, and must leave ledger admission and
Spectrum/OTCS as separate optional integrations. See `example-observer.json`
and, when that module is present, `docs/meta-coura.md`.
