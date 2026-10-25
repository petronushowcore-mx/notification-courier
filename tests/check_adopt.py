"""Adoption checks: readable show, hook init, private state directories."""
import base64
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUBJECT = ROOT / "courier.py"
CLAUDE = "00000000-0000-0000-0000-000000000001"


def run(args, cwd, env=None, check=True):
    done = subprocess.run(
        [sys.executable, "-B", str(SUBJECT), *args],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=30)
    if check and done.returncode != 0:
        raise AssertionError((args, done.returncode, done.stdout, done.stderr))
    return done


def write_config(directory, state, transports=None, recipients=None):
    body = {"state_dir": str(state)}
    if recipients is None:
        body["recipients"] = {"writer": {"client": "worker", "id": "writer"}}
        body["transports"] = {"worker": {"type": "inbox"}} if transports is None else transports
    else:
        body["recipients"] = recipients
        if transports is not None:
            body["transports"] = transports
    path = Path(directory) / "coura.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def flatten_hook(hook):
    if type(hook) is not dict:
        return str(hook)
    args = hook.get("args")
    if type(args) is list:
        return " ".join([str(hook.get("command", ""))] + [str(item) for item in args])
    command = hook.get("command")
    return command if type(command) is str else str(command)


def hook_commands(settings):
    commands = []
    for group in settings["hooks"]["UserPromptSubmit"]:
        for hook in group["hooks"]:
            commands.append(flatten_hook(hook))
    return commands


def claude_recipients():
    return {"writer": {"client": "claude", "id": CLAUDE}}


def load_module():
    name = "coura_adopt_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, SUBJECT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_text_and_json():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = write_config(root, root / "state")
        note = "Draft saved; please review."
        sent = run(["--config", str(config), "send", "writer", "--key", "demo/result",
                    "--sender", "researcher", "--note", note], root)
        assert "STORED" in sent.stdout
        watched = run(["--config", str(config), "watch", "writer", "--timeout", "0"], root)
        assert watched.stdout.strip() == "MAIL_READY"
        shown = run(["--config", str(config), "show", "writer", "--text"], root)
        assert shown.stdout == (
            "Check demo/result from researcher. Draft saved; please review.\n")
        assert "payload" not in shown.stdout
        assert run(["--config", str(config), "show", "writer", "--text"], root).stdout == ""
        run(["--config", str(config), "send", "writer", "--key", "demo/json",
             "--sender", "researcher", "--note", note], root)
        machine = run(["--config", str(config), "show", "writer"], root)
        document = json.loads(machine.stdout)
        assert set(document) == {"messages"}
        row = document["messages"][0]
        assert set(row) == {"ts", "recipient", "attempt", "payload"}
        assert base64.b64decode(row["payload"], validate=True).decode("utf-8").endswith(note)


def test_non_utf8_text():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = write_config(root, root / "state")
        payload = root / "payload.bin"
        payload.write_bytes(b"\xff\xfe")
        run(["--config", str(config), "send-data", "writer", "--key", "demo/bytes",
             "--file", str(payload)], root)
        shown = run(["--config", str(config), "show", "writer", "--text"], root)
        assert shown.stdout.startswith("[payload is not UTF-8 text]\n")
        assert base64.b64encode(b"\xff\xfe").decode("ascii") in shown.stdout
        assert run(["--config", str(config), "show", "writer"], root).stdout == ""


