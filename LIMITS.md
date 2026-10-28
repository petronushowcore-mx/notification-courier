# Limits

This is a small local notification and bounded-data courier. Its trust boundary is a configured recipient
registry, a trusted state directory and explicitly supplied transport code. It has no
content judge, account manager, source-result store or deployment service.

## Local state

Anyone who can write state_dir is inside the trust boundary. Such a writer can append
or rewrite inbox letters, append or delete journal rows (an appended REFUSED/NOT_STARTED
row for a matching attempt releases a hold), rewrite unread bytes after the witnessed
prefix, truncate unread letters at the saved offset, and delete both cursor files so
that retained letters are shown again. These changes need not be detected. Cursor
binding, tail checks and the journal protect cooperating processes from mistakes,
not from that writer.

The journal retains requests and outcomes. A failed final journal append is reported as
OUTCOME_UNKNOWN after apparent success; a previously stored RESERVED or REQUESTED continues to
prevent replay. An explicit known refusal remains a refusal even if its final record
could not be written. Once a supplied adapter is entered, its Refused exception does not
establish that dispatch did not start, even if requested() was not called. Its recorded
refusal remains held. Exception diagnostics are copied only from a single exact string
argument of a plain local exception; supplied exception formatting is not invoked. A
transport receipt must be the exact Receipt type with plain string fields (or None for
optional fields). The courier snapshots it and validates the complete final record before
accepting it. Invalid or oversized receipts become a small held OUTCOME_UNKNOWN.
This also holds an honest pre-dispatch refusal from supplied code;
inspect it rather than automatically retrying. Built-in preflight refusals may retry
after configuration repair. Only the exact default CodexQueue executor can establish
NOT_STARTED from an OSError child-creation failure. The queue captures the callable it
invokes and turns a custom runner's NotStarted into an ordinary held refusal, even if
that runner restores the configured executor attribute before raising. Return containers must
be plain tuples or lists containing an exact int and two exact bytes values; deferred
methods on supplied return objects are not called. The courier also
requires the exact queue type and an OSError cause. This is not a sandbox against untrusted in-process code, for example code
that replaces or patches the courier module, its classes, globals or builtins, installs
interpreter tracing, profiling,
monitoring or signal hooks, injects asynchronous exceptions, or interferes with execution
frames. Such in-process code can forge NOT_STARTED after dispatch; use only trusted adapters.
Adapter ordering is still trusted, not observed. The courier retains RESERVED before
entering supplied callable code or built-in subclasses, without counting this as a callback.
A successful callback writes REQUESTED, separately from RESERVED; a repeated call
is refused. Exact built-in Inbox and CodexQueue wrappers reserve through their own callback
before effects.
All producers sharing state_dir must use compatible journal and send-lock protocols, including recognition of RESERVED as a retained hold. Earlier courier versions that do not recognise RESERVED may repeat an attempt held only by that status. Stop every old producer before upgrading; do not delete retained journal rows or lock files as a migration step.

A failed pre-hold blocks supplied-code entry. A process exit or BaseException after that
pre-hold can leave only RESERVED; a later cooperating send remains suppressed. BaseException
may propagate. This is not a power-loss guarantee. Unwritable state can prevent a durable
barrier; deleting the complete retained history can permit replay. Repeated
recipient/key pairs are suppressed after retained requests, successes, unknown outcomes
and refusals that cannot establish no dispatch. Another adapter raising NotStarted cannot release its hold; that record also
cannot release an older unknown attempt. Refusals recorded without an attempt id remain held. SKIPPED names its held status and returns a
nonzero CLI exit for held requests, refusals and unknown outcomes. There is no force flag or automatic
fallback to a different route. Keep the history intact when investigating uncertainty.

