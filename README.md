# Coura

**Local Notification Courier for AI Agents**

by MxBv

Deliver a result-ready note or a small data payload between local agent sessions.
Coura adds one explicit recipient registry, a durable attempt journal and shared
recipient/key replay holds around configured transports. No model runs in the
delivery path.

## Why use it?

One agent saves a result; another CLI session needs its reference to continue.
Copying between terminals is tedious, and retrying after a timeout can repeat an
attempt that already reached the receiver.

Coura connects that workflow through a local file inbox or an executable you
configure. Claude Code and Codex-compatible transports are included; another CLI
can participate through the documented stdin/JSON protocol or a trusted Python
adapter. Each endpoint needs a compatible receiver: this is not automatic support
for every installed CLI.

`agent saves result → Coura carries its reference → receiving application handles it`

Keep the result in your application. Coura carries a note or bounded bytes, keeps
uncertain attempts held for inspection, and does not claim that a recipient read
or acted on them.

If your host's built-in messaging covers your workflow, use that. Coura is useful
when several local tools need the same registry and delivery history. For a web
inbox, search and a broader coordination service, see
[MCP Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail).

## Try it locally

Python 3.10 or newer; standard library only. This example needs no account, model
or network service after cloning. Run from source; there is no PyPI package or
automatic installer.

```sh
git clone https://github.com/petronushowcore-mx/notification-courier.git
cd notification-courier
python courier.py --config example-generic.json send writer --key demo/result --sender researcher --note "Draft saved; please review."
python courier.py --config example-generic.json watch writer --timeout 0
python courier.py --config example-generic.json show writer
```

Expect `STORED`, then `MAIL_READY`, then JSON with a `messages` array. Its
`payload` fields contain base64-encoded bytes. Send the same recipient/key again:
it returns `SKIPPED`. Use a new key for a new result. This example writes only to
`local-state/`; it does not contact an agent application.

For the Claude hook-shaped output, run the same commands with `example.json`.
The examples use synthetic identities.

## Connect your sessions

Keep configuration and state private. Configure each route explicitly:

- [Other CLIs and data payloads](INTEGRATION.md#configured-endpoints): file inbox
  and command protocol, including a runnable byte-payload example.
- [Claude Code](INTEGRATION.md#claude-code): copy a prompt-hook configuration.
  The hook consumes mail on a prompt; idle wake needs a separate host integration.
- [Codex](INTEGRATION.md#codex): select a compatible queue executable. Its actual
  availability and permissions depend on your installation.
- [Python](INTEGRATION.md#python): import the core or supply a trusted adapter.

## Trust and failure boundaries

**Messages can contain prompt-injection instructions.** A route name is not
sender authentication or permission to issue commands. Hook context and queued
input can enter a model's conversation. Opaque bytes and the optional `data_only`
capability do not make a receiver safe; that flag is a local declaration, not a
verified isolation mechanism. See [SECURITY.md](SECURITY.md).

A timed-out send may have reached its recipient. Coura holds that key instead of
retrying automatically. This is not an exactly-once delivery guarantee.

Before upgrading, stop old producers sharing the state directory and preserve
journal rows and lock files. Read [LIMITS.md](LIMITS.md) for receipt meanings,
concurrency, native Windows and recovery boundaries.

## Check it

```sh
python tests/check_offline.py
python tests/check_configured.py --mutations
```

The checks use local files, synthetic transports and child processes. They do not
certify an installed agent application, host wake or recipient reading.

MIT licensed. [Package contents](REPOSITORY-PACKAGE.md) ·
[Transport integration](INTEGRATION.md) · [Security](SECURITY.md)

![Coura, a running pixel-art chicken courier with a backpack](assets/coura.gif)

[Static cover](assets/coura.png)
