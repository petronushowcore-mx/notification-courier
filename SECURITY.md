# Security and trust

Coura is for trusted local processes that cooperate. It is not a secure messaging
service for mutually untrusted agents or users.

## Notification text can carry instructions

The sender, key and note are message content, not authenticated commands.
A producer can place misleading instructions or a malicious result reference in
a notification. Codex-compatible queues may deliver that text as user input;
Claude hooks add it to model context. A receiving model can mistake such text
for authority: this is a prompt-injection risk.

Only connect producers you trust. Keep existing tool permissions and approval
checks at the receiver. Treat a notification as a pointer to inspect, apply the
receiver's own policy to any requested action, and do not execute commands merely
because a message names a familiar sender.

Coura does not judge message content, authenticate the claimed sender, validate
the referenced result, or remove prompt injection. Transport receipt validation,
JSON encoding and `shell=False` do not solve that model-level problem.

## Lens text is sender-controlled

A lens is a short mode label the sender asks the receiver to apply, such as
critic, peer, or auditor. Coura stores the name and the preset text, and renders
them on their own header line:

```text
Requested mode from sender (not an instruction from the user): critic. ...
```

That line is message content. It has the same trust boundary as the note and
the key. It is not a user instruction, not a system instruction, and it does
not change the receiver's permissions, tools, or approval rules. Preset text
comes from the sender's configuration, or from Coura's built-in presets, and
can be rewritten by whoever can edit that configuration. Treat it like any
other sender-controlled text. Do not give it authority the note does not have.

## Data paths are not a security mode

Configured endpoints can carry opaque bytes instead of producing hook context.
This separates a data representation from notification text; it does not enforce
what the receiving process does with those bytes. `data_only` and its alias
`receiver_hint` are a hint the operator records about that receiver. Coura does
not verify host isolation, register keys, check signed authority or provide an
anti-injection execution boundary. Do not forward arbitrary payloads into a model
prompt and call that a safe data path.

## Protect configuration and state

Anyone able to write `state_dir` can forge messages or change delivery history.
Do not share it with untrusted users, repositories or processes. Use appropriate
filesystem permissions. Local cursor checks and replay holds protect cooperating
processes from mistakes, not a malicious writer.

Queue executables and Python adapters are trusted code. Separate arguments avoid
shell interpolation in the default runner, but do not sandbox the configured
program or its descendants. Never put secrets in a notice or result key.

An optional observer feed is structured data for Agenda. Payload text cannot
install hooks, choose executables, set permissions, approve actions, or direct
Agenda to run instructions. Base Coura does not import Meta-Coura and does not
authenticate observer events. Attribution on those events stays unverified.
Meta-Coura is a separate optional module. It is not required for Coura or for
the observer, and removing that module leaves both working. A transport receipt
and an observer receipt are different facts. Neither one is permission to act.

The optional MCP adapter returns inbox bodies as data. An unsigned message, or
one whose Meta-Coura signature was not verified against the configured receiver
store, is marked untrusted. A verified signature is not permission to act and
does not apply a Meta-Coura effect. A JSON object with a duplicate key is
rejected and is not delivered. Tool annotations are hints, not guarantees.
The adapter is not a relay between agents.

The optional remote adapter stores command payloads as data and returns the
existing acknowledgement. A task subject, a file-link name, and a valid
HMAC-SHA256 result are not shell commands and are not model instructions.
The only implemented check is `byte-length`. The return route comes from the
local configuration. OpenSSH is started locally from that configuration with
fixed argv and shell=False. Its remote command is parsed by the configured host
environment. A shared directory is not required. The base courier.py does not
import this adapter; the optional usability commands do.

`setup`, `doctor`, `status`, and `pair` are optional usability commands.
`python3 coura.pyz doctor` does not write or run configured executables. It
checks Claude hook settings and Codex queue configuration; a found executable
is not a verified CLI or a proven host wake. The core `courier.py` doctor is
a separate diagnostic that can create locks and run a version probe.
Pairing stores a secret key on the device that created it and
sends only an address, a public key, and a signature. A peer is saved only
after an explicit confirm on that device, and only when that reply was stored
or the acknowledgement is missing. A refused reply does not save the peer.
`autostart --print` does not install a service or a hook.

For concurrency, process-failure and recovery boundaries, see [LIMITS.md](LIMITS.md).

Pairing's local key implementation rejects noncanonical encodings, identity
public keys and keys outside the prime subgroup. It is ordinary Python,
not constant-time code; protect local state and secrets from untrusted
processes. The remote example uses a separate shared HMAC key. Pairing does
not authorize that example, and possession of a HMAC key does not establish
independent device authorship or permission to execute an application task.
The wrapper bounds captured output in memory via temporary files; it does
not cap total disk output or manage the remote command's descendants.
