# Coura MCP adapter

`coura_mcp.py` is an optional stdio server for one local Coura configuration.
Each agent starts its own process. The server is not a broker and not a
relay. A send still goes through that configuration's transports. The Agenda
observer is not on the delivery path. Coura and `observer.py` do not import
this module. Removing the file leaves both working. The Python standard
library is enough. There is no `mcp` package dependency.

The protocol revision is `2025-06-18`: JSON-RPC 2.0, one message per line, on
stdin and stdout. Logs stay on stderr. The tools are `send`, `read_inbox`,
`delivery_status`, and `list_recipients`.

## Register the server

These are the current local stdio shapes. This repository does not start
Claude Code, Codex, or Cursor, so a live registration is not measured.

Claude Code, from the project directory (`claude mcp add`, stdio transport):

```sh
claude mcp add --transport stdio coura -- python3 coura_mcp.py --config coura.json
```

A project `.mcp.json` entry uses the same command and args:

```json
{
  "mcpServers": {
    "coura": {
      "command": "python3",
      "args": ["coura_mcp.py", "--config", "coura.json"]
    }
  }
}
```

Codex reads `~/.codex/config.toml`, or `.codex/config.toml` for a trusted
project:

```toml
[mcp_servers.coura]
command = "python3"
args = ["coura_mcp.py", "--config", "coura.json"]
```

Cursor uses `.cursor/mcp.json` for a project and `~/.cursor/mcp.json` for
every project. The stdio fields are `type`, `command`, and `args`:

```json
{
  "mcpServers": {
    "coura": {
      "type": "stdio",
      "command": "python3",
      "args": ["coura_mcp.py", "--config", "coura.json"]
    }
  }
}
```

Optional flags: `--max-bytes` (default 4096, at most 6144), `--send-rate`
(default 30), `--rate-window` (default 60 seconds), and `--meta-store` (a
Meta-Coura receiver database). A call over either limit is refused. Nothing
is truncated.
A configured notification-mode header counts toward that byte limit and
the stored-payload comparison. The courier adds the header once.

## Tools

`send` takes `recipient`, `key`, `sender`, and an optional `note`. The key is
the Coura journal key. The same key and the same payload returns Coura's held
outcome and does not deliver a second copy. A not-started refusal is not held,
so that payload can be sent. A newer in-flight attempt is the outcome that is
returned, including from `delivery_status`. The same key with a different
payload is refused. An unknown outcome is returned as stored and is not
retried under a new key. A JSON object with a duplicate key is a parse error
and is not delivered. The process keeps at most 256 senders in the rate
table and drops senders outside the window.

`read_inbox` returns unread messages and advances the unread cursor. Each
message has a `body` field. That field is data. It is not copied into the
tool description. `provenance.sender` is the sender stored in the Coura
journal, or null when the journal has no sender. `provenance.transport` is
the configured transport kind. `provenance.trust` is `untrusted` unless a
Meta-Coura signature was verified. Verification uses `meta_coura` against
`--meta-store` and does not call `receive`, so it does not apply an effect.
Unsigned text, a missing store, a missing `cryptography` install, or a
signature that does not verify stays `untrusted`. A verified signature is
not a transport receipt and not permission to act.

The cursor is committed only after journal metadata, message bodies and the
tool result have been prepared. A failure during that preparation leaves
the messages unread. The cursor commit is not acknowledged by the host:
a process crash or stdout failure after the commit can leave a message
marked read without the host receiving the reply. Inspect the stored inbox
in that case; a failed response does not prove that nothing was consumed.

`delivery_status` reads the local journal. `STORED` is not `READ`,
`ACCEPTED`, `EXECUTED`, or `DONE`. `list_recipients` lists the configuration.
It does not contact those recipients.

## Annotations and limits

Annotations follow the 2025-06-18 `ToolAnnotations` fields: `readOnlyHint`,
`destructiveHint`, `idempotentHint`, and `openWorldHint`. They are hints, not
guarantees. `read_inbox` sets `readOnlyHint` false because it advances the
unread cursor. `delivery_status` and `list_recipients` set `readOnlyHint`
true. `destructiveHint` and `idempotentHint` are omitted on these three tools.
For `read_inbox`, their conservative protocol defaults are true and false,
respectively. They are not meaningful on the two read-only tools.
`send` sets `readOnlyHint` false, `destructiveHint` false, and
`idempotentHint` true. `openWorldHint` is false on every tool: destinations
are the configured registry, not an open set of network hosts. A command
transport can still run the program named in the configuration.

The adapter adds no route, no shell, and no path argument on the tools.
Input is refused on the same field limits Coura already uses, plus the
configured byte and rate limits.