def test_claude_text_keeps_hook_json():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = write_config(
            root, root / "state",
            recipients={"writer": {"client": "claude", "id": CLAUDE}})
        run(["--config", str(config), "send", "writer", "--key", "demo/result",
             "--sender", "researcher", "--note", "Plain line."], root)
        shown = run(["--config", str(config), "show", "writer", "--text"], root)
        assert shown.stdout == "Check demo/result from researcher. Plain line.\n"
        run(["--config", str(config), "send", "writer", "--key", "demo/hook",
             "--sender", "researcher", "--note", "Hook line."], root)
        machine = run(["--config", str(config), "show", "writer"], root)
        document = json.loads(machine.stdout)
        assert set(document) == {"hookSpecificOutput"}
        assert document["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "Hook line." in document["hookSpecificOutput"]["additionalContext"]


def test_init_dry_run_and_merge():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        space = root / "dir with space"
        space.mkdir()
        config = write_config(space, root / "state", recipients=claude_recipients())
        project = root / "project"
        project.mkdir()
        settings = project / ".claude" / "settings.local.json"
        dry = run(["--config", str(config), "init", "--settings", str(settings),
                   "--dry-run"], project)
        assert not settings.exists()
        assert not (project / ".claude").exists()
        assert dry.stdout.startswith("---")
        assert "show" in dry.stdout and "writer" in dry.stdout
        # The dry run prints a diff of the settings JSON: a Windows path appears with its
        # backslashes escaped, so compare the JSON form of each value, not the raw string.
        assert json.dumps(str(config.resolve()))[1:-1] in dry.stdout
        assert json.dumps(sys.executable)[1:-1] in dry.stdout
        assert "'" in dry.stdout or '"' in dry.stdout
        settings.parent.mkdir(parents=True)
        original = {
            "permissions": {"allow": ["keep"]},
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
                "UserPromptSubmit": [{"hooks": [
                    {"type": "command", "command": "echo keep-me", "timeout": 5}
                ]}]
            }
        }
        settings.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(settings, 0o640)
            before_mode = stat.S_IMODE(settings.stat().st_mode)
        updated = run(["--config", str(config), "init", "--settings", str(settings)], project)
        assert updated.stdout.strip() == "updated %s" % settings
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert data["permissions"] == original["permissions"]
        assert data["hooks"]["Stop"] == original["hooks"]["Stop"]
        commands = hook_commands(data)
        assert "echo keep-me" in commands
        ours = [item for item in commands if "courier.py" in item and "show" in item]
        assert len(ours) == 1 and ours[0].rstrip().endswith("show writer")
        assert str(config.resolve()) in ours[0]
        if os.name != "nt":
            assert stat.S_IMODE(settings.stat().st_mode) == before_mode
        frozen = settings.read_bytes()
        again = run(["--config", str(config), "init", "--settings", str(settings)], project)
        assert again.stdout.strip() == "unchanged %s" % settings
        assert settings.read_bytes() == frozen
        dry_again = run(["--config", str(config), "init", "--settings", str(settings),
                         "--dry-run"], project)
        assert dry_again.stdout.strip() == "# unchanged: %s" % settings
        # An exact second copy is ours, but init must not delete hooks.
        ours_hook = [hook for hook in event_hooks(data, "UserPromptSubmit")
                     if flatten_hook(hook) == ours[0]][0]
        copied = dict(ours_hook)
        copied["timeout"] = 99
        data["hooks"]["UserPromptSubmit"].append({"hooks": [copied]})
        settings.write_text(json.dumps(data), encoding="utf-8")
        kept = settings.read_bytes()
        duplicate = run(["--config", str(config), "init", "--settings", str(settings)], project)
        assert duplicate.stdout.strip() == "unchanged %s" % settings
        assert settings.read_bytes() == kept
        assert hook_commands(json.loads(kept)).count(ours[0]) == 2


def test_init_user_default_and_refuse():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = write_config(root, root / "state", recipients=claude_recipients())
        home = root / "home"
        home.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        dry = run(["--config", str(config), "init", "--user", "--dry-run"], root, env=env)
        user_settings = home / ".claude" / "settings.json"
        assert not user_settings.exists()
        assert str(user_settings) in dry.stdout
        written = run(["--config", str(config), "init", "--user"], root, env=env)
        assert user_settings.is_file()
        assert written.stdout.strip() == "updated %s" % user_settings
        project = root / "proj"
        project.mkdir()
        run(["--config", str(config), "init"], project)
        local = project / ".claude" / "settings.local.json"
        assert local.is_file()
        if os.name != "nt":
            assert stat.S_IMODE(local.stat().st_mode) == 0o600
        both = run(["--config", str(config), "init", "--user", "--settings", str(local)],
                   project, check=False)
        assert both.returncode == 1
        assert "REFUSED" in both.stderr
        bad = project / "not-json.json"
        bad.write_text("{", encoding="utf-8")
        refused = run(["--config", str(config), "init", "--settings", str(bad), "--dry-run"],
                      project, check=False)
        assert refused.returncode == 1
        assert bad.read_text(encoding="utf-8") == "{"


def test_init_refuses_different_config_hook():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        project = root / "project"
        project.mkdir()
        settings = project / ".claude" / "settings.local.json"
        settings.parent.mkdir()
        foreign = "python3 /opt/other/courier.py --config /opt/other/coura.json show writer"
        reviewer = "python3 /opt/other/courier.py --config /opt/other/coura.json show reviewer"
        original = {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "echo keep-me", "timeout": 5}]},
            {"hooks": [{"type": "command", "command": foreign, "timeout": 10}]},
            {"hooks": [{"type": "command", "command": reviewer, "timeout": 10}]},
        ]}}
        settings.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
        before = settings.read_bytes()
        config = write_config(root, root / "state", recipients=claude_recipients())
        dry = run(["--config", str(config), "init", "--settings", str(settings), "--dry-run"],
                  project, check=False)
        assert dry.returncode == 1
        assert "REFUSED" in dry.stderr
        assert "# conflict:" in dry.stdout
        assert foreign in dry.stdout
        assert str(config.resolve()) in dry.stdout
        assert "state_dir:" in dry.stdout
        assert settings.read_bytes() == before
        real = run(["--config", str(config), "init", "--settings", str(settings)],
                   project, check=False)
        assert real.returncode == 1
        assert foreign in real.stdout
        assert settings.read_bytes() == before
        # A show for another recipient is not this hook, so it must not block install.
        settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": reviewer, "timeout": 10}]},
            {"hooks": [{"type": "command", "command": "echo keep-me"}]},
        ]}}), encoding="utf-8")
        added = run(["--config", str(config), "init", "--settings", str(settings)], project)
        assert added.returncode == 0
        commands = hook_commands(json.loads(settings.read_text(encoding="utf-8")))
        assert reviewer in commands and "echo keep-me" in commands
        assert sum("show writer" in item and str(config.resolve()) in item for item in commands) == 1
        assert foreign not in commands
        # An unparsable command that still looks like our show is ambiguous.
        broken = 'python3 "/opt/other/courier.py --config /opt/other/coura.json show writer'
        settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": broken}]}
        ]}}), encoding="utf-8")
        frozen = settings.read_bytes()
        ambiguous = run(["--config", str(config), "init", "--settings", str(settings),
                         "--dry-run"], project, check=False)
        assert ambiguous.returncode == 1
        assert broken in ambiguous.stdout
        assert "ambiguous" in ambiguous.stdout
        assert settings.read_bytes() == frozen


