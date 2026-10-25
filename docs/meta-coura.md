# Meta-Coura

Meta-Coura is an optional receiver for one local delegation profile. Coura still
delivers between configured endpoints. Meta-Coura adds a receiver-owned
registration, an exact signed request, and one local fixture effect.

The relationship is:

`principal agent -> registered delegate courier -> exact signed request -> named receiver`

This is explicit delegation of one request. It is not identity equality, an
inherited blanket permission, or proof of an entire agent trajectory. A Coura
transport receipt does not prove that the receiver read the request, trusts the
sender, treats it as urgent, holds a permission, or executed anything.

Base Coura does not import this module. It still runs from the Python standard
library alone.

## Install

Python 3.10 or newer. From this repository:

```sh
python -m pip install -r requirements-meta-coura.txt
```

That file names the optional `cryptography` package and does not pin a release.
This repository does not publish a PyPI package. If `cryptography` is missing,
signing, registration, and verification refuse with
`The optional Ed25519 primitive is unavailable`. `python courier.py` and
`import courier` keep working.

Ed25519 is supplied by that package. This module does not implement the
primitive. The algorithm is fixed. Incoming messages cannot negotiate another
algorithm.

## Trust is registered by the receiver

Outside the incoming message path, the receiver operator records:

- principal, delegate, and key id
- the delegate's raw 32-byte Ed25519 public key
- the exact recipient kind and id
- the allowed request kinds and local target identifiers
- the validity interval, policy revision, and active or revoked state

A message cannot register its own key, choose an issuer, or widen a grant.
The sender keeps a separate delegate private key. Do not reuse a Ledger event
key or permit key. Custody, rotation, and recovery stay with the host.

Registration and fixture rows live in the receiver's own SQLite file. Creating
the file sets mode `0600` on POSIX. Missing parent directories are created with
mode `0700`. An existing file that is not already this store is refused and left
unchanged. A symlink is refused. Windows ACLs remain the operator's
responsibility. Anyone who can write the store is inside the trust boundary and
can change rows directly. File mode does not stop that writer.

## Signed carrier

The Coura data carrier is one closed UTF-8 JSON object:

```json
{"body":"<canonical base64>","signature":"<canonical base64>"}
```

The decoded signature is 64 bytes. The signed message is the UTF-8 bytes of
`Coura local delegation`, one zero byte, and the exact body bytes. Verification
uses those received body bytes. It does not reserialize JSON.

Limits, checked before an unbounded read: carrier at most 6144 bytes, body at
most 4096 bytes. `send --body` reads at most 4097 bytes from that file and
refuses a longer file. It does not load the rest of the file. Malformed UTF-8, non-canonical base64, duplicate keys,
non-finite numbers, unknown or missing fields, and wrong types are refused.

The body contains exactly `profile`, `principal`, `delegate`, `key_id`,
`recipient`, `request_id`, `envelope_id`, `issued_at`, `expires_at`,
`policy_revision`, `request_kind`, and `payload`. `priority_request` is the
only optional field.

- `profile` is `coura-local-delegation`.
- `recipient` is `{"kind","id"}` and must be one full Coura address.
- `request_id` and `envelope_id` are lowercase full UUIDs.
- Times are nonnegative integer UTC epoch seconds, not booleans or floats, and
  fit a signed 64-bit integer.
- `issued_at <= receiver_now < expires_at`, and that whole interval fits the
  registration interval.
- Identity and revision strings are nonempty, at most 128 UTF-8 bytes, and
  contain no control, format, or surrogate characters.
- `request_kind` is `note` or `fixture.append`.
- A note payload has only `text`, at most 1024 UTF-8 bytes.
- A fixture payload has only `target` and `text`, with the same text bound.
  `target` must be one identifier the receiver already registered. It is not a
  filesystem path.

The Coura replay key is `meta:` plus `envelope_id`. Coura's attempt id is a
separate unsigned 32-character hex value. The signed recipient must equal the
resolved transport destination.

`request_id` is the logical action. A new envelope or a retry does not grant a
second effect for the same logical request.

## Send and receive

Send only through `Courier.send_data(..., data_only=True)`. The signed carrier
is passed through unchanged. These routes are refused before dispatch:

- Claude and Codex prompt transports
- a configured route without the `data_only` capability
- a file inbox, including one that merely declares `data_only`

`data_only` in Coura configuration is the operator's declaration. It does not
prove that the command verifies anything. Point the command at
`meta_coura.py receive`. A transport that prepends a header is not this profile:
the receiver parses a closed JSON object and refuses anything else.

On the v4 candidate, `send_data` leaves opaque bytes unchanged unless
`bind_lens` armed a lens for that exact call. A default lens applies to `send`,
not to `send_data`. `meta_coura.send` refuses before dispatch when a lens is
armed, because that header would be prepended to the signed carrier.

The command transport writes one JSON object to the receiver's stdin:

```json
{"recipient":{"kind":"worker","id":"writer"},"attempt":"<32 lowercase hex>","payload":"<base64 carrier>"}
```

On success the process writes one transport acknowledgement and exits 0:

