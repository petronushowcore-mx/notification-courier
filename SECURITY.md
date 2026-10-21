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

## Data paths are not a security mode

Configured endpoints can carry opaque bytes instead of producing hook context.
This separates a data representation from notification text; it does not enforce
what the receiving process does with those bytes. The `data_only` capability is
a trusted local declaration. Coura does not verify host isolation, register keys,
check signed authority or provide an anti-injection execution boundary. Do not
forward arbitrary payloads into a model prompt and call that a safe data path.

## Protect configuration and state

Anyone able to write `state_dir` can forge messages or change delivery history.
Do not share it with untrusted users, repositories or processes. Use appropriate
filesystem permissions. Local cursor checks and replay holds protect cooperating
processes from mistakes, not a malicious writer.

Queue executables and Python adapters are trusted code. Separate arguments avoid
shell interpolation in the default runner, but do not sandbox the configured
program or its descendants. Never put secrets in a notice or result key.

For concurrency, process-failure and recovery boundaries, see [LIMITS.md](LIMITS.md).