def test_init_refuses_non_claude_route():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        settings = root / "settings.json"
        for recipients, transports in (
                ({"writer": {"client": "worker", "id": "writer"}},
                 {"worker": {"type": "inbox"}}),
                ({"writer": {"client": "codex", "id": CLAUDE}}, None)):
            config = write_config(root, root / "state", transports=transports,
                                  recipients=recipients)
            done = run(["--config", str(config), "init", "--settings", str(settings),
                        "--dry-run"], root, check=False)
            assert done.returncode == 1, done.stderr
            assert "REFUSED" in done.stderr
            assert "Claude hook JSON" in done.stderr
            assert not settings.exists()


def test_windows_hook_quoting():
    """Exec form and POSIX sh -c must round-trip argv. A literal quote compare is not proof.

    Claude's documented shell form is sh -c, Git Bash, or PowerShell, not cmd.exe.
    Git Bash and a Windows host are not executed here. cmd.exe quoting is not a PASS.
    """
    module = load_module()
    dangerous = [
        "two words",
        "a&b|c<d>e^f%",
        'quote"here',
        "100%",
        "a'b",
        "semi;colon",
        "$(nope)",
        "`nope`",
        r"C:\Program Files\python.exe",
        "writer&whoami",
    ]
    probe = "import json,sys; sys.stdout.write(json.dumps(sys.argv[1:]))"
    direct = subprocess.run(
        [sys.executable, "-c", probe, *dangerous],
        capture_output=True, text=True, timeout=30)
    assert direct.returncode == 0, direct.stderr
    assert json.loads(direct.stdout) == dangerous
    spec = module.exec_form([sys.executable, "-c", probe, *dangerous])
    executed = subprocess.run(
        [spec["command"], *spec["args"]],
        capture_output=True, text=True, timeout=30, shell=False)
    assert executed.returncode == 0, executed.stderr
    assert json.loads(executed.stdout) == dangerous
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        odd = root / "a&b % 'dir'"
        odd.mkdir()
        config = write_config(odd, root / "state", recipients=claude_recipients())
        hook = module.show_hook_spec(config, "writer")
        assert hook["args"][-2:] == ["show", "writer"]
        assert str(config.resolve()) in hook["args"]
        assert all(isinstance(item, str) for item in hook["args"])
        ran = subprocess.run(
            [hook["command"], *hook["args"]],
            capture_output=True, text=True, timeout=30, shell=False)
        assert ran.returncode == 0, ran.stderr
        quoted = " ".join(module.quote_arg(part, windows=False) for part in (
            [sys.executable, "-c", probe, *dangerous]))
        shell = subprocess.run(["sh", "-c", quoted], capture_output=True, text=True, timeout=30)
        assert shell.returncode == 0, shell.stderr
        assert json.loads(shell.stdout) == dangerous
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        print("UNRUN powershell hook round-trip: powershell is not installed on this host")
    else:
        # A quoted first token is an expression in PowerShell; the call operator runs it.
        ps_parts = "& " + " ".join(module.quote_powershell_arg(part) for part in (
            [sys.executable, "-c", probe, *dangerous]))
        ps = subprocess.run(
            [powershell, "-NoProfile", "-Command", ps_parts],
            capture_output=True, text=True, timeout=30)
        assert ps.returncode == 0, ps.stderr
        assert json.loads(ps.stdout) == dangerous
    if os.name != "nt":
        print("UNRUN Windows Git Bash and cmd.exe: not executed on this host; cmd.exe is not the documented Claude hook shell")


