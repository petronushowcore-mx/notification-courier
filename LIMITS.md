# Limits

This is a small local notification wrapper. Its trust boundary is a configured recipient
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
is refused. Exact Inbox and CodexQueue wrappers reserve through their own callback
before effects.
All producers sharing state_dir must use compatible journal and send-lock protocols, including recognition of RESERVED as a retained hold. Earlier courier versions that do not recognise RESERVED may repeat an attempt held only by that status. Stop every old producer before upgrading; do not delete retained journal rows or lock files as a migration step.

A failed pre-hold blocks supplied-code entry. A process exit or BaseException after that
pre-hold can leave only RESERVED; a later cooperating send remains suppressed. BaseException
may propagate. This is not a power-loss guarantee. Unwritable state can prevent a durable
barrier; deleting the complete retained history can permit replay. Repeated
recipient/key pairs are suppressed after retained requests, successes, unknown outcomes
and refusals that cannot establish no dispatch. Another adapter raising NotStarted cannot release its hold; that record also
cannot release an older unknown attempt. Legacy refused records remain held. SKIPPED names its held status and returns a
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
ring creation can leave an unread letter even though delivery is unknown. The standalone
watcher checks unread content and can still discover it. The legacy consumer's behaviour
depends on its host integration.

Use one local consumer for each inbox. A crash or cursor-write error after output is
flushed can show a letter again. Saving the offset does not prove host admission. The
consumer binds the opened file identity and the first at most 4096 consumed bytes to the
numeric offset. Replacement, a length below the saved offset, or a changed witnessed
prefix is refused. In-place truncate-and-refill with the same identity and witnessed prefix is not
detectable; this is not a guarantee against every rewrite. A crash between identity and
offset writes causes an explicit mismatch refusal, requiring operator recovery. A legacy
nonzero offset without identity also requires operator recovery. The standalone
show/watch share one cursor lock; unrelated external readers do not participate.

Records are capped at 8192 UTF-8 bytes. Each show operation emits at most 64 letters;
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
files request mode 0600. Windows ACLs and existing-file permissions are the operator's
responsibility. Symlink, reparse-point, concurrent file replacement and
hostile-state-directory protection are not claimed.

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

The queue parser concatenates stdout followed by stderr, strips surrounding whitespace
and accepts one exact response shape. Extra stderr warnings also prevent acknowledgement.
Extra non-whitespace output, changed CLI wording, a mismatched receiver or missing
item ID produces OUTCOME_UNKNOWN. The offline checks run the wrapper CLI and synthetic local processes; they do not measure an installed
application CLI, application wake, IPC, network delay or receiver reading.

## Host integrations

Claude queue/urgent modes, native urgent transport, recipient supersession or
withdrawal, unattended host restart and automatic hook installation are not implemented.
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