Individual appends are protected by companion file locks across cooperating local
processes. An additional permanent OS-held lock covers the hold scan through the complete
send attempt and final recording for one canonical client/recipient/key. Its filename is
SHA-256 of an unambiguous JSON tuple; sender, alias and delivery mode do not split the lock.
Different keys use separate locks. The lock is separate from each journal append/read lock;
it is released on process exit and its file is never deleted. This serializes cooperating
same-key producers, without an exactly-once claim or a transaction joining journal and
transport. A contended lock waits at most 30 seconds and then raises Refused. This is a liveness
limit, not a failure of the replay barrier: a waiting sender does not dispatch. It can
expire while a holder runs a queue command (30 seconds from JSON or up to 120 seconds
from Python, plus up to two seconds after kill), or unbounded supplied adapter code.
Recursive same-key adapter calls open a second handle, wait for that deadline and
raise Refused; if propagated, the outer attempt records REFUSED/POSSIBLE and stays
held. Inverted nested sends on different keys can pause each other until the same
deadline. These nested usages are unsupported. Each new canonical recipient/key
leaves one permanent send lock file, so the directory grows with the key population.
All participating processes must use the same permanent lock file.
A crash after reservation
can leave RESERVED or REQUESTED with no later receipt. A crash after the local append and before
ring creation can leave an unread letter even though delivery is unknown. The ring is only
an empty marker file for an external host; `show` and `watch` do not read it. The standalone
watcher checks unread content and can still discover it. A Claude hook consumer's behaviour
depends on its host integration.

Use one local consumer for each inbox. A crash or cursor-write error after output is
flushed can show a letter again. Saving the offset does not prove host admission. The
consumer binds the opened file identity and the first at most 4096 consumed bytes to the
numeric offset. Replacement, a length below the saved offset, or a changed witnessed
prefix is refused. In-place truncate-and-refill with the same identity and witnessed prefix is not
detectable; this is not a guarantee against every rewrite. A crash between identity and
offset writes causes an explicit mismatch refusal, requiring operator recovery. A
nonzero offset without identity also requires operator recovery. The standalone
show/watch share one cursor lock; unrelated external readers do not participate.

Claude inbox and journal records are capped at 8192 UTF-8 bytes. Configured data
inbox records and command stdin are capped at 16384 bytes; the decoded payload
is limited to 1..6144 bytes. Each show operation emits at most 64 letters;
remaining complete letters wait for the next show operation. Key, sender and notice
fields have character limits of 256, 100 and 1024. Key and sender must be nonempty;
NUL characters in notification fields, queue argument lists and direct queue notices
are refused before reservation. Malformed or partial journal records
require inspection. Inbox readers wait for incomplete trailing records. Writers refuse
to append after an incomplete tail, so a later letter is not falsely acknowledged behind a fragment.
Inspection and explicit operator repair are required; fragments are not discarded.
The local inbox reserves its key before checking that tail. A refusal at that point
keeps the reservation, so repairing the fragment does not release the refused key;
a repeated send remains SKIPPED with held_status REFUSED. Inspect the retained history
rather than assuming that a refused, reserved attempt can be replayed.
The journal is scanned line by line; time spent scanning grows with retained history.

These files are intended for a trusted local directory. On Unix, newly created append
files request mode 0600, and newly created state directories request mode 0700.
Directories that already exist are not changed. `mkdir` is followed by `chmod`
because the process umask would otherwise widen the mode. Windows ACLs and
existing-file permissions are the operator's responsibility. Symlink, reparse-point,
concurrent file replacement and hostile-state-directory protection are not claimed.

## Command delivery

The default queue runner refuses ambiguous Windows basenames with trailing dots or
spaces and checks common executable names (cmd, powershell, pwsh, bash and sh, with or
without .exe) and Windows .cmd/.bat extensions. Renamed executables, other interpreters
and their behaviour are not classified. Other configured programs are trusted
operator-supplied code; `shell=False` is not a sandbox or proof of their safety.

The queue timeout is 30 seconds through JSON configuration. A Python host constructing
`CodexQueue` may pass a `timeout` of up to 120 seconds. The
default runner then allows up to two more seconds to wait after killing a timed-out
child. It captures output in temporary files and limits the response it reads. It does not
manage descendant processes. A timed-out command has an unknown outcome and must not be replayed
automatically. Temporary files use Python's configured temporary directory; set TMPDIR
or TEMP appropriately for your host.

Configured command adapters send bounded JSON on stdin and require the exact full
kind/id and attempt in a closed JSON acknowledgement. The configured status must
match. A generic command does not need a Codex queue item ID. stdout and stderr
are each bounded at 8192 bytes; only stdout is parsed for this protocol. A
configured timeout defaults to 30 seconds and must be positive and at most 120.
The declared `data_only` / `receiver_hint` value is an operator hint, not evidence
of isolation: receiver code and host context routing remain operator
responsibilities. Coura does not inspect the receiving process. Opaque byte storage
does not authenticate a sender or remove prompt injection. Generic file inboxes
emit data JSON and do not create ring files or hook context. `show --text` decodes
UTF-8 for a person; the default `show` document is unchanged.