def test_private_dirs_hint_and_removed_helper():
    module = load_module()
    assert not hasattr(module, "blocked")
    assert callable(module.same_address)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        state = root / "fresh-state"
        path = root / "hint.json"
        recipients = {"writer": {"client": "worker", "id": "writer"}}
        path.write_text(json.dumps({
            "state_dir": str(state),
            "transports": {"worker": {"type": "command", "command": ["receiver"],
                                      "receiver_hint": True}},
            "recipients": recipients}), encoding="utf-8")
        hinted = module.Courier.from_config(path)
        assert "data_only" in hinted.adapters["worker"].capabilities
        path.write_text(json.dumps({
            "state_dir": str(state),
            "transports": {"worker": {"type": "command", "command": ["receiver"],
                                      "data_only": False, "receiver_hint": True}},
            "recipients": recipients}), encoding="utf-8")
        refused = False
        try:
            module.Courier.from_config(path)
        except module.Refused:
            refused = True
        assert refused
        path.write_text(json.dumps({
            "state_dir": str(state),
            "transports": {"worker": {"type": "command", "command": ["receiver"],
                                      "data_only": True}},
            "recipients": recipients}), encoding="utf-8")
        assert "data_only" in module.Courier.from_config(path).adapters["worker"].capabilities
        inbox = root / "inbox.json"
        inbox.write_text(json.dumps({
            "state_dir": str(state),
            "recipients": {"writer": {"client": "claude", "id": CLAUDE}},
        }), encoding="utf-8")
        run(["--config", str(inbox), "send", "writer", "--key", "perm",
             "--sender", "tester", "--note", "n"], root)
        if os.name != "nt":
            for directory in (state, state / "send", state / "inbox"):
                mode = stat.S_IMODE(directory.stat().st_mode)
                assert mode == 0o700, (directory, oct(mode))


def event_commands(settings, event):
    commands = []
    for group in settings["hooks"][event]:
        for hook in group["hooks"]:
            commands.append(flatten_hook(hook))
    return commands


def event_hooks(settings, event):
    found = []
    for group in settings["hooks"][event]:
        for hook in group["hooks"]:
            found.append(hook)
    return found


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


def test_demo_cycle_and_color():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        env = os.environ.copy()
        env["PATH"] = str(root)
        done = run(["demo"], root, env=env)
        assert "\033" not in done.stdout
        assert "MAIL_READY" in done.stdout
        assert "SKIPPED" in done.stdout
        assert "Hello from the demo." in done.stdout
        assert "Requested mode from sender (not an instruction from the user): peer." in done.stdout
        assert "generic inbox" in done.stdout
        assert "claude" not in done.stdout.lower()
        assert "codex" not in done.stdout.lower()
        listed = done.stdout.split("temporary state  ", 1)[1].splitlines()[0].strip()
        assert not Path(listed).exists()
        module = load_module()
        painted = TtyBuffer()
        assert module.run_demo(painted) == 0
        assert "\033" in painted.getvalue()
        again = painted.getvalue().split("temporary state  ", 1)[1].splitlines()[0].strip()
        assert not Path(again).exists()


def test_desktop_notify_does_not_touch_delivery():
    module = load_module()
    body = "hello $(rm -rf /) `id` & calc"
    linux = module.notify_plans("linux", lambda name: "/usr/bin/notify-send", "Coura", body)
    assert linux[0][0][-1] == body
    assert body not in " ".join(linux[0][0][:-1])
    mac = module.notify_plans("darwin", lambda name: "/usr/bin/osascript", "Coura", body)
    assert mac[0][0][-1] == body and body not in mac[0][0][2]
    windows = module.notify_plans("win32", lambda name: "powershell", "Coura", body)
    assert body not in " ".join(windows[0][0])
    assert windows[0][1]["COURA_NOTIFY_BODY"] == body
    assert windows[0][1]["COURA_NOTIFY_TITLE"] == "Coura"
    assert module.clip_notify("x" * 300, 240) == "x" * 240
    assert module.clip_notify("a\nb\x00c", 80) == "a bc"
    assert module.desktop_notify(
        "Coura", body, system="linux", which=lambda name: "/usr/bin/notify-send",
        runner=lambda argv, env: (_ for _ in ()).throw(RuntimeError("notify failed"))) is False
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = write_config(root, root / "state")
        note = "ping $(touch %s)" % (root / "pwned")
        run(["--config", str(config), "send", "writer", "--key", "desk",
             "--sender", "alpha", "--note", note], root)
        journal = (root / "state" / "journal.jsonl").read_bytes()
        env = os.environ.copy()
        env["PATH"] = str(root / "empty-path")
        (root / "empty-path").mkdir()
        watched = run(["--config", str(config), "watch", "writer", "--timeout", "0",
                       "--desktop"], root, env=env)
        assert watched.stdout.strip() == "MAIL_READY"
        assert (root / "state" / "journal.jsonl").read_bytes() == journal
        assert not (root / "pwned").exists()
        inbox = module.Courier.from_config(config).reader(
            module.Address("worker", "writer"))
        target = module.Address("worker", "writer")

        def boom():
            raise RuntimeError("desktop failed")

        assert inbox.watch(target, 0, 0.2, boom) is True
        assert (root / "state" / "journal.jsonl").read_bytes() == journal


