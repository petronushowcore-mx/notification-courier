# Coura quickstart

This is the short path for one computer. A phone section is below. That phone
section was not run on a device.

You need Python 3.10 or newer. You do not need to clone the repository or
install a package. One file, `coura.pyz`, is enough.

## Build the one file

From a Coura checkout:

```sh
python3 build_coura_pyz.py coura.pyz
```

Copy `coura.pyz` to the computer that will use it. The checkout can stay behind.

## First run on a computer

```sh
python3 coura.pyz setup --non-interactive --node desk --kind computer --recipient writer=worker:writer --config coura.json
python3 coura.pyz doctor --config coura.json
python3 coura.pyz status --config coura.json
```

`setup` asks for a node name and recipients when you leave off `--non-interactive`.
It writes `coura.json` and a private `state` directory. If `coura.json` is
already there, setup stops and leaves it unchanged. Pass `--replace` only when
you mean to overwrite it.

`doctor` reads the configuration and prints problems in sentences. It checks
Claude hooks for configured Claude recipients and the queue command for Codex
recipients, as well as configured generic transports. A missing Codex queue or
executable is a problem. A found executable is not run, so its CLI compatibility
remains unmeasured.

For Claude it reads `.claude/settings.local.json` in the current directory.
Pass `--settings path/to/settings.json` to inspect another file, or `--user` to
inspect your user settings. An absent hook is a warning; a conflicting hook is
a problem. Doctor does not install hooks or change files. This read-only mode
is `python3 coura.pyz doctor`; the core `courier.py` diagnostic can create lock
files and run a version probe.

`status` is one summary: who this node is, who is configured or paired,
anything queued, anything uncertain, and observer trouble.

Send a note:

```sh
python3 coura.pyz --config coura.json send writer --key hello-1 --sender desk --note "Draft saved."
python3 coura.pyz --config coura.json show writer --text
```

`send` still prints JSON for scripts. The status names in that JSON are
unchanged. `python3 coura.pyz explain OUTCOME_UNKNOWN` prints the sentence for
a code.

## Pair a second device

Pairing sends addresses and public keys through the cross-machine adapter. It
does not send a secret key. Both people confirm before either device trusts
the other. The code works once and then expires.

On this computer, after a route to the other device is in `coura.json`:

```sh
python3 coura.pyz pair offer --config coura.json
```

Type the code, or scan the text QR, on the other device:

```sh
python3 coura.pyz pair join ABCD-EFGH --to desk --config coura.json
```

Back on the first device:

```sh
python3 coura.pyz pair confirm --confirm --config coura.json
```

On the second device:

```sh
python3 coura.pyz pair finish --confirm --config coura.json
```

Without `--confirm`, Coura shows the public key and does not save a peer.
`pair offer` and `pair join` print this device's own public key: compare it
with the key the other device shows before you pass `--confirm`. A reply is
trusted once, and only for the join this device sent to the device named by
`--to`. If a different key answers after you looked, or two keys answer the same
pairing, nothing is trusted: start the pairing again.
A second `pair offer` does not replace a code that is still open. If the
reply's acknowledgement is missing, confirm says so and still records the
peer on this device, because that key is held. If the reply is refused,
confirm does not record the peer. Pairing errors print a sentence, not a
traceback.

## Starting it yourself

Coura does not install a service or a hook.

```sh
python3 coura.pyz autostart --print --config coura.json
```

Copy the lines into cron or a systemd unit if you want them. You enable that
yourself.

## Termux

Not measured on a phone. The steps to follow there are:

1. Install Python and OpenSSH in Termux: `pkg install python openssh`.
2. Copy `coura.pyz` onto the phone. Do not share the computer's `state` directory.
3. Run the same `setup` command with a different node name, for example `handset` and kind `phone`.
4. Start `sshd` yourself. Termux usually uses port 8022.
5. Put an SSH command route in each `coura.json`, as in [docs/remote.md](remote.md).
6. Pair with the commands above.
7. If you want a repeating sync, run `autostart --print` and start that command yourself.

## Limits

Messages are at most 6144 bytes. A missing acknowledgement stays
`OUTCOME_UNKNOWN` and that key is not sent again. Pairing does not invent a
network path: the command route has to exist first. Meta-Coura needs the
optional `cryptography` package; Coura still runs when that package is absent.
The text QR is for a short code. An SSH server and a real Termux phone were
not part of the automated checks.
