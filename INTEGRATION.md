# Integrating Coura

Start with the local example in [README.md](README.md). The same configuration is
shared by the sender and consumer. Set `state_dir` to a trusted local directory;
relative paths resolve beside the configuration file. Register each recipient
explicitly. Built-in Claude/Codex IDs are full lowercase UUIDs; configured kinds
use bounded exact local IDs.

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
`payload`. No ring file is written for this inbox. A receiving application must
consume and interpret that data itself. `watch`/`show` are available only for
file-inbox routes; a command receiver owns its own downstream consumption.

Send exact file bytes (1..6144 bytes):

```sh
python -c "from pathlib import Path; Path('example-payload.bin').write_bytes(b'result reference')"
python courier.py --config example-generic.json send-data writer --key demo/bytes --file example-payload.bin
python courier.py --config example-generic.json show writer
```

`send` still carries the ordinary `Check KEY from SENDER. NOTE` text, encoded
as UTF-8 on a configured route. Both entry points share the same recipient/key
hold. Changing the payload, alias or mode does not make a held key reusable.

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

`data_only: true` declares that the receiver has a separate data path. It permits
`send-data ... --data-only`; it does not inspect the host or enforce isolation.
The ordinary file-inbox example does not declare this capability. Never set the
flag merely to make a check pass. Coura alone does not authenticate messages or
make text safe to place in a model prompt.

## Claude Code

Coura's `show` command emits Claude Code hook JSON. A `UserPromptSubmit` hook can
read the inbox when you submit a prompt.

For a dedicated project with **one Claude session consuming the registered
`writer` inbox**, merge this into `.claude/settings.local.json`. Preserve any
existing hooks. Replace both absolute example paths and configure `writer`
with that session's actual ID:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"/absolute/path/to/notification-courier/courier.py\" --config \"/absolute/path/to/private/coura.json\" show writer",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Use paths valid on your host and a Python executable available to the hook.
On Windows, JSON paths can use forward slashes, for example `C:/path/to/courier.py`.

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
python courier.py --config /absolute/path/to/private/coura.json watch writer
```

It returns `MAIL_READY` when complete unread content exists. The host must then
run `show writer` and rearm `watch`. Polling defaults to two seconds. Coura
does not install a background service or promise that your host supports this wake
pattern. See the [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
for hook configuration and context delivery.

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

A host may supply `queue` or `urgent` callables to `Courier`. There is no bundled
native IPC implementation or stable native API promise.

A legacy `queue`/`urgent` callable receives `(target, notice, requested)`. It must authenticate
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
These are trusted local bindings, not a stable plugin API or a sandbox.

## Addresses and local state

Built-in recipients may be addressed by alias, exact `client:UUID`, or an unambiguous
lowercase UUID prefix of at least eight characters. An optional `claude:` or
`codex:` prefix selects the client. Self suppression in a Python host compares
both the client and full identity supplied by that host.

The legacy Claude inbox uses JSON-lines letters, a numeric offset and an identity sidecar.
A `.ring` file is written for compatibility with external host consumers.
The standalone `watch` command reads unread inbox content; it does not read
the ring. Do not delete companion lock files while a producer or consumer is
running.

A pre-existing nonzero cursor without its identity sidecar requires explicit
operator recovery. Do not delete cursors to repair an uncertain send: that can
show retained letters again. No automatic migration or history cleanup is
provided.