def test_init_on_stop_any_recipient():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        recipients = {
            "writer": {"client": "claude", "id": CLAUDE},
            "beta": {"client": "worker", "id": "beta"},
            "reviewer": {"client": "codex", "id": "00000000-0000-0000-0000-000000000002"},
        }
        config = write_config(root, root / "state",
                              transports={"worker": {"type": "inbox"}},
                              recipients=recipients)
        settings = root / "settings.json"
        dry = run(["--config", str(config), "init", "--settings", str(settings),
                   "--on-stop", "beta", "--dry-run"], root)
        assert not settings.exists()
        assert "UserPromptSubmit" in dry.stdout
        assert "Stop" in dry.stdout
        assert "host-event" in dry.stdout and "beta" in dry.stdout
        assert "--expect" in dry.stdout and "Stop" in dry.stdout
        assert "session finished" not in dry.stdout
        assert "--unique-key" not in dry.stdout
        written = run(["--config", str(config), "init", "--settings", str(settings),
                       "--on-stop", "beta"], root)
        assert written.stdout.strip() == "updated %s" % settings
        data = json.loads(settings.read_text(encoding="utf-8"))
        shows = event_commands(data, "UserPromptSubmit")
        stops = event_commands(data, "Stop")
        assert any("show writer" in item for item in shows)
        ours = [item for item in stops if "host-event" in item and "beta" in item and "--expect" in item]
        assert len(ours) == 1
        assert "session finished" not in ours[0]
        assert "--unique-key" not in ours[0]
        frozen = settings.read_bytes()
        again = run(["--config", str(config), "init", "--settings", str(settings),
                     "--on-stop", "beta"], root)
        assert again.stdout.strip() == "unchanged %s" % settings
        assert settings.read_bytes() == frozen
        stop_hook = [hook for hook in event_hooks(data, "Stop") if flatten_hook(hook) == ours[0]][0]
        copied = dict(stop_hook)
        copied["timeout"] = 99
        data["hooks"]["Stop"].append({"hooks": [copied]})
        settings.write_text(json.dumps(data), encoding="utf-8")
        kept = settings.read_bytes()
        duplicate = run(["--config", str(config), "init", "--settings", str(settings),
                         "--on-stop", "beta"], root)
        assert duplicate.stdout.strip() == "unchanged %s" % settings
        assert settings.read_bytes() == kept
        foreign = "python3 /opt/other/courier.py --config /opt/other/coura.json send beta --key other"
        settings.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": foreign}]}
        ]}}), encoding="utf-8")
        before = settings.read_bytes()
        conflict = run(["--config", str(config), "init", "--settings", str(settings),
                        "--on-stop", "beta", "--dry-run"], root, check=False)
        assert conflict.returncode == 1
        assert foreign in conflict.stdout
        assert "# conflict:" in conflict.stdout
        assert "state_dir:" in conflict.stdout
        assert settings.read_bytes() == before
        other = "python3 /opt/other/courier.py --config /opt/other/coura.json send gamma --key other"
        settings.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": other}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo keep"}]}],
        }}), encoding="utf-8")
        added = run(["--config", str(config), "init", "--settings", str(settings),
                     "--on-stop", "beta"], root)
        assert added.returncode == 0
        merged = json.loads(settings.read_text(encoding="utf-8"))
        stop_commands = event_commands(merged, "Stop")
        assert other in stop_commands
        assert any("beta" in item and "host-event" in item and "--expect" in item for item in stop_commands)
        assert "echo keep" in event_commands(merged, "UserPromptSubmit")
        broken = 'python3 "/opt/other/courier.py --config /opt/other/coura.json send beta'
        settings.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": broken}]}
        ]}}), encoding="utf-8")
        frozen = settings.read_bytes()
        ambiguous = run(["--config", str(config), "init", "--settings", str(settings),
                         "--on-stop", "beta", "--dry-run"], root, check=False)
        assert ambiguous.returncode == 1
        assert broken in ambiguous.stdout
        assert settings.read_bytes() == frozen
        missing = run(["--config", str(config), "init", "--settings", str(settings),
                       "--on-stop", "nobody", "--dry-run"], root, check=False)
        assert missing.returncode == 1
        assert "REFUSED" in missing.stderr
        codex_settings = root / "codex-stop.json"
        codex = run(["--config", str(config), "init", "--settings", str(codex_settings),
                     "--on-stop", "reviewer", "--dry-run"], root)
        assert "reviewer" in codex.stdout
        assert "host-event" in codex.stdout
        assert not codex_settings.exists()


