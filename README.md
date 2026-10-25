# Coura

**Local delivery between CLI agent sessions**

[![Offline checks](https://github.com/petronushowcore-mx/notification-courier/actions/workflows/offline.yml/badge.svg)](https://github.com/petronushowcore-mx/notification-courier/actions/workflows/offline.yml)

by MxBv

Coura carries a note or a small payload from one local CLI agent session to
another. Claude and Codex are built-in routes. Any other CLI uses a configured
transport: a file inbox it can read, or a command that accepts one JSON object
on stdin. No model runs in the delivery path.

Deliver a result-ready note or a small data payload between local agent sessions.
Each endpoint needs a compatible receiver: this is not automatic support for
every installed CLI.

`agent saves result → Coura carries its reference → receiving application handles it`

Keep the result in your application. Coura carries a note or bounded bytes, keeps
uncertain attempts held for inspection, and does not claim that a recipient read
or acted on them. An optional observer can record those transport facts for
Agenda after delivery. Agenda is not on the path from one chat to another.
No observer is required. See [INTEGRATION.md](INTEGRATION.md#optional-observer).

## Try it locally

Python 3.10 or newer; standard library only. From a checkout, `python3` is enough.
No account, model, or network service is required.

```sh
git clone https://github.com/petronushowcore-mx/notification-courier.git
cd notification-courier
python3 courier.py --config example-generic.json send writer --key demo/result --sender researcher --note "Draft saved; please review."
python3 courier.py --config example-generic.json watch writer --timeout 0
python3 courier.py --config example-generic.json show writer --text
```

Expect `STORED`, then `MAIL_READY`, then one readable line:

```text
Check demo/result from researcher. Draft saved; please review.
```

`show` without `--text` still prints machine JSON. `payload` stays base64 in that
view so existing consumers keep the same document. Send the same recipient and
key again and the receipt is `SKIPPED`. Use a new key for a new result. This
example writes only to `local-state/`. It does not contact an agent application.

For the Claude hook-shaped output, run the same commands with `example.json`
and omit `--text`. The examples use synthetic identities.

## What's new / quick tour

`demo` runs one local cycle with two generic recipients and deletes its
temporary state. `doctor` checks the config, the state directory, and each
configured route. `log` prints the journal. `--lens` adds a sender-chosen mode
such as `critic`, `peer`, or `auditor`. The mode is a label in the message, not
a change to the receiver's permissions.

```sh
python3 courier.py demo
python3 courier.py --config example-generic.json doctor
python3 courier.py --config example-generic.json log
python3 courier.py --config example-generic.json send writer --key demo/result --sender researcher --note "Draft saved; please review." --lens critic
```

After `pipx install`, the same commands start with `coura`.

### Connect any CLI

Inbox, for a CLI that can read a file:

```sh
python3 examples/read-inbox.py example-generic.json writer
```

Command, for a CLI that accepts one JSON object on stdin. The receiver in
`examples/command-receiver.py` echoes the acknowledgement Coura requires:

```json
"worker": {
  "type": "command",
  "command": ["python3", "examples/command-receiver.py", "messages.txt"],
  "receipt_status": "STORED"
}
```

Claude and Codex stay available when a recipient's client is `claude` or
`codex`. See [INTEGRATION.md](INTEGRATION.md#any-cli).

## Install

Runtime dependencies: none. This repository is not published to PyPI. From a
checkout:

```sh
pipx install .
coura --help
```

`uvx --from . coura --help` runs the same console script without a permanent
install. `python3 courier.py` remains the no-install path. Use `python3` when
`python` is not on `PATH`.

## Connect your sessions

Keep configuration and state private. Configure each route explicitly:

- [Other CLIs and data payloads](INTEGRATION.md#configured-endpoints): file inbox
  and command protocol, including a runnable byte-payload example.
- [Claude Code](INTEGRATION.md#claude-code): `init` merges a prompt hook.
  The hook consumes mail on a prompt; idle wake needs a separate host integration.
- [Codex](INTEGRATION.md#codex): select a compatible queue executable. Its actual
  availability and permissions depend on your installation.
- [Python](INTEGRATION.md#python): import the core or supply a trusted adapter.
- [Background watch](INTEGRATION.md#background-watch): shell, launchd, and systemd
  examples that run `watch` and then `show`.

## Trust and failure boundaries

**Messages can contain prompt-injection instructions.** A route name is not
sender authentication or permission to issue commands. Hook context and queued
input can enter a model's conversation. Opaque bytes and the `data_only` /
`receiver_hint` flag do not make a receiver safe. That flag is a hint you set
on a command transport. Coura does not check that the receiver kept the bytes
off a model prompt. See [SECURITY.md](SECURITY.md).

A timed-out send may have reached its recipient. Coura holds that key instead of
retrying automatically. This is not an exactly-once delivery guarantee.

Before upgrading, stop old producers sharing the state directory and preserve
journal rows and lock files. Read [LIMITS.md](LIMITS.md) for receipt meanings,
concurrency, native Windows and recovery boundaries.

## Check it

```sh
python3 tests/check_offline.py
python3 tests/check_configured.py --mutations
python3 tests/check_adopt.py
python3 tests/check_observer.py
python3 tests/check_observer_lens.py
python3 tests/break_observer_lens.py --out ../coura-observer-lens-check
python3 tests/check_v5_regressions.py
python3 tests/check_mcp.py
```

The checks use local files, synthetic transports and child processes. They do not
certify an installed agent application, host wake or recipient reading.

## Meta-Coura

Meta-Coura is optional. It verifies a receiver-registered Ed25519 delegate and
applies at most one local fixture row. Install `cryptography` from
`requirements-meta-coura.txt` (no version pin, no package publish) and read
[docs/meta-coura.md](docs/meta-coura.md). Base Coura does not import it.

```sh
python meta_coura.py demo --workdir ./meta-demo
python tests/check_meta_coura.py
```

## MCP

`coura_mcp.py` is an optional stdio adapter for one local Coura configuration.
It is not a broker. Chats still deliver through Coura transports. Registration
examples for Claude Code, Codex, and Cursor are in [docs/mcp.md](docs/mcp.md).
Coura does not import this module.

## Another device

`coura_remote.py` sends a task to a second device through the existing
command transport and returns a separate result. Each device keeps its own
state. The SSH form uses OpenSSH. Setup and the unmeasured Termux path are
in [docs/remote.md](docs/remote.md). Coura does not import this module.

## One file

`python3 build_coura_pyz.py coura.pyz` builds a single archive. Copy that file
to an empty directory and run `python3 coura.pyz setup`. The short path for a
computer, and the unmeasured Termux steps, are in
[docs/quickstart.md](docs/quickstart.md). `doctor` and `status` print
sentences. `send` still prints the same JSON.

MIT licensed. [Package contents](REPOSITORY-PACKAGE.md) ·
[Transport integration](INTEGRATION.md) · [Security](SECURITY.md)

![Coura, a running pixel-art chicken courier with a backpack](assets/coura.gif)

[Static cover](assets/coura.png)
