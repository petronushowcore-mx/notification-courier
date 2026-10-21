# Integrating Coura

Start with the local example in [README.md](README.md). The same configuration is
shared by the sender and consumer. Set `state_dir` to a trusted local directory;
relative paths resolve beside the configuration file. Register each recipient
explicitly with a full lowercase UUID.

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

A supplied adapter receives `(target, notice, requested)`. It must authenticate
through its own host, verify current runtime authority, call `requested()`
exactly once immediately before dispatch, and return an exact `Receipt` with
plain string fields and the exact receiver ID. The core checks the receipt
contract; it cannot establish that trusted external code respected the ordering.

Before entering supplied code, the core retains a hold. A process exit,
inconsistent receipt or missing acknowledgement does not release it. Read
[LIMITS.md](LIMITS.md) before writing adapters or investigating uncertain attempts.
Never discover another session's private IPC endpoint or borrow its identity.

## Addresses and local state

Recipients may be addressed by alias, exact `client:UUID`, or an unambiguous
lowercase UUID prefix of at least eight characters. An optional `claude:` or
`codex:` prefix selects the client. Self suppression in a Python host compares
both the client and full identity supplied by that host.

The inbox uses JSON-lines letters, a numeric offset and an identity sidecar.
A `.ring` file is written for compatibility with external host consumers.
The standalone `watch` command reads unread inbox content; it does not read
the ring. Do not delete companion lock files while a producer or consumer is
running.

A pre-existing nonzero cursor without its identity sidecar requires explicit
operator recovery. Do not delete cursors to repair an uncertain send: that can
show retained letters again. No automatic migration or history cleanup is
provided.