Text for a person (`show --text`, `init`, `doctor`, `log`, `demo` and the guided
commands) is written in the encoding of the output stream. A line that encoding
cannot represent, as on a console or pipe that is not UTF-8, is written escaped
instead of failing, and so is a line that itself begins with the reserved marker
`[escaped] `. An escaped line is that marker followed by the line with its
backslashes doubled and each missing character as a Python escape (`\xNN`,
`\uNNNN`, `\UNNNNNNNN`). Every other line is written unchanged. The marker alone
tells the two apart: a line without it is exactly the original, and a line with it
reads back exactly by undoing the escapes. Nothing is dropped or replaced by a
look-alike. A stream without an encoding, such as an in-memory one, receives the
text unchanged. A pairing QR code drawn with block characters is readable only on
a UTF-8 console. `show --text` advances the cursor only after the write and flush
succeed; a write or flush error still fails the command, and the next `show` reads
the same letters again.

The built-in Codex queue parser concatenates stdout followed by stderr, strips surrounding whitespace
and accepts one exact response shape. Extra stderr warnings also prevent acknowledgement.
Extra non-whitespace output, changed CLI wording, a mismatched receiver or missing
item ID produces OUTCOME_UNKNOWN. The offline checks run the wrapper CLI and synthetic local processes; they do not measure an installed
application CLI, application wake, IPC, network delay or receiver reading.

## Host integrations

Claude queue/urgent modes, native urgent transport, recipient supersession or
withdrawal and unattended host restart are not implemented.
Hook installation is opt-in: an explicitly invoked `init` command can merge Claude Code
hooks into the selected settings file; `--dry-run` previews changes without writing.
Coura does not install a background service or provide idle wake by itself.
The native urgent extension is experimental and supplied by the user. It must preserve
application authority checks and obtain actual executor context. No private application
implementation is included.

A watcher can notice unread mail; only a host capable of consuming a completed
background task can turn that notice into an idle chat wake. The wrapper does not
promise a host will do so.

Native Windows operation, including concurrent appends, is not guaranteed.

## Read the receipt literally

| Status | What it establishes |
|---|---|
| STORED | A local inbox append completed (with an empty ring marker for the Claude inbox), or a configured adapter acknowledged storage for this address/attempt. |
| RESERVED | A durable hold was written before supplied-code entry; the callback may not have run. |
| REQUESTED | The transport callback recorded its reservation; it does not prove dispatch. |
| QUEUED | A Codex-compatible command acknowledged an item, or a configured adapter acknowledged queuing for the exact address/attempt. |
| THREAD_ACCEPTED | A supplied adapter reported acceptance for the exact receiver (and attempt for configured routes). |
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

## Observer

The observer is optional. With no `observer` object, no observer files are
required and delivery is unchanged. A misconfigured observer does not refuse
the participant delivery. The receipt keeps the transport result, and
`observation.status` is `CAPTURE_FAILED` with the configuration error.
`observer-sync` and the other observer commands still refuse that
configuration.

There is no distributed transaction between the participant and the observer.
Ordinary send commits its participant receipt and reports a deferred PENDING
observation. It does not import the observer module or access observer files,
locks or indexes. A background command recovers the fact from that journal;
the participant message is not sent again. With include_body enabled, a
bounded plaintext note may be retained in the final journal row for later
capture; opaque data carriers are never copied.

The append-only outbox is the observer audit history. A derived SQLite index
uses a 512 KiB page cache and disk temporary storage. Each pass imports at most
32 outbox records and 32 records from each participant/claim source; unresolved
attempt candidates are retained on disk, with at most 32 processed at source
EOF. Publication waits while the outbox index is catching up. Counts and
inspection refer to the indexed prefix; index_complete=false explicitly
means incomplete coverage. source_recovery_pending separately reports unread
source bytes or unprocessed source candidates. Inspection may recover facts but never delivers them.
truncated also reports results beyond the requested limit.

Unknown, held and oversized events remain retained; they do not pin the
selection of later pending events. Only confirmed not-started events are
eligible again, with the same key. Pending selection advances around the
indexed queue. The history, delivery journal and lock files are not deleted.
Stop every older observer writer before switching to the indexed layout;
do not mix observer implementations against the same state directory.

