# Repository package

This package contains a standalone notification/data courier, synthetic local
examples, an MIT licence and local checks.

The reusable mechanism is: resolve a recipient in an explicit registry, compare
canonical client/full identities for self suppression, reserve a delivery in an
append-only journal, dispatch through a selected adapter and preserve its actual
receipt. Built-in Claude inbox writes are followed by an empty `.ring` marker
for external hosts; `show` and `watch` do not read it. Configured inboxes store
opaque bytes without a ring. Consumers show only complete unread records.
`show --text` prints a decoded view and does not change the stored document.

## Dependencies

| Component | Dependency |
|---|---|
| Core, local inbox, show/watch, offline check | Python standard library; declared minimum Python 3.10. `pyproject.toml` installs a `coura` console script. No runtime dependencies. |
| Configured endpoint | Explicit file inbox or installed stdin/JSON-compatible receiver |
| Optional queue | User-configured installed Codex CLI or compatible executable |
| Optional urgent | User-supplied authenticated host adapter; no native IPC code included |
| Idle chat wake | Host background-task completion and prompt-hook integration |
| Optional observer | Local config only. No broker or model. Agenda may be offline. `observer.py` does not import Meta-Coura and does not authenticate events. |
| Optional Meta-Coura | `cryptography` for Ed25519, named in `requirements-meta-coura.txt` with no version pin. Not imported by base Coura or by `observer.py`. Not a published package. |

Native Windows operation has portability limits described in LIMITS.md.

The installed applications, host registration, external command, application permissions
and receiver acknowledgement are outside this package. No application bundle, user home
path, live chat ID, credential, source corpus or private registry is included.

## Local checks

Run `python3 tests/check_offline.py` from this directory. The script uses temporary state
beneath the package's test directory, restores every changed callable and runs local
synthetic subprocesses and needs no external application command or model. The checks exercise local file handling and simulated transports. They do not
establish real delivery or host acceptance.

`manifest.json` lists the package files and content digests. The manifest excludes
itself to avoid a circular digest.

Run `python3 tests/check_configured.py --mutations` for configured kinds, bounded
byte payloads, full-address/attempt acknowledgements and their regression cases.
Run `python3 tests/check_adopt.py` for `init`, `show --text`, and private state
directories. Run `python3 tests/check_observer.py` for direct delivery with an
optional Agenda observer. Run `python3 tests/check_observer_lens.py` for JSON
events consumed by the example collector and preservation of ordinary message
modes. Run `python3 tests/break_observer_lens.py --out ../coura-observer-lens-check`
for isolated source mutations; choose a new output directory outside the checkout.
Run `python3 tests/check_v5_regressions.py` for a
misconfigured observer, one producer id under two racing processes, a receipt
that returns while the outbox lock is held, and bounded recovery.
Run `python3 tests/check_mcp.py` for the optional stdio adapter.
Run `python3 tests/check_remote.py` for two local devices on the command
transport. That run does not measure an SSH server or Termux.
Run `python3 tests/check_ux.py` for setup, doctor, pairing, and the one-file
archive. That run does not measure Termux.
No installed third-party agent application is required. Live registration
inside Claude Code, Codex, or Cursor is not measured.

Run `python3 tests/check_meta_coura.py` after installing the optional
`cryptography` dependency. It checks the local delegation receiver, including
signature, scope, one-use effects, the command transport, and the observer
interaction for a `meta:<envelope_id>` send. It does not call a live agent or a
model. The receiver design is described in
[docs/meta-coura.md](docs/meta-coura.md). Example shapes are
`examples/meta-coura-courier.json` and `examples/meta-coura-registration.json`.
The registration example is an operator worksheet. `receive` does not load it.
Base Coura and `observer.py` do not import this module. Observer events stay
provenance-unverified.

See [README.md](README.md) for usage and [LIMITS.md](LIMITS.md) for declared ceilings.

Integration examples are in INTEGRATION.md; message and producer trust boundaries are in SECURITY.md. The repository workflow invokes the offline check on Linux, macOS and Windows. A configured job is not a portability guarantee; consult its actual result.
