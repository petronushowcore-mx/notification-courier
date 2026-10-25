# Cross-machine delivery

`coura_remote.py` is an optional adapter over the command transport that is
already in Coura. It does not add a network protocol to Coura. Each device
has its own recipient identity, its own Coura state directory, and its own
remote inbox. A shared directory is not required.

OpenSSH is an external dependency of the SSH form of this adapter. The base
`courier.py` does not import `coura_remote.py`; the optional usability commands do.

`tests/check_remote.py` measures two processes with separate state directories
and the command contract between them. It does not start an SSH server. A
Termux device was not measured.

## What the two devices exchange

The computer sends a task with `Courier.send_data`. The command on that route
runs the phone's `receive` process. `receive` stores the payload bytes, then
writes the acknowledgement Coura already requires:

```json
{"status":"STORED","recipient":{"kind":"phone","id":"handset"},"attempt":"<the same attempt>"}
```

That acknowledgement is a transport receipt. It is not the check result.
A payload whose recipient does not match this device is not stored.

The phone later runs `complete`. That local step runs the one implemented
check, `byte-length`, and sends a separate result message back through the
phone's own Coura configuration. The result carries the task id, the transport
attempt that accepted the task, and an HMAC-SHA256 over the result body. The
return route is the phone's configured recipient. A `from` label inside the
task does not choose it.

## Command route

Put either a direct receive command or `ssh-exec` in the Coura transport.
`ssh-exec` reads the same bounded JSON on stdin and forwards those bytes. In
SSH mode the forwarded command is fixed in the peer file:

```json
{
  "ssh": {
    "program": "ssh",
    "user": "u0",
    "address": "192.0.2.10",
    "port": 8022,
    "identity_file": "/home/me/.ssh/coura_phone",
    "known_hosts": "/home/me/.ssh/coura_known_hosts",
    "remote_command": [
      "python3", "/data/data/com.termux/files/home/coura_remote.py", "receive",
      "--state", "/data/data/com.termux/files/home/coura-phone",
      "--expect-kind", "phone",
      "--expect-id", "handset"
    ]
  }
}
```

The generated argv uses `ssh -T`, `BatchMode=yes`, `StrictHostKeyChecking=yes`,
`IdentitiesOnly=yes`, `LogLevel=ERROR`, an identity file, a known-hosts file,
and `--` before the remote command. The message does not supply the host, the
user, or the command. User and address must be single tokens, so a value such
as `a;rm` is refused before `ssh` is started. An address that contains a colon
is passed as `[address]`, which is the OpenSSH form for an IPv6 destination.

Stdout from that process must be only the acknowledgement. A banner on stdout
does not match the recipient and attempt, so Coura keeps `OUTCOME_UNKNOWN`.
The wrapper uses the base file-backed command runner and refuses stdout or
stderr above 8192 bytes. Captured memory is bounded; temporary-file disk
output and descendant lifetimes are not. Local `shell=False` does not
describe the remote host's command parsing: OpenSSH sends its command
through the configured host environment. Keep every peer-file command and
path under the local operator's control.

## Computer

1. Install Python 3.10 or newer and the OpenSSH client.
2. Create a state directory on this computer only, for example `~/coura-computer`.
3. Create `coura.json` whose `phone` transport is `command`, with `data_only`
   true and `receipt_status` `STORED`. Point `command` at
   `python3 coura_remote.py ssh-exec --peer ./phone-peer.json`.
4. Register the phone recipient, for example kind `phone` and id `handset`.
5. Create an SSH key and a known-hosts file for the phone. Put those paths in
   the peer file. Do not copy the phone's Coura state onto this computer.
6. Write the result key to a file that is not the task. The file contains 64
   lowercase hex characters (32 bytes). `complete` on the phone signs with it.
   `verify-result` on the computer checks with it. This is a shared HMAC key,
   not an Ed25519 signature. Anyone holding that key can produce a valid MAC.
   Verification alone does not establish which device produced it, bind it to
   an expected outstanding task, or apply a receiving policy. The separate
   pairing registry and Meta-Coura registrations are not consulted by this
   demo verifier.

Send a task by calling `send_data` with a payload of at most 6144 bytes:

```json
{"schema":"coura.remote.task.v1","task_id":"task-1","check":"byte-length","subject":"measure me","key":"task-1"}
```

`subject` is data. It is not a shell command.

## Termux

These steps are the intended phone setup. They were not run on a device.

1. Install Python and OpenSSH in Termux: `pkg install python openssh`.
2. Start `sshd`. Termux normally listens on port 8022.
3. Create a state directory on the phone only. Do not mount the computer's
   state directory.
