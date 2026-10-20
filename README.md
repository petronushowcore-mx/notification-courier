# Local notification courier

Send a short notification when a result is ready. The courier resolves a named
recipient, selects a transport and records what that transport actually reports. It does
not ask a language model to interpret or approve the message.

The intended flow is:

`result stored by your application -> explicit event -> recipient -> delivery -> receipt`

Store the result in your own application first. Then call this courier with its
reference key and a short notice. This package sends the notice; it neither stores the
source result nor verifies that the reference exists.

## What works here

- A local file inbox with legacy JSON-lines, ring and numeric offset files, plus a bound identity sidecar.
- A command that prints those letters as Claude hook context.
- A watcher that returns when a complete unread letter exists.
- An optional Codex queue command with an exact receiver and queue-item check.
- A Python extension point for authorised queue and urgent transports.

The declared minimum is Python 3.10. Native Windows operation has additional
portability limits; see LIMITS.md. No packages, account, network service or model
are needed for the example or offline check.

## Quick example

Run these commands from the package directory. The identities in the example are
synthetic, and no external command is configured.

```sh
python courier.py --config example.json send writer --key example/result --sender demo --note "Result is ready."
python courier.py --config example.json watch writer --timeout 0
python courier.py --config example.json show writer
python tests/check_offline.py
```

The first command returns `STORED`. The watcher prints `MAIL_READY`. The show command
prints hook JSON and advances the local offset after flushing its output. Sending the
same recipient/key after a retained reservation or outcome returns `SKIPPED` with
`held_status`. Its exit code is zero only for a retained successful delivery; held
requests, refusals and unknown outcomes return one. Use a new key for a new result;
inspect an uncertain earlier outcome rather than automatically retrying it.

Cooperating senders using one state directory serialize the same canonical client,
recipient ID and key under a permanent OS-held lock. Different keys use separate locks.
A contender raises `Refused` after the 30-second lock deadline. This liveness limit can
expire during a queue command (30 seconds from JSON, up to 120 from Python, plus up to
two seconds after killing it), or an unbounded supplied adapter. Recursive same-key
sends wait for that deadline and refuse; the outer attempt stays held. Inverted nested
sends on different keys can pause each other until refusal. Permanent lock files
accumulate, one per canonical recipient/key; see LIMITS.md.

Replace the example identities with complete lowercase UUIDs and set `state_dir` to a
directory trusted by both local parties. Relative state paths are resolved beside the
configuration file. Recipients are explicitly registered; registration alone does not
establish that a chat is running. State paths are bound to an absolute path when the
courier is created, even if a Python host later changes directory. An alias, an exact
`client:UUID` address (for example `claude:00000000-0000-0000-0000-000000000001`),
or a unique lowercase UUID prefix of at least eight characters resolves within
that registry. An optional `claude:` or `codex:` prefix selects the client. A prefix matching more than one registered address is
refused. Self suppression compares both the client and full ID, using a caller address
supplied by the host Python integration.

## Connect a Claude consumer

Configure the host's supported prompt hook to execute the `show` command for its own
configured recipient. An idle host also needs to run `watch` as a background task,
consume its completion notice and then execute `show`. This package supplies these
commands but does not install or register host hooks.

The producer appends a letter under a local file lock and then creates its ring. Do not
delete companion lock files while any process is using the state. The standalone watcher
polls unread content every two seconds by default; event-driven sending does not mean
the idle consumer is free of polling. Rearm the watcher after showing the mail. Use one
consumer per inbox. The standalone consumer and another hook must not concurrently
manage the same offset.

All producers sharing state_dir must use compatible journal and send-lock protocols,
including recognition of RESERVED as a retained hold. Stop every old producer before
upgrading; keep retained journal rows and lock files. See the version-compatibility
boundary in [LIMITS.md](LIMITS.md).

Only file inbox delivery is implemented for Claude. Numeric offsets remain available for
host layout compatibility. The standalone consumer also requires its identity sidecar. A
pre-existing nonzero offset without that sidecar requires explicit operator recovery;
while that cursor metadata is retained, it is never silently rebound to the current file.
Deleting both cursor files starts from zero and can show retained letters again. Anyone
who can write `state_dir` is inside the trust boundary; LIMITS.md lists that writer's powers. External consumers do not participate in
these locks and must not share the offset concurrently. Explicit `queue` and `urgent`
requests to a Claude recipient are refused.

## Connect a Codex queue

Add an argument list to your configuration, for example:

```json
"codex_queue_command": ["codex"]
```

This name must resolve to an executable supported by the default runner. If a Windows
npm installation resolves it to a `.cmd` shim, configure the absolute path to its
native `codex.exe` instead. This does not establish that its queue API is available.