```json
{"status":"STORED","recipient":{"kind":"worker","id":"writer"},"attempt":"<the same attempt>"}
```

That acknowledgement is only the command protocol. It is not the policy result.
A direct `Receiver.receive` call records `transport.status` as `UNMEASURED`
because that call does not observe Coura's journal. Read the application result
with `results` or `meta_coura.py result`.

A policy or signature refusal exits nonzero. Coura then reports
`OUTCOME_UNKNOWN` for that `meta:<envelope_id>` key and holds it. That hold is
not a signed policy decision. The application row is written only when the
local effect commits. After a lost acknowledgement, `results` still returns the
committed row, and a later `send_data` of the same envelope is `SKIPPED`.

## What receive does

1. Parse bounded closed data and select a registration that already exists.
2. Verify the signature and keep the checked principal, delegate, key,
   recipient, identifiers, and request for every later step.
3. Check scope against the receiver clock.
4. Immediately before the fixture insert, `BEGIN IMMEDIATE`, re-read the
   registration, and compare the public key, revision, generation, active flag,
   recipient, scope, targets, and validity.
5. In that same transaction, enforce one use of the logical request, append the
   local row, and record the result.
6. The same signed bytes, if they still verify under the current registration,
   return the recorded result and do not append again. Expired or revoked
   requests are refused. Different bytes for the same logical request are
   refused. Reusing one envelope for a different request is refused.
7. A failed transaction rolls back both the request row and the fixture row.

The fixture appends a row in the receiver database. It does not open a
sender-chosen path. Payload text cannot start a shell, evaluate code, call a
model, import a module, select a tool, or change a permission.

Free text stays untrusted data after a valid signature. Instruction-looking
text is stored as a string. Authentication does not remove prompt injection,
and it does not constrain a completely compromised local account.

The result separates:

| Field | Meaning |
|---|---|
| `transport` | `UNMEASURED` from `Receiver.receive`. Coura's own receipt is separate. |
| `identity_check` | `VERIFIED` plus the bound principal, delegate, and key id. |
| `policy_disposition` | `ACCEPTED` plus the bound policy revision. |
| `execution` | `STORED` for a note, or `APPLIED` for a fixture append, plus `effect_id` and `request_id`. |
| `priority_request` | The sender's request, or null when omitted. |
| `priority_assessment` | The receiver's own assessment. Null means not assessed. |

## Priority

When present, `priority_request` has exactly `urgency` and `importance`. Each
value is true, false, or null. These are sender requests. The receiver records
its own urgent or not urgent and important or not important assessment, or
leaves a value unknown. The two objects are independent. Sender priority does
not select a handler, skip a policy check, grant a target, or force a wake.
This revision does not add a ranking engine.

## Python API

```python
import meta_coura
from courier import Courier

private, public = meta_coura.generate_keypair()
receiver = meta_coura.Receiver("receiver.sqlite", "worker:writer", ["board"])
receiver.register(
    "research-lead", "courier-delegate", "delegate-key-1", public,
    ["note", "fixture.append"], ["board"],
    not_before, not_after, "demo-1")
body = {
    "profile": meta_coura.PROFILE,
    "principal": "research-lead",
    "delegate": "courier-delegate",
    "key_id": "delegate-key-1",
    "recipient": {"kind": "worker", "id": "writer"},
    "request_id": request_id,
    "envelope_id": envelope_id,
    "issued_at": issued_at,
    "expires_at": expires_at,
    "policy_revision": "demo-1",
    "request_kind": "note",
    "payload": {"text": "Stored as data."},
}
receipt = meta_coura.send(Courier.from_config("coura.json"), "writer", body, private)
application = receiver.results("research-lead", request_id)
```

`send` returns Coura's transport receipt. `results` returns the recorded
application result, including after a restart. `results` does not authorize the
request again. `receive` does, and it refuses an expired, revoked, or changed
registration even when a row already exists.

`revoke(principal, delegate, key_id)` returns false when no row exists. A
second revoke of an inactive row does not bump the generation again.
`register` on an existing row reactivates it, replaces the grant, and bumps
the generation. A bump that commits before `receive` starts is the registration
`receive` will use. A bump between verification and the fixture insert refuses
that in-flight request.

## Commands

```sh
python meta_coura.py keygen --private delegate.key --public delegate.pub
python meta_coura.py register --store receiver.sqlite --recipient worker:writer \
  --target board --principal research-lead --delegate courier-delegate \
  --key-id delegate-key-1 --public delegate.pub --kind note --kind fixture.append \
  --not-before 1700000000 --not-after 1800000000 --policy-revision demo-1
python meta_coura.py receive --store receiver.sqlite --recipient worker:writer \
  --target board --urgent no --important yes
python meta_coura.py result --store receiver.sqlite --recipient worker:writer \
  --principal research-lead --request-id REQUEST-UUID
python meta_coura.py effects --store receiver.sqlite --recipient worker:writer
python meta_coura.py registrations --store receiver.sqlite --recipient worker:writer
python meta_coura.py revoke --store receiver.sqlite --recipient worker:writer \
  --principal research-lead --delegate courier-delegate --key-id delegate-key-1
python meta_coura.py send --config coura.json --destination writer \
  --private delegate.key --body body.json
python meta_coura.py demo --workdir ./meta-demo
```