A partial final source line stays on disk. Publication waits until that
participant tail is complete. Corrupt logs or an unavailable/corrupt index
are observer errors, never evidence of an empty healthy queue. Recovery is
bounded in application working memory, not in accumulated disk space or
total I/O. SQLite work and replay checks can take longer with larger history.
Explicit all-history helpers such as known_ids and fold are diagnostics and
do not have the automatic worker's working-set guarantee.

Agenda being offline, slow, full or refusing does not roll back a participant
receipt. A background capture/index failure is reported by the observer
command; it does not retroactively change that receipt. Configuration errors
remain `observation.status = CAPTURE_FAILED` beside the transport result.
Observer publication uses `observer/delivery.jsonl` and does not capture itself,
so an observation does not create another observation. Publication carries a JSON
event without a sender mode header and preserves the caller's armed mode for its
next participant send. Previously stored prefixed event bytes are not rewritten
or automatically published again; inspect retained inbox and outbox records.

`STORED` and `QUEUED` do not mean `READ`, `ACCEPTED`, `EXECUTED`, or `DONE`.
A Stop hook is `response_stopped`. It does not mark a session or a task
complete. SessionEnd is a separate host event. A sender's finished wording is
not a verified claim. `report` stores an unverified disposition. The
projection keeps conflicting facts and leaves verified task, read, acceptance,
and execution state `unknown`. It does not parse prose, lens names, or Stop
text into identity or completion.

Event ids come from the producer id, source log, byte offset, attempt, and
status. The offset is a local append position, not a global clock. Host
deduplication for Stop exists only when the hook JSON supplies a usable
`session_id` and `prompt_id`. A generated id is labeled
`local_key_identifies_host_event: false` and is not a host session or turn.
Missing host fields stay unknown. The transcript path and the assistant
message text are not copied; a SHA-256 of the path may be stored when the
path was supplied.

Unknown observer deliveries stay unknown. They are not retried under a new
key. `NOT_STARTED` is the only automatic same-key retry, and base Coura sets
that flag only for the default Codex queue child-creation failure. Command
observer failures that Coura records as `REFUSED`/`POSSIBLE` are retained.
`observer-export` is not an automatic feed. `observer-run` is a foreground
loop with backoff. It is not installed into the user environment. Backlog
lives on disk. Each pass delivers at most 64 events. The loop sleeps; it does
not busy-wait on the same unread records.

Base Coura does not import Meta-Coura and does not authenticate observer
events. Meta-Coura is an optional module that can be shipped beside Coura.
Removing it does not change Coura or the observer. Observation does not
authorise a sender, a completion claim, or a transport receipt.
Full body copy requires `include_body: true` and still only copies the
delivery bytes already being sent, and only when the record fits. Claude hook
installation uses exec form. This environment does not execute Git Bash,
PowerShell, or cmd.exe unless those programs are present; a missing host
shell is not a pass.

## MCP adapter

`coura_mcp.py` speaks MCP protocol 2025-06-18 on stdio for one local
configuration. It is not a broker, and the observer is not on the delivery
path. Defaults are a 4096-byte message limit and 30 sends per 60 seconds for
each sender in that process. Over-limit calls are refused and are not
truncated. The byte limit includes the complete delivered notification,
including a configured mode header. The rate limit is process-local because each agent runs its own
server. Expired senders are dropped from that table, and at most 256 senders
are retained; a further new sender is refused and is not recorded. A
caller-supplied key is the Coura journal key. The same key and payload
returns Coura's held outcome and does not deliver a second copy. A
not-started refusal is not a held outcome, so the same payload can be sent.
A newer in-flight attempt is the outcome that is returned. A different
payload is refused, and an unknown outcome is not retried under a new key.
Duplicate JSON keys are rejected. `read_inbox` advances the unread cursor
and advertises `readOnlyHint` false. The cursor is committed after reply
preparation. A process or
stdout failure after that commit can still hide an unread message from the
next call; see docs/mcp.md for this boundary. `openWorldHint` is false because recipients come from the
configured registry. A command transport can still reach whatever program the
operator configured. Live registration inside Claude Code, Codex, or Cursor
is not measured by `tests/check_mcp.py`.