The adapter adds `queue --thread FULL_ID --message NOTICE` as separate arguments. The
default runner uses `shell=False`, rejects a small named set of common shell executables
and rejects Windows batch wrappers. This is a filename check, not a sandbox or a
universal interpreter classifier. It requires a successful command plus the exact
response `Queued message ITEM_ID for thread FULL_ID.` before returning `QUEUED`.
The parsed response is stdout followed by stderr, with surrounding whitespace stripped;
an extra warning on stderr therefore prevents an exact acknowledgement.
Availability and permissions depend on your installed CLI and running host. The shipped
check exercises the courier CLI in local subprocesses and uses a fake queue runner; it
does not certify an installed application CLI version. On Windows, batch wrappers need a
separately supplied authorised adapter; the default command runner refuses .cmd and .bat
executables. The operating system may execute a batch file through a shell despite
`shell=False`; see the [Python subprocess security
considerations](https://docs.python.org/3/library/subprocess.html#security-considerations).
A configured Python adapter must be a script that implements the queue command contract.
The example runner in the check is simulated and launches no process.

For in-limit output that decodes as valid UTF-8, a changed command reply produces
OUTCOME_UNKNOWN with a bounded output excerpt and exit code for inspection.
That excerpt is not promised for oversized output or decoding errors. Do not automatically retry the held key.
A Python integration can pass a `queue` callable to `Courier` with the same
`(target, notice, requested)` receipt contract as the urgent adapter below; validate
that adapter against the actual installed CLI before using it. No alternative parser
or automatic fallback is bundled.

## Optional urgent integration

There is no bundled native Codex IPC implementation or stable public API promise. A
Python host may pass an `urgent` callable when constructing `Courier`. It receives
`(target, notice, requested)` and returns an exact `Receipt` with plain string fields
(or `None` for optional IDs and held status). Subclasses and values that cannot fit the
8192-byte final journal record produce a retained `OUTCOME_UNKNOWN`.

The host owns authentication and application permissions. Its adapter must obtain
context from its actual executor, verify its own current active turn, and call
`requested()` exactly once immediately before dispatch. A successful return must include
the exact recipient ID. Format-valid IDs and a user-provided configuration are not proof
of native context or permission. Preserve a real application refusal. Do not discover
another chat's pipe or borrow its identity.

Before entering supplied code, the core durably records `RESERVED`. Exact built-in
Inbox and CodexQueue wrappers reserve through their callback before effects; their
subclasses count as supplied code. The first successful callback records `REQUESTED`;
the adapter must call it exactly once. A supplied attempt can therefore have one
RESERVED and one REQUESTED row. An early retained hold is not a callback invocation.
The core rejects an apparent success
without the callback, matching recipient echo or a status compatible with the selected
transport. Such an inconsistent return is OUTCOME_UNKNOWN. This checks the receipt
contract; it cannot prove that an arbitrary external adapter followed the required
ordering. A supplied adapter that exits or raises SystemExit before its callback can
leave RESERVED held; inspect that uncertain attempt rather than replay it. A Refused exception from an entered supplied adapter remains held even if
that adapter omitted the callback; it is not evidence of no dispatch. See LIMITS.md
for the remaining process-failure boundary. For an unheld key, urgent delivery without a supplied adapter returns
`REFUSED`. A retained key remains `SKIPPED` under the replay barrier.

## Read the receipt literally

| Status | What it establishes |
|---|---|
| STORED | Local letter and ring writes completed. |
| RESERVED | A durable hold was written before supplied-code entry; the callback may not have run. |
| REQUESTED | The transport callback recorded its reservation; it does not prove dispatch. |
| QUEUED | The configured command acknowledged an item for the exact receiver. |
| THREAD_ACCEPTED | A supplied native adapter reported the exact receiver's acceptance. |
| REFUSED | Dispatch did not start, or the adapter explicitly reported a refusal; a failed child creation can follow reservation. |
| OUTCOME_UNKNOWN | Dispatch may have happened; a usable receipt is missing. |
| SKIPPED | This recipient/key has a retained outcome, named by held_status. |
| SELF | The resolved client and full identity equal the supplied caller. |

The CLI reports `RECEIPT_OUTPUT_FAILED` if writing or flushing a completed send receipt
fails; its `delivery_receipt` retains the transport outcome and the exit code is nonzero.
An output error does not cancel delivery or permit replay. Check the retained journal.

No transport result is `READ`. Printed hook context is not proof that an assistant read
the underlying result or acted on it. Obtain reading evidence from the recipient through
your application's own acknowledgement channel.

See [LIMITS.md](LIMITS.md) for failure and concurrency limits, and
[REPOSITORY-PACKAGE.md](REPOSITORY-PACKAGE.md) for the package boundary. The wrapper is
licensed under MIT.
