# Repository package

This package contains a standalone notification/data courier, synthetic local
examples, an MIT licence and local checks.

The reusable mechanism is: resolve a recipient in an explicit registry, compare
canonical client/full identities for self suppression, reserve a delivery in an
append-only journal, dispatch through a selected adapter and preserve its actual
receipt. Legacy Claude inbox writes are followed by a ring; configured inboxes
store opaque bytes without a ring. Consumers show only complete unread records.

## Dependencies

| Component | Dependency |
|---|---|
| Core, local inbox, show/watch, offline check | Python standard library; declared minimum Python 3.10 |
| Configured endpoint | Explicit file inbox or installed stdin/JSON-compatible receiver |
| Optional queue | User-configured installed Codex CLI or compatible executable |
| Optional urgent | User-supplied authenticated host adapter; no native IPC code included |
| Idle chat wake | Host background-task completion and prompt-hook integration |

Native Windows operation has portability limits described in LIMITS.md.

The installed applications, host registration, external command, application permissions
and receiver acknowledgement are outside this package. No application bundle, user home
path, live chat ID, credential, source corpus or private registry is included.

## Local checks

Run `python tests/check_offline.py` from this directory. The script uses temporary state
beneath the package's test directory, restores every changed callable and runs local
synthetic subprocesses and needs no external application command or model. The checks exercise local file handling and simulated transports. They do not
establish real delivery or host acceptance.

`manifest.json` lists the package files and content digests. The manifest excludes
itself to avoid a circular digest.

Run `python tests/check_configured.py --mutations` for configured kinds, bounded
byte payloads, full-address/attempt acknowledgements and their regression cases.
No installed third-party agent application is required.

See [README.md](README.md) for usage and [LIMITS.md](LIMITS.md) for declared ceilings.

Integration examples are in INTEGRATION.md; message and producer trust boundaries are in SECURITY.md. The repository workflow invokes the offline check on Linux, macOS and Windows. A configured job is not a portability guarantee; consult its actual result.