`coura_remote.py` uses the command transport for a second device. The payload
limit stays 1..6144 bytes. A payload of that size is stored; the inbox line
uses the 16384-byte record limit and is read one bounded line at a time. A
longer line is refused and is not loaded. The command acknowledgement still
has to echo the recipient and attempt. A lost acknowledgement after a local store, a
nonzero exit, and a remote refusal all become held `OUTCOME_UNKNOWN`; Coura
has no acknowledgement status for a clean remote refusal. The same key is not
sent again. A repeated schema and task id does not run `byte-length` again. A
result is not a repeat of the task with that id. `complete` releases the inbox
lock before the result send. A file link carries a size and a sha256 only.
Nothing in this adapter fetches the file. An SSH address that contains a colon
is passed as `[address]`. `tests/check_remote.py` measures two local state
directories and one same-state result send. It does not measure an SSH server,
and it does not measure Termux.

`coura.pyz` is a stored zip of the Python modules. `setup` writes a new
config and refuses to replace an existing one unless `--replace` is passed.
The command-first form `python3 coura.pyz doctor --config coura.json` only
reads. Built-in Claude hook and Codex queue checks share the core route
decisions, but this form does not start a watch or run an executable. Finding
a queue executable does not prove CLI compatibility or host wake. Putting the
global option first, `python3 coura.pyz --config coura.json doctor`, selects
the core diagnostic, just like `courier.py`; it can create lock files and
probe a version. `status` does not load a journal line over 8192 bytes;
that journal is reported unreadable. `pair` exchanges an address and a public
key after both sides pass `--confirm`; the secret key stays in
`identity/secret.key`. A
pairing code expires and works once. A second offer does not replace an open
code. Confirm records the peer when the reply is stored, and also when the
acknowledgement is missing, because that key is held. A refused reply does
not record the peer. A reply names the SHA-256 of the exact join it answers.
`pair finish` trusts a reply only if it answers the join this device sent, to
the device named by `pair join --to`, and only once: after `pair finish
--confirm` every further reply is refused and cannot change the trusted key. A
join entered before this check existed must be entered again. The reply's
signature proves possession of the key it carries, not that the key belongs to
the device you meant: the join carries the code in clear, so a party that reads
the join in transit can still answer it first. `pair offer` and `pair join`
print this device's public key; compare it with the key the other device shows
before passing `--confirm`. `pair confirm --confirm` and `pair finish --confirm`
trust only the key that their preview without `--confirm` showed, and none at
all when two different keys answer the same pairing: then start the pairing
again. A Coura from before this check neither sends nor checks the join digest:
this version refuses its reply, and such a device does not check this version's
reply, so update both devices before pairing. Pairing failures print their own
sentence. `autostart
--print` only prints scheduler text. `tests/check_ux.py` runs `observer-sync`
from the archive on this computer. It does not measure Termux.

### Remote execution and pairing boundaries

The remote adapter implements only the fixed, pure byte-length example.
It records a durable start before evaluating it. An interrupted start with
no retained output becomes execution_unknown; it is not automatically
computed again. Exact signed result bytes and their return address are
retained before delivery. result-sent means a confirmed transport success,
not merely that an attempt was made. A no-start refusal may retry the same
key after configuration repair; an uncertain result does not get a new key.
Conflicting task bytes are preserved in the repeat audit without replacing
the first task. STORED remains a storage receipt, not application admission.
See docs/remote.md for the separate journal states.

Pairing trust slots use the complete kind and id. On a subsequent confirmed
write, legacy id-only rows are normalized from the full address inside each
row. A row overwritten by an older implementation cannot be reconstructed.
Stop older pairing writers before upgrading. State writes use atomic
replacement under the relevant local locks; a filesystem without these
semantics is outside this claim. The small Python Ed25519 implementation
checks canonical point encodings and public-key subgroup membership, but is
not a constant-time implementation or a general device-security audit.
Pairing is separate from remote HMAC verification and from Meta-Coura's
receiver-owned registrations.

Status groups transport records by the full recipient and key. Invalid,
partial or oversized journal rows make the journal, counts and discrepancy
summary unreadable; unreadable is not an empty healthy state. Complete validated
rows before the first invalid record remain visible as known retained holds, but
those rows cannot establish complete counts. The unreadable suffix is not skipped.
Observer
source_recovery_pending also remains true while unresolved source candidates
wait to be emitted, even after the source byte cursor reaches EOF.