`meta_coura.py send` prints the transport receipt. It exits 0 for `STORED`,
`QUEUED`, `THREAD_ACCEPTED`, `SELF`, or `SKIPPED` holding `STORED`, `QUEUED`, or
`THREAD_ACCEPTED`; other returned statuses exit 1. An unknown outcome or its
held repeat can follow a receiver effect. Read the receipt and receiver result;
the exit status does not certify application execution.

If printing a completed receipt fails, the sender exits nonzero and attempts
a `RECEIPT_OUTPUT_FAILED` report on stderr with the original `delivery_receipt`.
The receiver effect and transport journal can already be stored. If stdout and
stderr both fail, no report is guaranteed; the exact nonzero exit is platform
and interpreter dependent.

On the register and receive commands, each `--target` is both a hosted
identifier and, for register, the grant's target. Use `Receiver.register` when
the grant should be a narrower subset of the hosted identifiers. `--urgent` and
`--important` are the receiver operator's assessment (`yes` or `no`). Omit them
to record unknown. They are not read from the message.

`examples/meta-coura-courier.json` is a Coura configuration shape.
`examples/meta-coura-registration.json` is an operator worksheet. Neither file
is loaded by `receive`. Replace the placeholder command with an absolute
`receive` argv before use. `python meta_coura.py demo --workdir DIR` writes a
working configuration, registers one delegate, and runs the note and fixture
path through a real command subprocess.

## Ledger, Spectrum, and OTCS

This profile runs without those projects. The executable acceptance example is
the same-store fixture above. It does not prove a Ledger integration.

A Ledger adapter would have to call that project's declared domain, profile,
intent, evidence, permit, and outcome interfaces after reading a pinned SDK.
This revision does not include one and does not simulate a permit with a
boolean callback. An external target has a check/call race unless that target
enforces the decision atomically. Ambiguous outcomes stay ambiguous. Revocation
here is local to this database and is not a distributed transaction.

Spectrum can describe pinned observations of finite models. It does not inspect
every foreign architecture. OTCS listings are not runtime credentials or an
endorsement. This module does not claim an OTCS or KTP wire profile.

## Observer

A configured v4 observer records a local fact after a `meta:<envelope_id>`
send. That fact is not Meta-Coura authentication. `observer.py` does not import
this module, does not verify the delegation signature, and does not check the
registration. The event keeps `provenance.authentication` as `unverified`,
`provenance.meta_coura` as `absent`, and `provenance.authority` as `none`.

The capture copies a body only when `include_body` is true and the delivered
line is a string, and only for a published transport status within the copy
limit. A meta send passes opaque bytes, so the signed carrier is not copied
into `event["body"]` even when `include_body` is true. A notification string on
the same courier can still be copied when that flag is true. That is the v4
capture rule. It is not a claim that Meta checked the event.

`observer.sync` publishes the event with a separate `send_data` under
`obs-<event_id>` on the observer destination, using the observer delivery
journal. It does not prepend bytes to the signed carrier already given to the
participant. Observer publication isolates the sender mode context so the
separate event remains JSON without a mode header. An armed mode stays available
for the caller's next participant send. The signed carrier is not rewritten.
Previously stored prefixed event bytes are not rewritten or automatically
published again; inspect the retained inbox and outbox for recovery.

## Checks

```sh
python tests/check_offline.py
python tests/check_configured.py --mutations
python tests/check_adopt.py
python tests/check_observer.py
python tests/check_meta_coura.py
```

The Coura checks do not need `cryptography`. `check_meta_coura.py` does. They
use local files and local processes. They do not call a live agent, a paid API,
or an installed hook.

## Named limits

- A valid signature does not make payload text a command or a policy update.
- `transport: UNMEASURED` means `receive` did not observe Coura. A stdout
  `STORED` acknowledgement means only that this command accepted the attempt.
- A nonzero `receive` exit becomes Coura `OUTCOME_UNKNOWN` and holds
  `meta:<envelope_id>`. It is not itself the policy result.
- Exact duplicate bytes return the recorded result only while current
  verification still passes. `results` can still show an older row after revoke
  or expiry; that read is not a new authorization.
- The locked re-read closes the registration race for this database. It does
  not cover an external process that applies an effect outside the transaction.
- The receiver clock is trusted. A wrong clock accepts or refuses the wrong
  interval.
- Generation is compared across the verification and effect steps of one
  `receive` call. It is not a field inside the signature.
- Prompt routes and file-inbox storage are refused by `send`. A custom callable
  with `data_only` is still operator-trusted code.
- An armed v4 lens is refused before `send_data`. With no lens armed, the
  signed carrier is delivered unchanged. Observer publication does not rewrite
  that carrier, and it does not authenticate the event.
- No shell, model, tool, or path is selected from payload text. The host
  account can still do those things on its own.
- Key files are raw 32-byte values, mode `0600` when this module creates them.
  Overwrite is refused. Recovery is not implemented.