def test_doctor_generic_and_builtin_routes():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        missing = root / "missing-state"
        config = write_config(root, missing)
        done = run(["--config", str(config), "doctor"], root)
        assert done.returncode == 0
        assert "warn" in done.stdout
        assert "state" in done.stdout
        assert not missing.exists()
        names = [line.split()[1] for line in done.stdout.splitlines() if line.strip()]
        assert "codex" not in names
        assert not any(name.startswith("hook:") for name in names)
        state = root / "state"
        state.mkdir()
        (root / "loose").mkdir()
        loose_config = write_config(root / "loose", state)
        if os.name != "nt":
            os.chmod(state, 0o755)
            loose = run(["--config", str(loose_config), "doctor", "--json"], root, check=False)
            assert loose.returncode == 1
            document = json.loads(loose.stdout)
            assert document["ok"] is False
            assert any(item["name"] == "state" and item["status"] == "fail" for item in document["checks"])
            os.chmod(state, 0o700)
        (root / "cmd").mkdir()
        command = write_config(
            root / "cmd", root / "cmd-state",
            transports={"worker": {"type": "command", "command": [sys.executable, "-c", "raise SystemExit(0)"]}},
            recipients={"writer": {"client": "worker", "id": "writer"}})
        (root / "cmd-state").mkdir(mode=0o700)
        healthy = run(["--config", str(command), "doctor", "--json"], root)
        body = json.loads(healthy.stdout)
        assert healthy.returncode == 0 and body["ok"] is True
        by_name = {item["name"]: item["status"] for item in body["checks"]}
        assert by_name["command:writer"] == "pass"
        assert by_name["handshake:writer"] == "warn"
        assert "codex" not in by_name
        assert not any(name.startswith("hook:") for name in by_name)
        (root / "bad").mkdir()
        bad_cmd = write_config(
            root / "bad", root / "bad-state",
            transports={"worker": {"type": "command", "command": ["no-such-coura-bin"]}},
            recipients={"writer": {"client": "worker", "id": "writer"}})
        (root / "bad-state").mkdir(mode=0o700)
        failed = run(["--config", str(bad_cmd), "doctor"], root, check=False)
        assert failed.returncode == 1
        assert "command:writer" in failed.stdout and "fail" in failed.stdout
        (root / "box").mkdir()
        inbox = write_config(root / "box", state, recipients=claude_recipients())
        absent = run(["--config", str(inbox), "doctor", "--settings", str(root / "none.json")], root)
        assert absent.returncode == 0
        assert "hook:writer" in absent.stdout and "warn" in absent.stdout
        run(["--config", str(inbox), "init", "--settings", str(root / "hook.json")], root)
        exact = run(["--config", str(inbox), "doctor", "--settings", str(root / "hook.json")], root)
        assert exact.returncode == 0
        assert "hook:writer" in exact.stdout
        hook_line = [line for line in exact.stdout.splitlines() if "hook:writer" in line][0]
        assert hook_line.startswith("pass")
        foreign = "python3 /opt/other/courier.py --config /opt/other/coura.json show writer"
        (root / "hook.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": foreign}]}
        ]}}), encoding="utf-8")
        conflict = run(["--config", str(inbox), "doctor", "--settings", str(root / "hook.json")],
                       root, check=False)
        assert conflict.returncode == 1
        assert "hook:writer" in conflict.stdout and "fail" in conflict.stdout
        codex_id = "00000000-0000-0000-0000-000000000002"
        no_queue = root / "nq.json"
        no_queue.write_text(json.dumps({
            "state_dir": str(state),
            "recipients": {"reviewer": {"client": "codex", "id": codex_id}},
        }), encoding="utf-8")
        no_bin = run(["--config", str(no_queue), "doctor"], root, check=False)
        assert no_bin.returncode == 1
        assert "codex" in no_bin.stdout and "fail" in no_bin.stdout
        missing_bin = root / "mb.json"
        missing_bin.write_text(json.dumps({
            "state_dir": str(state),
            "codex_queue_command": ["no-such-coura-codex"],
            "recipients": {"reviewer": {"client": "codex", "id": codex_id}},
        }), encoding="utf-8")
        assert run(["--config", str(missing_bin), "doctor"], root, check=False).returncode == 1
        versioned = root / "ver.json"
        versioned.write_text(json.dumps({
            "state_dir": str(state),
            "codex_queue_command": [sys.executable],
            "recipients": {"reviewer": {"client": "codex", "id": codex_id}},
        }), encoding="utf-8")
        version = run(["--config", str(versioned), "doctor", "--json"], root)
        found = json.loads(version.stdout)
        codex_check = [item for item in found["checks"] if item["name"] == "codex"][0]
        assert codex_check["status"] == "pass"
        assert codex_check["detail"]
        broken = root / "broken.json"
        broken.write_text("{", encoding="utf-8")
        refused = run(["--config", str(broken), "doctor", "--json"], root, check=False)
        assert refused.returncode == 1
        assert json.loads(refused.stdout)["ok"] is False


