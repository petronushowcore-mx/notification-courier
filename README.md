# Coura

**Local Notification Courier for AI Agents**

by MxBv

Pass a result-ready note from one local agent session to another, and keep a durable
record of the delivery attempt. Coura suppresses repeated sends with the same
recipient/key, including attempts whose outcome is uncertain.

## Why use it?

You run Claude Code and Codex side by side. One session saves a result; another
needs its reference so it can continue. Copying the same message between terminals
is tedious, and retrying after a timeout can send it twice.

Coura provides a small shared delivery layer for that workflow:

- Explicit recipient names mapped to session IDs.
- A local Claude inbox, or a configured Codex-compatible queue command.
- A journal that keeps uncertain attempts held for inspection.
- No language model, database server or MCP service in the delivery path.

`agent saves result → Coura sends its reference → recipient receives a note`

Keep the actual result in your application. Coura carries a short reference and
notice; it does not store the result or prove that the recipient read it.

If your host's built-in messaging already covers your workflow, use that. Coura
adds a shared local registry and delivery journal around supported transports.
For searchable conversations, a web inbox and a broader coordination service,
consider [MCP Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail).
Coura deliberately has a smaller scope.

## Try it locally

Python 3.10 or newer. The example needs no account, model or network service.
Run from source; there is currently no PyPI package or automatic installer.

```sh
git clone https://github.com/petronushowcore-mx/notification-courier.git
cd notification-courier
python courier.py --config example.json send writer --key demo/result --sender researcher --note "Draft saved; please review."
python courier.py --config example.json watch writer --timeout 0
python courier.py --config example.json show writer
```

You should see `STORED`, then `MAIL_READY`, then hook JSON containing the note.
Run the send again with the same key: it returns `SKIPPED`. Use a new key for a
new result. The example uses synthetic identities and writes only to
`local-state/`; it does not contact an agent application.

## Connect your sessions

Replace the example's synthetic IDs with your actual session IDs. Keep the
configuration and state directory private.

- **Claude Code:** [copy a prompt-hook configuration](INTEGRATION.md#claude-code).
  The hook reads letters when you submit a prompt. Idle-session wake needs a
  separate host-managed watcher; the hook alone does not provide it.
- **Codex:** [configure a compatible queue executable](INTEGRATION.md#codex).
  CLI availability and permissions depend on your installation; the local
  example does not validate them.
- **Python:** [use the core or provide a trusted adapter](INTEGRATION.md#python).

## Trust and failure boundaries

**Messages can contain prompt-injection instructions.** Only connect trusted local
producers. A recipient name identifies a route, not authority to issue commands.
Queued messages and hook context enter the receiving agent's conversation;
Coura does not sanitise them into safe instructions. See [SECURITY.md](SECURITY.md).

A timed-out send can have reached its recipient. Coura keeps the key held instead
of retrying automatically. This trades automatic recovery for avoiding a repeated
attempt; it is not an exactly-once delivery guarantee.

Before upgrading, stop old producers sharing the state directory. Preserve journal
rows and lock files. Native Windows operation has additional portability limits.
Read [LIMITS.md](LIMITS.md) for receipt meanings, concurrency and recovery boundaries.

## Check it

```sh
python tests/check_offline.py
```

This runs local file and CLI checks, simulated transports and regression mutations.
It does not certify an installed host, wake mechanism or recipient reading.

MIT licensed. [Package contents](REPOSITORY-PACKAGE.md) ·
[Transport integration](INTEGRATION.md) · [Security](SECURITY.md)

![Coura, a running pixel-art chicken courier with a backpack](assets/coura.gif)

[Static cover](assets/coura.png)