4. Put `coura_remote.py` on the phone and use the `receive` command shown above.
5. Give the computer's public key access to that Termux user.
6. For the result path, give the phone its own `coura.json`. Its command
   transport runs `ssh` back to the computer's `receive` process, with the
   computer's kind and id. The phone also needs the result key file.

The computer's address and the phone's address both come from configuration
on the device that sends.

## Check result

`python3 coura_remote.py complete --state PHONE_STATE --config PHONE_COURA.json --recipient desk --key-id phone-1 --secret-file RESULT.key`

`complete` holds the remote inbox lock only while it records the check. The
lock is released before the result send, so a receive on that same state can
store during the send. It durably records a start before the fixed
`byte-length` check, retains the computed output, then sends a result:

```json
{"schema":"coura.remote.result.v1","task_id":"task-1","check":"byte-length","output":{"byte_length":10},"about_key":"task-1","about_attempt":"<attempt>","signature":{"alg":"hmac-sha256","key_id":"phone-1","mac":"<64 hex>"}}
```

The signature covers the canonical result without the `signature` field.
Verifying it returns the body. It does not run `output`. A changed body or a
changed mac fails verification.

A task whose `check` is not `byte-length` is stored and then marked
`not_allowed`. No other check is implemented. There is no shell and no model
instruction in this path.

## Broken links and repeats

`receive` appends the payload before it writes the acknowledgement. If the
process then exits without that acknowledgement, the phone has the payload
and the computer's journal holds `OUTCOME_UNKNOWN` for that key. A later
`send_data` of the same key is `SKIPPED` and does not start the command.

Opening both configurations again after a restart keeps that hold. A second
delivery under a new Coura key, or a second copy of the same schema and task
id, does not run the check again. The first stored payload stays the one that
was checked. A result uses the same task id and a different schema, so it is
stored beside the task and does not replace it. If `complete` stored the check
and died before the result send, the next `complete` sends the stored output
and does not run the check again. If that send already has a held outcome, the
next `complete` does not send it again. An `OUTCOME_UNKNOWN` result is not
given a new key.

A start without a durable output is reported as `execution_unknown` and is
not automatically computed again. This includes interruption during the
check or while saving its output; the state does not prove whether that
computation finished. This is a retained hold, not an exactly-once execution
guarantee.

Before the first result send, the exact signed result bytes, digest, stable
key and full configured return address are retained in `remote/exec.jsonl`.
Only a confirmed `STORED`, `QUEUED`, or `THREAD_ACCEPTED` receipt, including a
matching held success, is marked `result-sent`. A measured no-start refusal
can retry the same bytes and key after a local configuration repair.
Other uncertain or held sends stay held by the base journal. Reusing a
different secret does not re-sign retained output; changing its key id or
return address is refused.

A task id is unique within one remote state. A repeated task with different
bytes or recipient is kept in `remote/repeats.jsonl` with `conflict: true`,
its full address, attempt, digest and exact payload. The original task is
not replaced or recomputed. `STORED` for that delivery means its bytes are
durably recorded in this audit; it does not mean the new task was admitted.
`repeat_rows` reads that audit with the 16384-byte per-record bound.

## Small messages and large files

Coura refuses a `send_data` payload above 6144 bytes before it starts the
command. A payload of 6144 bytes is stored. The inbox record uses the
16384-byte limit, and each inbox line is read with that cap. A longer line
is refused and is not loaded into memory. Use a task, a short result, or a
file link:

```json
{"schema":"coura.remote.link.v1","task_id":"link-1","name":"report.bin","size":100000,"sha256":"<64 hex>"}
```

`describe_fetch` returns `fetch: not-run` with the size and hash. It does not
open the named file and it does not contact the network. Copying the file is
a separate step that this adapter does not perform.

## Limits of this adapter

A remote refusal and a lost acknowledgement look the same to Coura. The
command acknowledgement can only report `STORED`, `QUEUED`, or
`THREAD_ACCEPTED`. Any other status, a nonzero exit, or a mismatched
recipient or attempt becomes `OUTCOME_UNKNOWN`, and that key stays held.
That is coarse, and it is also why a second transport is not required: Coura
would treat a `REFUSED` acknowledgement as unknown too.

The declared `data_only` flag is still only an operator hint. This receiver
does not turn the payload into a command, but Coura does not inspect an
arbitrary command you configure yourself.

The SSH hop was not measured here. These tests do not start or configure an
SSH server. They forward command bytes to a local `receive` process.
Termux was not measured. Live host keys, network delay, and a phone sleep
policy were not measured.