def test_log_filters():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        recipients = {
            "writer": {"client": "worker", "id": "writer"},
            "other": {"client": "worker", "id": "other"},
        }
        config = write_config(root, root / "state", recipients=recipients,
                              transports={"worker": {"type": "inbox"}})
        empty = run(["--config", str(config), "log", "--json"], root)
        assert json.loads(empty.stdout) == []
        run(["--config", str(config), "send", "writer", "--key", "demo/result",
             "--sender", "alpha", "--note", "hello", "--lens", "critic"], root)
        run(["--config", str(config), "send", "other", "--key", "demo/other",
             "--sender", "beta", "--note", "away"], root)
        listed = run(["--config", str(config), "log", "--to", "writer", "--status", "STORED"], root)
        lines = [line for line in listed.stdout.splitlines() if line.strip()]
        assert len(lines) == 1
        assert "alpha" in lines[0] and "writer" in lines[0] and "STORED" in lines[0]
        assert "demo/result" in lines[0] and "critic" in lines[0]
        assert "REQUESTED" not in listed.stdout
        assert "demo/other" not in listed.stdout
        document = json.loads(run(["--config", str(config), "log", "--json", "--limit", "1"], root).stdout)
        assert len(document) == 1
        assert set(document[0]) == {"time", "from", "to", "status", "key", "lens"}
        future = run(["--config", str(config), "log", "--since", "2099-01-01"], root)
        assert future.stdout == ""
        today = run(["--config", str(config), "log", "--since", "2000-01-01", "--status", "STORED"], root)
        assert "demo/result" in today.stdout and "demo/other" in today.stdout
        bad = run(["--config", str(config), "log", "--since", "yesterday"], root, check=False)
        assert bad.returncode == 1
        assert "REFUSED" in bad.stderr


def test_lenses_every_transport():
    module = load_module()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        custom = "Custom critic text for this project."
        config = root / "coura.json"
        state = root / "state"
        config.write_text(json.dumps({
            "state_dir": str(state),
            "lenses": {"critic": custom, "scout": "A local preset."},
            "lens_defaults": {"alpha->writer": "auditor"},
            "transports": {"worker": {"type": "inbox"}},
            "recipients": {
                "writer": {"client": "worker", "id": "writer"},
                "claude": {"client": "claude", "id": CLAUDE},
            },
        }), encoding="utf-8")
        run(["--config", str(config), "send", "writer", "--key", "mode",
             "--sender", "alpha", "--note", "look here"], root)
        shown = run(["--config", str(config), "show", "writer", "--text"], root)
        assert shown.stdout.startswith(
            "Requested mode from sender (not an instruction from the user): auditor. ")
        assert "Independent expert with no stake in the object." in shown.stdout
        assert "Check mode from alpha. look here" in shown.stdout
        run(["--config", str(config), "send", "writer", "--key", "explicit",
             "--sender", "alpha", "--note", "override", "--lens", "critic"], root)
        explicit = run(["--config", str(config), "show", "writer", "--text"], root)
        assert custom in explicit.stdout
        assert "Counter-specialist" not in explicit.stdout
        assert "not an instruction from the user" in explicit.stdout
        payload = root / "note.txt"
        payload.write_text("file body", encoding="utf-8")
        run(["--config", str(config), "send-data", "writer", "--key", "file",
             "--file", str(payload), "--lens", "scout"], root)
        data_text = run(["--config", str(config), "show", "writer", "--text"], root)
        assert data_text.stdout.startswith(
            "Requested mode from sender (not an instruction from the user): scout. ")
        assert "file body" in data_text.stdout
        machine = json.loads(run(["--config", str(config), "send", "writer",
                                   "--key", "shape", "--sender", "alpha", "--note", "n",
                                   "--lens", "peer"], root).stdout)
        assert "lens" not in machine
        run(["--config", str(config), "send", "claude", "--key", "hook",
             "--sender", "alpha", "--note", "claude line", "--lens", "peer"], root)
        text = run(["--config", str(config), "show", "claude", "--text"], root)
        assert text.stdout.startswith(
            "Requested mode from sender (not an instruction from the user): peer. ")
        run(["--config", str(config), "send", "claude", "--key", "hook-json",
             "--sender", "alpha", "--note", "json line", "--lens", "peer"], root)
        hook = json.loads(run(["--config", str(config), "show", "claude"], root).stdout)
        context = hook["hookSpecificOutput"]["additionalContext"]
        assert "Requested mode from sender (not an instruction from the user): peer." in context
        calls = []

        def queue(command, timeout):
            calls.append(command)
            thread = command[command.index("--thread") + 1]
            item = "00000000-0000-0000-0000-000000000009"
            return 0, ("Queued message %s for thread %s." % (item, thread)).encode(), b""

        codex_id = "00000000-0000-0000-0000-000000000002"
        courier = module.Courier(
            state / "codex",
            {"reviewer": module.Address("codex", codex_id)},
            queue=module.CodexQueue(["codex"], runner=queue))
        module.bind_lens(courier, "auditor")
        receipt = courier.send("reviewer", "queue-key", "alpha", "queued")
        assert receipt.status == "QUEUED"
        message = calls[0][calls[0].index("--message") + 1]
        assert message.startswith(
            "Requested mode from sender (not an instruction from the user): auditor. ")
        assert "Check queue-key from alpha. queued" in message
        wire = []

        def command_runner(command, timeout, payload):
            wire.append(payload)
            request = json.loads(payload)
            reply = {"status": "STORED", "recipient": request["recipient"],
                     "attempt": request["attempt"]}
            return 0, json.dumps(reply).encode(), b""

        bound = module.Courier(
            state / "cmd",
            {"writer": module.Address("worker", "writer")},
            adapters={"worker": module.Adapter(
                module.CommandAdapter(["receiver"], runner=command_runner),
                frozenset({"notification", "data"}))})
        plain = bound.send_data("writer", "plain", b"exact-bytes")
        assert plain.status == "STORED"
        assert base64.b64decode(json.loads(wire[-1])["payload"]) == b"exact-bytes"
        module.bind_lens(bound, "peer")
        prefixed = bound.send_data("writer", "prefixed", b"exact-bytes")
        assert prefixed.status == "STORED"
        merged = base64.b64decode(json.loads(wire[-1])["payload"])
        assert merged.endswith(b"exact-bytes")
        assert merged.startswith(
            b"Requested mode from sender (not an instruction from the user): peer. ")
        assert b"\nexact-bytes" in merged
        module.bind_lens(bound, "peer")
        rejected = False
        try:
            bound.send_data("writer", "huge", b"x" * 6144)
        except module.Refused:
            rejected = True
        assert rejected
        refused = run(["--config", str(config), "send", "writer", "--key", "bad",
                       "--sender", "alpha", "--lens", "Nope"], root, check=False)
        assert refused.returncode == 1
        assert "Invalid lens name" in refused.stderr or "Unknown lens" in refused.stderr
        unknown = run(["--config", str(config), "send", "writer", "--key", "bad",
                       "--sender", "alpha", "--lens", "missing"], root, check=False)
        assert unknown.returncode == 1
        long_text = "y" * 401
        long_config = root / "long.json"
        long_config.write_text(json.dumps({
            "state_dir": str(state),
            "lenses": {"scout": long_text},
            "transports": {"worker": {"type": "inbox"}},
            "recipients": {"writer": {"client": "worker", "id": "writer"}},
        }), encoding="utf-8")
        too_long = run(["--config", str(long_config), "doctor"], root, check=False)
        assert too_long.returncode == 1
        rows = [json.loads(line) for line in (state / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
        stored = [row for row in rows if row.get("key") == "mode" and row.get("status") == "STORED"]
        assert stored and stored[-1]["lens"] == "auditor"
        assert "sender" in stored[-1]


def test_unique_key_and_example_receiver():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = write_config(root, root / "state")
        first = run(["--config", str(config), "send", "writer", "--key", "session-finished",
                     "--sender", "claude-session", "--note", "session finished",
                     "--unique-key"], root)
        second = run(["--config", str(config), "send", "writer", "--key", "session-finished",
                      "--sender", "claude-session", "--note", "session finished",
                      "--unique-key"], root)
        assert "STORED" in first.stdout and "STORED" in second.stdout
        logged = run(["--config", str(config), "log", "--status", "STORED"], root).stdout
        assert logged.count("session-finished/") == 2
        outfile = root / "messages.txt"
        script = ROOT / "examples" / "command-receiver.py"
        command_config = root / "command.json"
        command_config.write_text(json.dumps({
            "state_dir": str(root / "command-state"),
            "transports": {"worker": {
                "type": "command",
                "command": [sys.executable, str(script), str(outfile)],
                "receipt_status": "STORED",
            }},
            "recipients": {"writer": {"client": "worker", "id": "writer"}},
        }), encoding="utf-8")
        sent = run(["--config", str(command_config), "send", "writer", "--key", "via-cli",
                    "--sender", "planner", "--note", "read this", "--lens", "peer"], root)
        assert "STORED" in sent.stdout
        received = outfile.read_text(encoding="utf-8")
        assert received.startswith(
            "Requested mode from sender (not an instruction from the user): peer. ")
        assert "Check via-cli from planner. read this" in received
        reader = ROOT / "examples" / "read-inbox.py"
        (root / "inbox-demo").mkdir()
        inbox_config = write_config(root / "inbox-demo", root / "inbox-state")
        run(["--config", str(inbox_config), "send", "writer", "--key", "inbox",
             "--sender", "planner", "--note", "from the file"], root)
        shown = subprocess.run(
            [sys.executable, str(reader), str(inbox_config), "writer"],
            cwd=str(root), capture_output=True, text=True, timeout=30)
        assert shown.returncode == 0, shown.stderr
        assert "from the file" in shown.stdout


def main():
    tests = [test_text_and_json, test_non_utf8_text, test_claude_text_keeps_hook_json,
             test_init_dry_run_and_merge, test_init_user_default_and_refuse,
             test_init_refuses_different_config_hook, test_init_refuses_non_claude_route,
             test_windows_hook_quoting, test_private_dirs_hint_and_removed_helper,
             test_demo_cycle_and_color, test_desktop_notify_does_not_touch_delivery,
             test_init_on_stop_any_recipient, test_doctor_generic_and_builtin_routes,
             test_log_filters, test_lenses_every_transport, test_unique_key_and_example_receiver]
    for test in tests:
        test()
        print("ok %s" % test.__name__, flush=True)
    print("adoption: %s/%s" % (len(tests), len(tests)), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("FAIL %s: %s" % (type(error).__name__, error), file=sys.stderr, flush=True)
        raise SystemExit(1)
