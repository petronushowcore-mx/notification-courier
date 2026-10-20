"""Bounded offline checks: real file inbox, fake queue runner, source regressions."""
import importlib.util
import io
import os
import subprocess
import threading
import time
from contextlib import contextmanager
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("courier", ROOT / "courier.py")
courier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = courier
spec.loader.exec_module(courier)
A = "00000000-0000-0000-0000-000000000001"
B = "00000000-0000-0000-0000-000000000002"
ITEM = "00000000-0000-0000-0000-000000000003"
CURRENT = ""
# A marker attributes its whole named function, not an individual require() clause.


def require(value):
    if not value:
        raise AssertionError(CURRENT)


def setup(m, root, runner=None, urgent=None):
    recipients = {"writer": m.Address("claude", A),
                  "other": m.Address("codex", A),
                  "reviewer": m.Address("codex", B)}
    queue = None if runner is None else m.CodexQueue(["example-executable"], runner=runner)
    return m.Courier(root, recipients, queue, urgent)


def exact(command, timeout):
    return 0, ("Queued message %s for thread %s." % (ITEM, command[-3])).encode(), b""


def resolves(m, root):
    c = setup(m, root)
    require(c.resolve("claude:" + A) == m.Address("claude", A))
    require(c.resolve("codex:" + B) == m.Address("codex", B))


def self_scope(m, root):
    c = setup(m, root, exact)
    own = m.Address("codex", A)
    require(c.send("writer", "cross-client", "demo", caller=own).status == "STORED")
    require(c.send("reviewer", "same-prefix", "demo", caller=own).status == "QUEUED")
    before = (root / "journal.jsonl").read_bytes()
    require(c.send("other", "same", "demo", caller=own).status == "SELF")
    require((root / "journal.jsonl").read_bytes() == before)


def ambiguity(m, root):
    try:
        setup(m, root).resolve("00000000")
    except m.Refused:
        return
    require(False)


def command_boundary(m, root):
    original_os, original_run, original_which = m.os, m.run_command, m.shutil.which
    calls = []
    reservations = []
    def fake(command, timeout):
        calls.append(command)
        return exact(command, timeout)
    m.os = types.SimpleNamespace(name="nt")
    m.run_command = fake
    m.shutil.which = lambda name: name
    target = m.Address("codex", B)
    try:
        good = m.CodexQueue(["example.exe"])(target, "notice",
                                            lambda: reservations.append("requested"))
        require(good.status == "QUEUED")
        for command in ("example.cmd", "example.bat", "cmd.exe", "powershell.exe",
                        "pwsh", "bash", "sh"):
            try:
                m.CodexQueue([command])(target, "notice",
                                       lambda: reservations.append("requested"))
            except m.Refused:
                pass
            else:
                require(False)
        require(len(calls) == 1 and reservations == ["requested"])
    finally:
        m.os, m.run_command, m.shutil.which = original_os, original_run, original_which


def requested_blocks(m, root):
    c = setup(m, root, exact)
    m.append_record(root / "journal.jsonl",
                    dict(client="codex", id=B, key="reserved", status="REQUESTED"))
    require(c.send("reviewer", "reserved", "demo").status == "SKIPPED")
    require(len(list(m.records(root / "journal.jsonl"))) == 1)


def literal_message(m, root):
    c = setup(m, root)
    note = 'quoted "\n; $() and backslashes \\ stay data'
    require(c.send("writer", "example/key", "demo", note).status == "STORED")
    raw = c.inbox.paths(c.resolve("writer"))["jsonl"].read_bytes()
    require(raw.count(b"\n") == 1)
    require(json.loads(raw)["line"] == "Check example/key from demo. " + note)


def append_before_ring(m, root):
    c = setup(m, root)
    original = m.append_record
    observed = []
    ring = c.inbox.paths(c.resolve("writer"))["ring"]
    ring.parent.mkdir(parents=True, exist_ok=True)

    def recording(path, row):
        if path.parent.name == "inbox":
            observed.append(not ring.exists())
        original(path, row)

    m.append_record = recording
    try:
        require(c.send("writer", "order", "demo").status == "STORED")
        require(observed == [True])
        require(ring.exists())
    finally:
        m.append_record = original


def partial_waits(m, root):
    c = setup(m, root)
    target = c.resolve("writer")
    c.inbox(target, "complete", lambda: None)
    path = c.inbox.paths(target)["jsonl"]
    complete = path.stat().st_size
    with path.open("ab") as stream:
        stream.write(b'{"line":')
    try:
        letters, end = c.inbox.unread(target)
    except json.JSONDecodeError:
        require(False)
    require(len(letters) == 1 and letters[0]["line"] == "complete" and end == complete)


def show_once(m, root):
    c = setup(m, root)
    target = c.resolve("writer")
    c.inbox(target, "notice", lambda: None)
    output = io.StringIO()
    require(c.inbox.show(target, out=output) == 1)
    require(json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"] ==
            "Notifications:\nnotice")
    require(c.inbox.show(target, out=output) == 0)


def watch(m, root):
    c = setup(m, root)
    target = c.resolve("writer")
    require(c.inbox.watch(target, timeout=0) is False)
    c.inbox(target, "notice", lambda: None)
    require(c.inbox.watch(target, timeout=0) is True)


def exact_echo(m, root):
    def wrong(command, timeout):
        return 0, ("Queued message %s for thread %s." % (ITEM, A)).encode(), b""
    c = setup(m, root, wrong)
    require(c.send("reviewer", "wrong-echo", "demo").status == "OUTCOME_UNKNOWN")


def reserve_before_run(m, root):
    def recording(command, timeout):
        rows = list(m.records(root / "journal.jsonl"))
        require(bool(rows) and rows[-1]["status"] == "REQUESTED")
        require(command[-2] == "--message" and command[-3] == B)
        return exact(command, timeout)
    c = setup(m, root, recording)
    receipt = c.send("reviewer", "queue", "demo")
    require(receipt.status == "QUEUED" and receipt.queue_item_id == ITEM)
    require(receipt.as_dict()["read"] == "UNMEASURED")


def unknown_no_fallback(m, root):
    calls = []
    def missing(command, timeout):
        calls.append("queue")
        raise TimeoutError("No usable receipt")
    def other(target, line, requested):
        calls.append("urgent")
        requested()
        return m.Receipt("THREAD_ACCEPTED", "fake", target.identity)
    c = setup(m, root, missing, other)
    require(c.send("reviewer", "unknown", "demo").status == "OUTCOME_UNKNOWN")
    require(c.send("reviewer", "unknown", "demo", delivery="urgent").status == "SKIPPED")
    require(calls == ["queue"])


def claude_explicit(m, root):
    c = setup(m, root)
    for mode in ("queue", "urgent"):
        require(c.send("writer", mode, "demo", delivery=mode).status == "REFUSED")
    require(not c.inbox.paths(c.resolve("writer"))["jsonl"].exists())
    require(all(row["status"] != "REQUESTED" for row in m.records(root / "journal.jsonl")))


def urgent_absent(m, root):
    calls = []
    def queued(command, timeout):
        calls.append(command)
        return exact(command, timeout)
    c = setup(m, root, queued)
    require(c.send("reviewer", "urgent", "demo", delivery="urgent").status == "REFUSED")
    require(calls == [])


def callback_required(m, root):
    def unreserved(target, line, requested):
        return m.Receipt("THREAD_ACCEPTED", "fake", target.identity)
    c = setup(m, root, urgent=unreserved)
    require(c.send("reviewer", "unreserved", "demo", delivery="urgent").status ==
            "OUTCOME_UNKNOWN")
    dispatches, blocked_second = [], []
    def duplicate(target, line, requested):
        requested()
        dispatches.append("once")
        try:
            requested()
        except ValueError:
            blocked_second.append(True)
            raise
        return m.Receipt("THREAD_ACCEPTED", "fake", target.identity)
    c = setup(m, root / "duplicate", urgent=duplicate)
    require(c.send("reviewer", "twice", "demo", delivery="urgent").status ==
            "OUTCOME_UNKNOWN")
    require(dispatches == ["once"] and blocked_second == [True])
    statuses = [r["status"] for r in m.records(root / "duplicate" / "journal.jsonl")]
    require(statuses.count("REQUESTED") == 1)


def receiver_required(m, root):
    def mismatched(target, line, requested):
        requested()
        return m.Receipt("THREAD_ACCEPTED", "fake", A)
    c = setup(m, root, urgent=mismatched)
    require(c.send("reviewer", "mismatch", "demo", delivery="urgent").status ==
            "OUTCOME_UNKNOWN")


def generic_exception(m, root):
    for kind in (OSError, ValueError, TypeError, TimeoutError, RuntimeError):
        def failure(target, line, requested, kind=kind):
            raise kind("Adapter outcome cannot be established")
        c = setup(m, root / kind.__name__, urgent=failure)
        try:
            receipt = c.send("reviewer", "exception", "demo", delivery="urgent")
        except RuntimeError:
            require(False)
        require(receipt.status == "OUTCOME_UNKNOWN")
        require(c.send("reviewer", "exception", "demo", delivery="urgent").status ==
                "SKIPPED")
    def known_refusal(target, line, requested):
        raise m.Refused("Explicit application refusal")
    c = setup(m, root / "refusal", urgent=known_refusal)
    require(c.send("reviewer", "refusal", "demo", delivery="urgent").status == "REFUSED")


def mode_required(m, root):
    routes = [("writer", None, ("QUEUED", "THREAD_ACCEPTED")),
              ("reviewer", "queue", ("STORED", "THREAD_ACCEPTED")),
              ("reviewer", "urgent", ("STORED", "QUEUED"))]
    for recipient, mode, wrong_statuses in routes:
        for wrong in wrong_statuses:
            def incompatible(target, line, requested, wrong=wrong):
                requested()
                return m.Receipt(wrong, "Incompatible transport status", target.identity, ITEM)
            c = setup(m, root / (str(mode) + "-" + wrong), urgent=incompatible)
            c.inbox = incompatible
            c.queue = incompatible
            require(c.send(recipient, "mode", "demo", delivery=mode).status == "OUTCOME_UNKNOWN")


def final_journal_failure(m, root):
    calls = []
    def queued(command, timeout):
        calls.append(command)
        return exact(command, timeout)
    c = setup(m, root, queued)
    original = m.append_record
    def fail_final(path, row):
        if row.get("status") != "REQUESTED":
            raise OSError("Final journal write failed")
        original(path, row)
    m.append_record = fail_final
    try:
        receipt = c.send("reviewer", "journal", "demo")
        require(receipt.status == "OUTCOME_UNKNOWN" and "not recorded" in receipt.detail)
        require([r["status"] for r in m.records(root / "journal.jsonl")] == ["REQUESTED"])
        require(c.send("reviewer", "journal", "demo").status == "SKIPPED")
        require(len(calls) == 1)
    finally:
        m.append_record = original


def journal_invalid(m, root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "journal.jsonl").write_bytes(b'{"status":')
    c = setup(m, root, exact)
    try:
        c.send("reviewer", "new", "demo")
    except m.Refused:
        return
    require(False)


def batch_bound(m, root):
    c = setup(m, root)
    target = c.resolve("writer")
    for i in range(65):
        c.inbox(target, str(i), lambda: None)
    output = io.StringIO()
    require(c.inbox.show(target, out=output) == 64)
    require(c.inbox.show(target, out=output) == 1)
    require(c.inbox.show(target, out=output) == 0)


def config_example(m, root):
    data = json.loads((ROOT / "example.json").read_text(encoding="utf-8"))
    data["state_dir"] = "data"
    path = root / "example.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    c = m.Courier.from_config(path)
    require(c.state == root / "data" and c.queue is None and c.urgent is None)
    require(c.send("writer", "config", "demo").status == "STORED")



def short_prefix(m, root):
    try:
        result=setup(m, root).resolve("claude:" + A[:30])
    except m.Refused:
        require(False)
    require(result == m.Address("claude", A))


def byte_preflight(m, root):
    c = setup(m, root)
    try:
        c.send("writer", "\x01" * 256, "\x01" * 100, "\x01" * 1024)
    except m.Refused:
        pass
    else:
        require(False)
    require(not (root / "journal.jsonl").exists())
    require(c.send("writer", "ok", "demo", "x" * 1024).status == "STORED")


def record_cap(m, root):
    m.append_record(root / "ok", {"line": "x" * 8000})
    try:
        m.append_record(root / "bad", {"line": "x" * 8192})
    except m.Refused:
        return
    require(False)


def field_limits(m, root):
    c = setup(m, root)
    for fields in (("k" * 257, "s", ""), ("k", "s" * 101, ""), ("k", "s", "n" * 1025)):
        try:
            c.send("writer", *fields)
        except m.Refused:
            continue
        require(False)


def legacy_refused(m, root):
    c = setup(m, root, exact)
    m.append_record(root / "journal.jsonl", dict(client="codex", id=B, key="refused", status="REFUSED"))
    r = c.send("reviewer", "refused", "demo")
    require(r.status == "SKIPPED" and r.held_status == "REFUSED")


def dedup_namespace(m, root):
    c = setup(m, root, exact)
    require(c.send("writer", "shared", "demo").status == "STORED")
    require(c.send("other", "shared", "demo").status == "QUEUED")


def output_precedes_cursor(m, root):
    c = setup(m, root); target = c.resolve("writer")
    c.inbox(target, "notice", lambda: None)
    class BrokenOutput:
        def write(self, text): raise OSError("pipe closed")
        def flush(self): raise AssertionError("unreachable")
    try:
        c.inbox.show(target, out=BrokenOutput())
    except OSError:
        pass
    require(not c.inbox.paths(target)["offset"].exists())
    require(len(c.inbox.unread(target)[0]) == 1)


def inbox_replaced(m, root):
    c=setup(m, root); t=c.resolve("writer"); paths=c.inbox.paths(t)
    for line in ("old1", "old2"): c.inbox(t, line, lambda:None)
    c.inbox.show(t, out=io.StringIO()); cursor=paths["offset"].read_bytes()
    raw=paths["jsonl"].read_bytes()
    paths["jsonl"].rename(root/'old-inbox')
    paths["jsonl"].write_bytes(raw)
    c.inbox(t, 'third', lambda:None)
    try: c.inbox.show(t, out=io.StringIO())
    except m.Refused: pass
    else: require(False)
    require(paths["offset"].read_bytes()==cursor)


def consumed_prefix(m, root):
    c=setup(m, root); t=c.resolve("writer"); p=c.inbox.paths(t)["jsonl"]
    c.inbox(t,"old1",lambda:None); c.inbox.show(t,out=io.StringIO())
    before=p.stat().st_ino
    p.write_bytes(p.read_bytes().replace(b'old1',b'new1'))
    require(p.stat().st_ino==before)
    try: c.inbox.unread(t)
    except m.Refused: return
    require(False)


def legacy_cursor(m, root):
    c=setup(m,root); t=c.resolve("writer"); paths=c.inbox.paths(t)
    c.inbox(t,"first",lambda:None)
    paths["offset"].write_text(str(paths["jsonl"].stat().st_size))
    try: c.inbox.unread(t)
    except m.Refused: return
    require(False)


def cursor_pair(m, root):
    c=setup(m,root); t=c.resolve("writer"); paths=c.inbox.paths(t)
    c.inbox(t,"first",lambda:None); c.inbox.show(t,out=io.StringIO())
    identity=json.loads(paths["identity"].read_text());identity['offset']+=1
    paths["identity"].write_text(json.dumps(identity))
    try: c.inbox.unread(t)
    except m.Refused: return
    require(False)


def pre_dispatch_retry(m, root):
    c=setup(m,root)
    require(c.send("reviewer","retry","demo").status=="REFUSED")
    require(all(r['status']!='REQUESTED' for r in m.records(root/'journal.jsonl')))
    c.queue=m.CodexQueue(['example'],runner=exact)
    require(c.send("reviewer","retry","demo").status=="QUEUED")


def child_start_boundary(m, root):
    c=setup(m,root)
    c.queue=m.CodexQueue([str(root/'missing-executable-384729.exe')])
    r=c.send("reviewer","not-started","demo")
    require(r.status=='REFUSED' and 'not started' in r.detail)
    c.queue=m.CodexQueue(['example'],runner=exact)
    require(c.send("reviewer","not-started","demo").status=='QUEUED')
    observed=[]
    def after_dispatch(target,line,requested):
        requested();observed.append('dispatched');raise FileNotFoundError('adapter after dispatch')
    c=setup(m,root/'adapter',urgent=after_dispatch)
    require(c.send("reviewer","unknown","demo",delivery='urgent').status=='OUTCOME_UNKNOWN')
    require(c.send("reviewer","unknown","demo",delivery='urgent').status=='SKIPPED')
    require(observed==['dispatched'])
    # A proven no-start record must not clear an unrelated earlier unknown attempt.
    m.append_record(root/'adapter/journal.jsonl',dict(client='codex',id=B,key='unknown',attempt='new',status='REFUSED',dispatch='NOT_STARTED'))
    require(c.send("reviewer","unknown","demo",delivery='urgent').status=='SKIPPED')


def cli_config(m, root):
    root.mkdir(parents=True,exist_ok=True)
    cfg=root/'config.json'
    cfg.write_text(json.dumps(dict(state_dir=str(root/'state'),recipients={'writer':dict(client='claude',id=A),'reviewer':dict(client='codex',id=B)})),encoding='utf-8')
    return cfg


def cli(m,cfg,*args):
    env=dict(os.environ,PYTHONUTF8='0',PYTHONIOENCODING='cp1251')
    return subprocess.run([sys.executable,'-B','-X','utf8=0',m.__file__,'--config',str(cfg),*args],capture_output=True,env=env,timeout=10)


def skipped_cli(m, root):
    cfg=cli_config(m,root)
    require(cli(m,cfg,'send','writer','--key','ok','--sender','demo').returncode==0)
    good=cli(m,cfg,'send','writer','--key','ok','--sender','demo')
    require(good.returncode==0 and json.loads(good.stdout)['held_status']=='STORED')
    for status in ('REQUESTED','OUTCOME_UNKNOWN','REFUSED'):
        m.append_record(root/'state/journal.jsonl',dict(client='codex',id=B,key=status,status=status))
        bad=cli(m,cfg,'send','reviewer','--key',status,'--sender','demo')
        require(bad.returncode==1 and json.loads(bad.stdout)['held_status']==status)


def unicode_cli(m, root):
    cfg=cli_config(m,root)
    sent=cli(m,cfg,'send','writer','--key','unicode','--sender','demo','--note','\u2192 \U0001f642')
    require(sent.returncode==0)
    shown=cli(m,cfg,'show','writer')
    require(shown.returncode==0 and all(b<128 for b in shown.stdout))
    require('\u2192 \U0001f642' in json.loads(shown.stdout)['hookSpecificOutput']['additionalContext'])
    require(cli(m,cfg,'show','writer').stdout==b'')


def watch_cli(m, root):
    cfg=cli_config(m,root)
    quiet=cli(m,cfg,'watch','writer','--timeout','0')
    require(quiet.returncode==0 and quiet.stdout.strip()==b'QUIET')
    require(cli(m,cfg,'send','writer','--key','ready','--sender','demo').returncode==0)
    ready=cli(m,cfg,'watch','writer','--timeout','0')
    require(ready.returncode==0 and ready.stdout.strip()==b'MAIL_READY')


def bom_config(m, root):
    cfg=cli_config(m,root); cfg.write_bytes(b'\xef\xbb\xbf'+cfg.read_bytes())
    try:
        loaded=m.Courier.from_config(cfg)
    except (m.Refused,ValueError):
        require(False)
    require(loaded.resolve('writer')==m.Address('claude',A))


def windows_names(m, root):
    original_os,original_run,original_which=m.os,m.run_command,m.shutil.which
    calls=[]; reservations=[]
    def fake(command,timeout):calls.append(command);return exact(command,timeout)
    m.os=types.SimpleNamespace(name='nt'); m.run_command=fake; m.shutil.which=lambda name:name
    try:
        for name in ('example.cmd.','example.bat ','cmd.exe.','powershell.exe ','pwsh.','bash '):
            try:m.CodexQueue([name])(m.Address('codex',B),'notice',lambda:reservations.append(1))
            except m.Refused:continue
            require(False)
        require(not calls and not reservations)
    finally:m.os,m.run_command,m.shutil.which=original_os,original_run,original_which


def locked_operation(m, root, kind):
    c=setup(m,root); t=c.resolve('writer'); paths=c.inbox.paths(t)
    if kind=='offset':
        c.inbox(t,'first',lambda:None); c.inbox.show(t,out=io.StringIO()); c.inbox(t,'second',lambda:None)
    path=paths['offset'] if kind=='offset' else root/'append.jsonl'
    child=r"""import importlib.util,sys
p=sys.argv[1];spec=importlib.util.spec_from_file_location('child',p);m=importlib.util.module_from_spec(spec);sys.modules['child']=m;spec.loader.exec_module(m)
from pathlib import Path
with m.file_lock(Path(sys.argv[2])):
    stream=Path(sys.argv[2]).open('rb') if sys.argv[3]=='offset' else None
    try:
        print('LOCKED',flush=True)
        sys.stdin.readline()
    finally:
        if stream is not None:stream.close()
"""
    proc=subprocess.Popen([sys.executable,'-B','-c',child,m.__file__,str(path),kind],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    finished=threading.Event(); errors=[]; output=io.StringIO()
    def operation():
        try:
            if kind=='offset':c.inbox.show(t,out=output)
            else:m.append_record(path,dict(line='notice'))
        except Exception as e:errors.append(e)
        finally:finished.set()
    thread=threading.Thread(target=operation)
    try:
        require(proc.stdout.readline().strip()==b'LOCKED')
        thread.start();early=finished.wait(.15)
        require(not early)
    finally:
        proc.communicate(b'RELEASE\n',timeout=5)
        if thread.ident is not None:thread.join(5)
    require(finished.is_set() and not errors and proc.returncode==0)
    if kind=='offset':
        require(output.getvalue().count('hookSpecificOutput')==1)
        require(c.inbox.unread(t)[0]==[])
    else:require(json.loads(path.read_bytes())['line']=='notice')


def offset_lock(m,root):locked_operation(m,root,'offset')
def append_lock(m,root):locked_operation(m,root,'append')


def distinct_keys(m, root):
    child=r"""import importlib.util,sys,time
spec=importlib.util.spec_from_file_location('child',sys.argv[1]);m=importlib.util.module_from_spec(spec);sys.modules['child']=m;spec.loader.exec_module(m)
c=m.Courier(sys.argv[2],{'writer':m.Address('claude',sys.argv[3])})
print('READY',flush=True);sys.stdin.readline()
for i in range(12):
    r=c.send('writer',sys.argv[4]+'-'+str(i),'demo')
    assert r.status=='STORED',r
"""
    children=[subprocess.Popen([sys.executable,'-B','-c',child,m.__file__,str(root),A,str(w)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for w in range(2)]
    try:
        require(all(p.stdout.readline().strip()==b'READY' for p in children))
        for p in children:p.stdin.write(b'GO\n');p.stdin.flush()
        for p in children:
            out,err=p.communicate(timeout=10);require(p.returncode==0)
    finally:
        for p in children:
            if p.poll() is None:p.kill();p.wait()
    expected={str(w)+'-'+str(i) for w in range(2) for i in range(12)}
    rows=[json.loads(x) for x in (root/'journal.jsonl').read_bytes().splitlines()]
    require(len(rows)==48 and {r['key'] for r in rows if r['status']=='REQUESTED'}==expected and {r['key'] for r in rows if r['status']=='STORED'}==expected)
    letters=[json.loads(x) for x in (root/'inbox'/ (A+'.jsonl')).read_bytes().splitlines()]
    require(len(letters)==24 and {r['line'].split()[1] for r in letters}==expected)
    # The independent census distinguishes a real missing different key.
    require(expected-{r['line'].split()[1] for r in letters[:-1]}=={letters[-1]['line'].split()[1]})


def timeout_kills(m,root):
    original=m.subprocess.Popen
    calls=[]
    class Process:
        def wait(self,timeout):
            calls.append('wait')
            if len(calls)==1:raise subprocess.TimeoutExpired('fake',timeout)
            return 0
        def kill(self):calls.append('kill')
    m.subprocess.Popen=lambda *a,**kw:Process()
    try:
        try:m.run_command(['fake'],.01)
        except TimeoutError:pass
        else:require(False)
        require(calls==['wait','kill','wait'])
    finally:m.subprocess.Popen=original


def negative_offset(m,root):
    c=setup(m,root);t=c.resolve('writer');p=c.inbox.paths(t)['offset'];p.parent.mkdir(parents=True);p.write_text('-1')
    try:c.inbox.unread(t)
    except m.Refused as e:require('Invalid inbox offset' in str(e));return
    require(False)


def deleted_inbox(m,root):
    c=setup(m,root);t=c.resolve('writer');c.inbox(t,'notice',lambda:None);c.inbox.show(t,out=io.StringIO());c.inbox.paths(t)['jsonl'].unlink()
    try:c.inbox.unread(t)
    except m.Refused:return
    require(False)



# Additional independently named functions; require() attributes the FUNCTION, not each clause.
def _reply(m, payload, code=0):
    called=[]
    def runner(command, timeout):
        called.append(command)
        return code, payload, b''
    receipt=m.CodexQueue(['fixture'],runner=runner)(m.Address('codex',B),'notice',lambda:None)
    require(len(called)==1)
    return receipt

def queue_nonzero(m,root):
    payload=('Queued message %s for thread %s.'%(ITEM,B)).encode()
    for code in (1,-9):require(_reply(m,payload,code).status=='OUTCOME_UNKNOWN')

def queue_extra(m,root):
    payload=('Queued message %s for thread %s.'%(ITEM,B)).encode()
    require(_reply(m,b' \n'+payload+b'\r\n\t').status=='QUEUED')
    for bad in (b'extra '+payload,payload+b' extra'):
        require(_reply(m,bad).status=='OUTCOME_UNKNOWN')

def queue_wording(m,root):
    require(_reply(m,('Enqueued message %s for thread %s.'%(ITEM,B)).encode()).status=='OUTCOME_UNKNOWN')

def queue_item(m,root):
    r=_reply(m,('Queued message  for thread %s.'%B).encode())
    require(r.status=='OUTCOME_UNKNOWN' and r.queue_item_id is None)

def queue_diagnostic(m,root):
    payload=b'changed '+b'x'*300
    r=_reply(m,payload,7)
    require(r.status=='OUTCOME_UNKNOWN' and 'exit 7' in r.detail)
    require(repr(payload.decode()[:200]) in r.detail and len(r.detail)<350)

def final_refused(m,root):
    calls=[]
    def refused(target,line,requested):
        requested();calls.append('attempt');raise m.Refused('Explicit receiver refusal')
    c=setup(m,root,urgent=refused);original=m.append_record
    def append(path,row):
        if row.get('status') not in ('RESERVED','REQUESTED'):raise OSError('Final journal write failed')
        original(path,row)
    m.append_record=append
    try:
        r=c.send('reviewer','refused-final','demo',delivery='urgent')
        require(r.status=='REFUSED' and 'not recorded' in r.detail)
        # No separate mutation targets this assertion; attribution is per function.
        require([x['status'] for x in m.records(root/'journal.jsonl')][-1:]==['REQUESTED'])
        require(c.send('reviewer','refused-final','demo',delivery='urgent').status=='SKIPPED')
        require(calls==['attempt'])
    finally:m.append_record=original

def _long_consumed(m,root):
    c=setup(m,root);target=c.resolve('writer');paths=c.inbox.paths(target)
    for n in range(2):c.inbox(target,str(n)+'x'*3000,lambda:None)
    require(c.inbox.show(target,out=io.StringIO())==2)
    require(int(paths['offset'].read_text())>4096)
    return c,target,paths

def truncated_saved_cursor(m,root):
    c,t,p=_long_consumed(m,root);raw=p['jsonl'].read_bytes();offset=p['offset'].read_bytes()
    require(len(raw)>4500)
    with p['jsonl'].open('r+b') as f:f.truncate(4500)
    require(p['jsonl'].read_bytes()[:4096]==raw[:4096])
    try:c.inbox.unread(t)
    except m.Refused as e:require('Inbox truncated' in str(e))
    else:require(False)
    require(p['offset'].read_bytes()==offset)

def large_cursor_growth(m,root):
    c,t,p=_long_consumed(m,root)
    c.inbox(t,'new after large cursor',lambda:None)
    try:
        require(c.inbox.show(t,out=io.StringIO())==1)
        require(c.inbox.unread(t)[0]==[])
    except m.Refused:require(False)

def lock_deadline(m,root):
    import errno
    if m.os.name=='nt':
        import msvcrt as backend
        attr='locking'
    else:
        import fcntl as backend
        attr='flock'
    old_lock=getattr(backend,attr);old_clock=m.time.monotonic;old_sleep=m.time.sleep
    ticks=iter((0.0,29.0,31.0));attempts=[];sleeps=[]
    def contested(*args):
        attempts.append(True)
        if len(attempts)<=2:raise OSError(errno.EAGAIN,'synthetic contention')
    setattr(backend,attr,contested);m.time.monotonic=lambda:next(ticks);m.time.sleep=sleeps.append
    try:
        try:
            with m.file_lock(root/'deadline'):require(False)
        except m.Refused as e:require('Local state lock timed out' in str(e))
        require(len(attempts)==2 and sleeps==[0.01])
    finally:
        setattr(backend,attr,old_lock);m.time.monotonic=old_clock;m.time.sleep=old_sleep

def queue_default_timeout(m,root):require(m.CodexQueue(['fixture']).timeout==30)

def queue_timeout_120(m,root):
    observed=[]
    def runner(command,timeout):observed.append(timeout);return exact(command,timeout)
    try:r=m.CodexQueue(['fixture'],timeout=120,runner=runner)(m.Address('codex',B),'notice',lambda:None)
    except m.Refused:require(False)
    require(r.status=='QUEUED' and observed==[120])

def flush_before_cursor(m,root):
    c=setup(m,root);t=c.resolve('writer');c.inbox(t,'notice',lambda:None);writes=[];flushes=[]
    class BrokenFlush:
        def write(self,text):writes.append(text);return len(text)
        def flush(self):flushes.append(True);raise OSError('flush failed')
    try:c.inbox.show(t,out=BrokenFlush())
    except OSError as e:require('flush failed' in str(e))
    else:require(False)
    p=c.inbox.paths(t)
    require(len(writes)==1 and flushes==[True] and not p['offset'].exists() and not p['identity'].exists())
    require(len(c.inbox.unread(t)[0])==1)
    require(c.inbox.show(t,out=io.StringIO())==1 and p['offset'].exists())

def _main_output(m,cfg,key,out):
    keep=(m.sys.argv,m.sys.stdout,m.sys.stderr);err=io.StringIO()
    m.sys.argv=['courier.py','--config',str(cfg),'send','writer','--key',key,'--sender','demo']
    m.sys.stdout=out;m.sys.stderr=err
    try:
        try:code=m.main()
        except Exception:require(False)
        return code,err.getvalue()
    finally:m.sys.argv,m.sys.stdout,m.sys.stderr=keep

class _FailedReceiptOutput:
    def __init__(self,mode):self.mode=mode;self.writes=[];self.flushes=0
    def write(self,text):
        self.writes.append(text)
        if self.mode=='write':raise OSError('receipt write failed')
        return len(text)
    def flush(self):self.flushes+=1;raise OSError('receipt flush failed')

def _output_failure(m,root,mode):
    cfg=cli_config(m,root);writer=_FailedReceiptOutput(mode)
    code,err=_main_output(m,cfg,'output-failure',writer)
    require(code==1)
    report=json.loads(err)
    require(isinstance(report,dict) and report.get('status')=='RECEIPT_OUTPUT_FAILED')
    received=report.get('delivery_receipt')
    require(isinstance(received,dict) and received.get('status')=='STORED')
    require(bool(writer.writes) and (mode=='write' or writer.flushes==1))
    c=m.Courier.from_config(cfg);before=(root/'state/journal.jsonl').read_bytes()
    require([x['status'] for x in m.records(root/'state/journal.jsonl')]==['REQUESTED','STORED'])
    r=c.send('writer','output-failure','demo');require(r.status=='SKIPPED' and r.held_status=='STORED')
    require((root/'state/journal.jsonl').read_bytes()==before)
    require(len(c.inbox.unread(c.resolve('writer'))[0])==1)

def cli_write_failure(m,root):
    _output_failure(m,root,'write')
    code,err=_main_output(m,root/'missing-config.json','not-sent',io.StringIO())
    require(code==1 and json.loads(err)['status']=='REFUSED' and 'delivery_receipt' not in json.loads(err))

def cli_flush_failure(m,root):_output_failure(m,root,'flush')

def held_mode_precedence(m,root):
    c=setup(m,root);require(c.send('writer','held','demo').status=='STORED')
    before=(root/'journal.jsonl').read_bytes()
    for mode in ('queue','urgent','other'):
        require(c.send('writer','held','demo',delivery=mode).status=='REFUSED')
    require(c.send('writer','held','demo').status=='SKIPPED')
    require((root/'journal.jsonl').read_bytes()==before)

def _short_write(m,root):
    c=setup(m,root);t=c.resolve('writer');require(c.send('writer','complete','demo').status=='STORED')
    p=c.inbox.paths(t);before=p['jsonl'].read_bytes();real=m.os.write;fragments=[]
    def short(fd,data):
        if not fragments and 'line' in json.loads(data):
            fragment=data[:len(data)//2];count=real(fd,fragment)
            require(count==len(fragment));fragments.append(fragment);return count
        return real(fd,data)
    m.os.write=short
    try:r=c.send('writer','partial','demo')
    finally:m.os.write=real
    require(len(fragments)==1)
    return c,t,p,before,fragments[0],r

def short_write_count(m,root):
    c,t,p,before,fragment,r=_short_write(m,root)
    require(r.status=='OUTCOME_UNKNOWN' and p['jsonl'].read_bytes()==before+fragment)

def incomplete_tail_blocks_append(m,root):
    c,t,p,before,fragment,r=_short_write(m,root)
    require(r.status=='OUTCOME_UNKNOWN')
    saved=p['jsonl'].read_bytes();second=c.send('writer','different-key','demo')
    require(second.status=='REFUSED' and 'Incomplete append tail' in second.detail)
    require(p['jsonl'].read_bytes()==saved==before+fragment)
    out=io.StringIO();require(c.inbox.show(t,out=out)==1)
    require('complete' in json.loads(out.getvalue())['hookSpecificOutput']['additionalContext'])
    require(int(p['offset'].read_text())==len(before) and c.inbox.show(t,out=io.StringIO())==0)
    require(p['jsonl'].read_bytes()==saved)

def _untrusted_not_started(m,root,kind,restore=False):
    calls=[]
    def refusal(target,line,requested):
        requested();calls.append(True)
        raise m.NotStarted('untrusted helper not started') from OSError('helper creation')
    c=setup(m,root)
    if kind=='urgent':c.urgent=refusal;mode='urgent'
    elif kind=='custom':
        def runner(command,timeout):
            calls.append(True)
            if restore:c.queue.runner=m.run_command
            raise m.NotStarted('untrusted runner not started') from OSError('helper creation')
        c.queue=m.CodexQueue(['fixture'],runner=runner);mode='queue'
    elif kind=='subclass':
        class Queue(m.CodexQueue):
            def __call__(self,target,line,requested):return refusal(target,line,requested)
        c.queue=Queue(['fixture']);mode='queue'
    else:require(False)
    r=c.send('reviewer','untrusted','demo',delivery=mode)
    require(r.status=='REFUSED')
    require(list(m.records(root/'journal.jsonl'))[-1]['dispatch']=='POSSIBLE')
    require(c.send('reviewer','untrusted','demo',delivery=mode).status=='SKIPPED' and calls==[True])

def urgent_not_started(m,root):_untrusted_not_started(m,root,'urgent')
def custom_not_started(m,root):
    _untrusted_not_started(m,root/'unchanged','custom')
    _untrusted_not_started(m,root/'restored','custom',restore=True)
def subclass_not_started(m,root):_untrusted_not_started(m,root,'subclass')

def default_requires_cause(m,root):
    c=setup(m,root);c.queue=m.CodexQueue([str(root/'fixture.exe')]);calls=[];original=m.subprocess.Popen
    def wrong(*args,**kwargs):calls.append(True);raise m.NotStarted('no creation proof')
    m.subprocess.Popen=wrong
    try:
        require(c.send('reviewer','no-cause','demo').status=='REFUSED')
        require(list(m.records(root/'journal.jsonl'))[-1]['dispatch']=='POSSIBLE')
        require(c.send('reviewer','no-cause','demo').status=='SKIPPED' and calls==[True])
    finally:m.subprocess.Popen=original

def nonempty_preflight(m,root):
    c=setup(m,root,exact)
    for key,sender in (('','demo'),('valid','')):
        try:c.send('reviewer',key,sender)
        except m.Refused as e:require('must not be empty' in str(e))
        else:require(False)
    require(not (root/'journal.jsonl').exists())
    require(c.send('reviewer','valid','demo','').status=='QUEUED')

def nul_preflight(m,root):
    c=setup(m,root,exact)
    for fields in (('k\x00','s',''),('k','s\x00',''),('k','s','n\x00')):
        try:c.send('reviewer',*fields)
        except m.Refused as e:require('NUL' in str(e))
        else:require(False)
    require(not (root/'journal.jsonl').exists())
    require(c.send('reviewer','valid','demo','Unicode \u2192').status=='QUEUED')

def _nonfinite_watch(m,root,field):
    c=setup(m,root);t=c.resolve('writer')
    class LoopEntered(Exception):pass
    def unread(target):raise LoopEntered('validation was not before the loop')
    c.inbox.unread=unread
    for value in (float('nan'),float('inf'),float('-inf')):
        kwargs={field:value}
        try:c.inbox.watch(t,**kwargs)
        except m.Refused as e:require('Invalid watch interval' in str(e))
        except LoopEntered:require(False)
        else:require(False)
    c=setup(m,root/'valid')
    require(c.inbox.watch(c.resolve('writer'),timeout=0,poll=0.5) is False)

def finite_watch_timeout(m,root):_nonfinite_watch(m,root,'timeout')
def finite_watch_poll(m,root):_nonfinite_watch(m,root,'poll')

def config_cwd_binding(m,root):
    a=root/'A';b=root/'B';a.mkdir();b.mkdir();keep=Path.cwd()
    (a/'config.json').write_text(json.dumps(dict(state_dir='state',recipients={'writer':dict(client='claude',id=A)})),encoding='utf-8')
    try:
        os.chdir(a);c=m.Courier.from_config(Path('config.json'))
        require(c.send('writer','bound','demo').status=='STORED')
        first=(a/'state/journal.jsonl').read_bytes()
        os.chdir(b);r=c.send('writer','bound','demo')
        require(r.status=='SKIPPED' and r.held_status=='STORED')
        require(c.state==a/'state' and c.state.is_absolute() and not (b/'state').exists())
        require((a/'state/journal.jsonl').read_bytes()==first)
    finally:os.chdir(keep)


def queue_nul_boundary(m,root):
    calls=[];requests=[]
    def runner(command,timeout):calls.append(command);return exact(command,timeout)
    for command in (['fixture\x00'],['fixture','arg\x00']):
        try:m.CodexQueue(command,runner=runner)
        except m.Refused as e:require('NUL-free' in str(e))
        else:require(False)
    queue=m.CodexQueue(['fixture'],runner=runner)
    try:queue(m.Address('codex',B),'embedded\x00notice',lambda:requests.append(True))
    except m.Refused as e:require('NUL-free' in str(e))
    else:require(False)
    require(not calls and not requests)
    require(queue(m.Address('codex',B),'Unicode \u2192',lambda:requests.append(True)).status=='QUEUED')
    require(len(calls)==1 and requests==[True])


def huge_finite_poll(m,root):
    c=setup(m,root/'api');target=c.resolve('writer')
    try:c.inbox.watch(target,poll=1e308)
    except m.Refused as error:require('Invalid watch interval' in str(error))
    except OverflowError:require(False)
    else:require(False)
    require(c.inbox.watch(target,timeout=0,poll=0.5) is False)
    cfg=cli_config(m,root/'cli')
    failed=cli(m,cfg,'watch','writer','--poll','1e308')
    require(failed.returncode==1 and failed.stdout==b'')
    report=json.loads(failed.stderr)
    require(report['status']=='REFUSED' and 'Invalid watch interval' in report['detail'])
    require(b'Traceback' not in failed.stderr)
    quiet=cli(m,cfg,'watch','writer','--timeout','0','--poll','0.5')
    require(quiet.returncode==0 and quiet.stdout.strip()==b'QUIET')


def custom_refused_without_callback(m, root):
    for kind in ("queue", "urgent", "queue-subclass", "inbox-subclass"):
        state = root / kind
        effects = []
        def refused(target, line, requested):
            effects.append("synthetic dispatch")
            raise m.Refused("refusal after synthetic dispatch")
        c = setup(m, state)
        recipient, mode = "reviewer", "queue"
        if kind == "queue":
            c.queue = refused
        elif kind == "urgent":
            c.urgent, mode = refused, "urgent"
        elif kind == "queue-subclass":
            class Queue(m.CodexQueue):
                def __call__(self, target, line, requested):
                    return refused(target, line, requested)
            c.queue = Queue(["fixture"])
        else:
            class FileInbox(m.Inbox):
                def __call__(self, target, line, requested):
                    return refused(target, line, requested)
            c.inbox = FileInbox(state)
            recipient, mode = "writer", None
        first = c.send(recipient, "same-key", "demo", delivery=mode)
        require(first.status == "REFUSED" and effects == ["synthetic dispatch"])
        rows = list(m.records(state / "journal.jsonl"))
        require(rows and rows[-1]["dispatch"] == "POSSIBLE")
        require(c.send(recipient, "same-key", "demo", delivery=mode).status == "SKIPPED")
        require(effects == ["synthetic dispatch"])


def default_preflight_refusal_retry(m, root):
    c = setup(m, root)
    c.queue = m.CodexQueue(["sh"])
    first = c.send("reviewer", "preflight", "demo")
    require(first.status == "REFUSED" and "Shell interpreters" in first.detail)
    rows = list(m.records(root / "journal.jsonl"))
    require(rows and rows[-1]["dispatch"] == "NOT_STARTED")
    require(all(row["status"] != "REQUESTED" for row in rows))
    c.queue = m.CodexQueue(["fixture"], runner=exact)
    require(c.send("reviewer", "preflight", "demo").status == "QUEUED")


CHECKS = [
    ("REGISTRY_RESOLVES_CLIENT_ID", resolves),
    ("COMMAND_WRAPPERS_REFUSED_BEFORE_REQUEST", command_boundary),
    ("LETTER_PRECEDES_RING", append_before_ring),
    ("SELF_USES_CLIENT_AND_FULL_ID", self_scope),
    ("AMBIGUOUS_PREFIX_REFUSED", ambiguity),
    ("REQUESTED_IS_NOT_REPLAYED", requested_blocks),
    ("MESSAGE_IS_LITERAL_DATA", literal_message),
    ("PARTIAL_RECORD_WAITS", partial_waits),
    ("SHOW_ADVANCES_ONCE", show_once),
    ("WATCH_UNREAD_AND_QUIET", watch),
    ("QUEUE_ECHO_IS_RECEIVER_BOUND", exact_echo),
    ("QUEUE_REQUEST_PRECEDES_RUN", reserve_before_run),
    ("UNKNOWN_HAS_NO_FALLBACK", unknown_no_fallback),
    ("CLAUDE_EXPLICIT_MODE_REFUSED", claude_explicit),
    ("URGENT_HAS_NO_BUNDLED_ROUTE", urgent_absent),
    ("ADAPTER_RESERVATION_REQUIRED", callback_required),
    ("ADAPTER_RECEIVER_REQUIRED", receiver_required),
    ("ADAPTER_EXCEPTION_IS_UNKNOWN", generic_exception),
    ("ADAPTER_MODE_MATCHES_TRANSPORT", mode_required),
    ("FINAL_JOURNAL_FAILURE_IS_UNKNOWN", final_journal_failure),
    ("INVALID_JOURNAL_NOT_EMPTY", journal_invalid),
    ("SHOW_BATCH_IS_BOUNDED", batch_bound),
    ("CLEAN_CONFIG_IS_SELF_CONTAINED", config_example),
    ('UNIQUE_SHORT_PREFIX_RESOLVES', short_prefix),
    ('RECORD_BYTES_CAPPED', record_cap),
    ('BYTE_CAP_PRECEDES_RESERVATION', byte_preflight),
    ('FIELD_CHAR_LIMITS', field_limits),
    ('LEGACY_REFUSED_IS_HELD', legacy_refused),
    ('DEDUP_CLIENT_NAMESPACE', dedup_namespace),
    ('OUTPUT_PRECEDES_CURSOR', output_precedes_cursor),
    ('INBOX_REPLACEMENT_IS_BOUND', inbox_replaced),
    ('CONSUMED_PREFIX_IS_BOUND', consumed_prefix),
    ('LEGACY_CURSOR_NOT_AUTOBOUND', legacy_cursor),
    ('CURSOR_PAIR_MISMATCH_REFUSED', cursor_pair),
    ('PRE_DISPATCH_REFUSAL_CAN_RETRY', pre_dispatch_retry),
    ('CHILD_START_IS_KNOWN_BOUNDARY', child_start_boundary),
    ('SKIPPED_CLI_CODES_NAME_OUTCOME', skipped_cli),
    ('UNICODE_PIPE_IS_ASCII_JSON', unicode_cli),
    ('WATCH_CLI_WORDS', watch_cli),
    ('BOM_CONFIG_ACCEPTED', bom_config),
    ('WINDOWS_NAME_NORMALISATION', windows_names),
    ('OFFSET_ACCESS_SERIALISED', offset_lock),
    ('APPEND_ACCESS_SERIALISED', append_lock),
    ('DISTINCT_KEYS_RETAIN_ALL_ROWS', distinct_keys),
    ('TIMED_OUT_CHILD_KILLED', timeout_kills),
    ('NEGATIVE_OFFSET_TYPED_DIAGNOSTIC', negative_offset),
    ('DELETED_INBOX_REFUSED', deleted_inbox),
]

CHECKS.extend([
    ("QUEUE_NONZERO_NOT_ACKNOWLEDGED", queue_nonzero),
    ("QUEUE_SURROUNDING_TEXT_NOT_ACKNOWLEDGED", queue_extra),
    ("QUEUE_WORDING_NOT_ACKNOWLEDGED", queue_wording),
    ("QUEUE_ITEM_ID_REQUIRED", queue_item),
    ("QUEUE_UNKNOWN_DIAGNOSTIC_BOUNDED", queue_diagnostic),
    ("FINAL_JOURNAL_REFUSAL_RETAINED", final_refused),
    ("SAVED_CURSOR_TRUNCATION_REFUSED", truncated_saved_cursor),
    ("LARGE_CURSOR_GROWTH_READABLE", large_cursor_growth),
    ("LOCK_DEADLINE_30_SECONDS", lock_deadline),
    ("QUEUE_DEFAULT_TIMEOUT_30_SECONDS", queue_default_timeout),
    ("QUEUE_CONFIGURED_TIMEOUT_120_ALLOWED", queue_timeout_120),
    ("SHOW_FLUSH_PRECEDES_CURSOR", flush_before_cursor),
    ("CLI_WRITE_FAILURE_RETAINS_STORED", cli_write_failure),
    ("CLI_FLUSH_FAILURE_RETAINS_STORED", cli_flush_failure),
    ("CLAUDE_HELD_MODE_PREFLIGHT", held_mode_precedence),
    ("SHORT_WRITE_COUNT_NOT_ACKNOWLEDGED", short_write_count),
    ("INCOMPLETE_TAIL_BLOCKS_NEXT_APPEND", incomplete_tail_blocks_append),
    ("URGENT_NOT_STARTED_PRESERVES_HOLD", urgent_not_started),
    ("CUSTOM_QUEUE_NOT_STARTED_PRESERVES_HOLD", custom_not_started),
    ("QUEUE_SUBCLASS_NOT_STARTED_PRESERVES_HOLD", subclass_not_started),
    ("DEFAULT_QUEUE_NOT_STARTED_REQUIRES_CAUSE", default_requires_cause),
    ("NONEMPTY_FIELDS_PREFLIGHT", nonempty_preflight),
    ("NUL_FIELDS_PREFLIGHT", nul_preflight),
    ("WATCH_TIMEOUT_REQUIRES_FINITE", finite_watch_timeout),
    ("WATCH_POLL_REQUIRES_FINITE", finite_watch_poll),
    ("RELATIVE_CONFIG_STATE_STAYS_BOUND", config_cwd_binding),
    ("QUEUE_NUL_BOUNDARY_PRECEDES_REQUEST", queue_nul_boundary),
    ("HUGE_FINITE_POLL_TYPED_REFUSAL", huge_finite_poll),
])

CHECKS.extend([
    ('CUSTOM_REFUSED_WITHOUT_CALLBACK_HELD', custom_refused_without_callback),
    ('DEFAULT_PREFLIGHT_REFUSAL_RETRY', default_preflight_refusal_retry),
])


def _held_synthetic(m, state, adapter=None, runner=None, expected="OUTCOME_UNKNOWN"):
    effects = []
    def call(target, line, requested):
        effects.append("dispatch")
        return adapter(target, line, requested)
    def queue(argv, timeout):
        effects.append("dispatch")
        return runner(argv, timeout)
    c = setup(m, state, runner=queue if runner is not None else None,
              urgent=call if adapter is not None else None)
    mode = "queue" if runner is not None else "urgent"
    try:
        first = c.send("reviewer", "same-key", "demo", delivery=mode)
        rows = list(m.records(state / "journal.jsonl"))
        second = c.send("reviewer", "same-key", "demo", delivery=mode)
    except Exception:
        require(False)
    require(type(first) is m.Receipt and first.status == expected)
    require(rows and rows[-1].get("dispatch") == "POSSIBLE")
    require(rows[-1].get("status") == expected)
    require(second.status == "SKIPPED" and second.held_status == expected)
    require(effects == ["dispatch"])
    return first

def queue_plain_container(m, root):
    methods = []
    class Foreign:
        def __iter__(self):
            methods.append("iter")
            return iter(exact(["queue", "--thread", B, "--message", "literal"], 30))
        def __len__(self):
            methods.append("len")
            return 3
    _held_synthetic(m, root/"foreign", runner=lambda a,t: Foreign())
    require(methods == [])
    class ForeignTuple(tuple):
        def __len__(self):
            methods.append("tuple len")
            return tuple.__len__(self)
        def __iter__(self):
            methods.append("tuple iter")
            return tuple.__iter__(self)
    class ForeignList(list):
        def __len__(self):
            methods.append("list len")
            return list.__len__(self)
        def __iter__(self):
            methods.append("list iter")
            return list.__iter__(self)
    for index, container in enumerate((ForeignTuple, ForeignList)):
        methods.clear()
        _held_synthetic(m, root/("subclass"+str(index)),
                        runner=lambda a,t: container(exact(a,t)))
        require(methods == [])
    for index, values in enumerate(((0, b""), [0, b""])):
        _held_synthetic(m, root/str(index), runner=lambda a,t: values)
    for index, container in enumerate((tuple, list)):
        calls = []
        def runner(argv, timeout):
            calls.append(True)
            return container(exact(argv, timeout))
        c = setup(m, root/("valid"+str(index)), runner)
        require(c.send("reviewer", "valid", "demo").status == "QUEUED")
        require(c.send("reviewer", "valid", "demo").status == "SKIPPED")
        require(calls == [True])

def queue_plain_fields(m, root):
    methods = []
    def deferred(name):
        methods.append(name)
        raise m.NotStarted("foreign deferred operation") from OSError("fixture")
    class Length(bytes):
        def __len__(self): return deferred("len")
    class Addition(bytes):
        def __add__(self, other): return deferred("add")
    class Decode(bytes):
        def __add__(self, other): return self
        def decode(self, *args, **kwargs): return deferred("decode")
    class Code(int):
        def __eq__(self, other): return deferred("code")
    ack = ("Queued message %s for thread %s." % (ITEM, B)).encode()
    for index, values in enumerate(((0, Length(ack), b""), (0, Addition(ack), b""),
                                     (0, Decode(ack), b""), (Code(0), ack, b""),
                                     (0, ack, Length(b"")))):
        methods.clear()
        _held_synthetic(m, root/str(index), runner=lambda a,t: values)
        require(methods == [])

def queue_exception_formatting_inert(m, root):
    for broken in (False, True):
        text_calls = []
        class Foreign(m.NotStarted):
            def __str__(self):
                text_calls.append(True)
                if broken:
                    raise m.NotStarted("deferred format") from OSError("fixture")
                return "foreign detail"
        def runner(argv, timeout):
            raise Foreign("local looking text") from OSError("fixture")
        _held_synthetic(m, root/str(broken), runner=runner, expected="REFUSED")
        require(text_calls == [])

def exception_detail_is_inert(m, root):
    cases = [(kind, broken) for kind in ("refused", "generic", "argument")
             for broken in (False, True)]
    cases += [("str-argument", False), ("many-arguments", False)]
    for index, (kind, broken) in enumerate(cases):
        text_calls = []
        def foreign_text(self):
            text_calls.append("foreign str effect")
            if broken: raise AttributeError("unprintable fixture")
            return "foreign detail"
        base = ValueError if kind == "generic" else m.Refused
        class ForeignError(base):
            __str__ = foreign_text
            def __getattribute__(self, name):
                if name == "args":
                    text_calls.append("foreign args")
                    raise AttributeError("foreign args access")
                return object.__getattribute__(self, name)
        class Argument:
            __str__ = foreign_text
        class TextArgument(str):
            __str__ = foreign_text
            def __getitem__(self, key):
                text_calls.append("foreign slicing")
                return str.__getitem__(self, key)
        if kind == "argument": error = m.Refused(Argument())
        elif kind == "str-argument": error = m.Refused(TextArgument("untrusted argument"))
        elif kind == "many-arguments": error = m.Refused("untrusted argument", "second")
        else: error = ForeignError("untrusted argument")
        def adapter(target, line, requested): raise error
        expected = "OUTCOME_UNKNOWN" if kind == "generic" else "REFUSED"
        first = _held_synthetic(m, root/str(index), adapter=adapter, expected=expected)
        require(type(first.detail) is str)
        require(first.detail not in ("untrusted argument", "foreign detail"))
        require(text_calls == [])
    for error in (m.Refused("plain"), m.NotStarted("plain"), ValueError("plain")):
        require(m.failure_detail(error) == "plain")

def receipt_subclass_is_inert(m, root):
    accesses = []
    class ForeignReceipt(m.Receipt):
        def __getattribute__(self, name):
            accesses.append(name)
            return object.__getattribute__(self, name)
        def as_dict(self):
            accesses.append("foreign as_dict effect")
            raise AttributeError("unserializable receipt")
    offered = ForeignReceipt("REFUSED", "untrusted receipt")
    accesses.clear()
    _held_synthetic(m, root, adapter=lambda t,l,r: offered)
    require(accesses == [])

def receipt_fields_are_durable(m, root):
    accesses = []
    class ForeignText(str):
        def __hash__(self):
            accesses.append("hash")
            return str.__hash__(self)
        def __eq__(self, other):
            accesses.append("eq")
            return str.__eq__(self, other)
        def __str__(self):
            accesses.append("str")
            return str.__str__(self)
    cases = [
        ("valid", {"detail": "literal"}, "REFUSED"),
        ("status", {"status": ForeignText("REFUSED")}, "OUTCOME_UNKNOWN"),
        ("detail-type", {"detail": None}, "OUTCOME_UNKNOWN"),
        ("detail-subclass", {"detail": ForeignText("untrusted receipt")}, "OUTCOME_UNKNOWN"),
        ("recipient", {"recipient_id": False}, "OUTCOME_UNKNOWN"),
        ("item", {"queue_item_id": 7}, "OUTCOME_UNKNOWN"),
        ("held", {"held_status": []}, "OUTCOME_UNKNOWN"),
        ("oversized", {"detail": "x" * 8193}, "OUTCOME_UNKNOWN"),
        ("unencodable", {"detail": "\ud800"}, "OUTCOME_UNKNOWN"),
    ]
    for name, changes, expected in cases:
        fields = dict(status="REFUSED", detail="untrusted receipt")
        fields.update(changes)
        offered = m.Receipt(**fields)
        accesses.clear()
        first = _held_synthetic(m, root/name, adapter=lambda t,l,r: offered, expected=expected)
        require(accesses == [])
        if name == "valid":
            require(first is not offered and first.detail == "literal")

CHECKS.extend([
    ("QUEUE_RESULT_CONTAINER_PLAIN", queue_plain_container),
    ("QUEUE_RESULT_FIELDS_PLAIN", queue_plain_fields),
    ("QUEUE_EXCEPTION_FORMATTING_INERT", queue_exception_formatting_inert),
    ("EXCEPTION_DETAIL_IS_INERT", exception_detail_is_inert),
    ("RECEIPT_SUBCLASS_IS_INERT", receipt_subclass_is_inert),
    ("RECEIPT_FIELDS_ARE_DURABLE", receipt_fields_are_durable),
])


def exception_detail_utf8_durable(m, root):
    for index, constructor in enumerate((m.Refused, m.NotStarted, ValueError)):
        for suffix, detail in (("high", "\ud800"), ("low", "\udfff")):
            def adapter(target, line, requested):
                raise constructor(detail)
            expected = "OUTCOME_UNKNOWN" if constructor is ValueError else "REFUSED"
            first = _held_synthetic(m, root/(str(index)+suffix), adapter=adapter, expected=expected)
            require(type(first.detail) is str)
            try:
                first.detail.encode("utf-8")
            except UnicodeEncodeError:
                require(False)
    valid = "Unicode \u2192 \U0001f642"
    def honest(target, line, requested):
        raise m.Refused(valid)
    first = _held_synthetic(m, root/"valid", adapter=honest, expected="REFUSED")
    require(first.detail == valid)

CHECKS.append(("EXCEPTION_DETAIL_UTF8_DURABLE", exception_detail_utf8_durable))

REGRESSIONS = [
    ('ordinary adapter exception escapes', 'except Exception as error:\n            receipt = Receipt("OUTCOME_UNKNOWN"', 'except (OSError, ValueError, TypeError, TimeoutError, subprocess.SubprocessError) as error:\n            receipt = Receipt("OUTCOME_UNKNOWN"', 'ADAPTER_EXCEPTION_IS_UNKNOWN'),
    ('second reservation allowed', 'if requested_count:', 'if False:', 'ADAPTER_RESERVATION_REQUIRED'),
    ('resolver loses client identity', 'return hits.pop()', 'return Address("claude", hits.pop().identity)', 'REGISTRY_RESOLVES_CLIENT_ID'),
    ('ambiguous address accepted', 'if len(hits) != 1:', 'if not hits:', 'AMBIGUOUS_PREFIX_REFUSED'),
    ('notice newline rewritten', '(" " + note if note else "")', '(" " + note.replace("\\n", " ") if note else "")', 'MESSAGE_IS_LITERAL_DATA'),
    ('partial inbox record decoded', 'if not raw or not raw.endswith(b"\\n"):', 'if not raw:', 'PARTIAL_RECORD_WAITS'),
    ('shown offset not committed', 'os.replace(temporary, path)', 'pass  # offset not committed', 'SHOW_ADVANCES_ONCE'),
    ('watch ignores unread mail', 'if self.unread(target)[0]:', 'if False:', 'WATCH_UNREAD_AND_QUIET'),
    ('queue run precedes reservation', '        requested()\n        runner = self.runner\n        default_runner = runner is run_command\n        try:\n            result = runner(\n                command + ["queue", "--thread", target.identity, "--message", line],\n                self.timeout)\n', '        runner = self.runner\n        default_runner = runner is run_command\n        try:\n            result = runner(\n                command + ["queue", "--thread", target.identity, "--message", line],\n                self.timeout)\n            requested()\n', 'QUEUE_REQUEST_PRECEDES_RUN'),
    ('unknown outcome permits replay', 'elif row.get("status") in TERMINAL | {"REQUESTED", "RESERVED"}:', 'elif row.get("status") == "OUTCOME_UNKNOWN":\n            attempts.pop(attempt, None)\n        elif row.get("status") in TERMINAL | {"REQUESTED", "RESERVED"}:', 'UNKNOWN_HAS_NO_FALLBACK'),
    ('missing urgent falls back to queue', 'adapter = self.urgent if delivery == "urgent" else self.queue', 'adapter = (self.urgent or self.queue) if delivery == "urgent" else self.queue', 'URGENT_HAS_NO_BUNDLED_ROUTE'),
    ('invalid journal treated as empty', 'raise Refused("Incomplete or oversized journal record; inspect state")', 'return', 'INVALID_JOURNAL_NOT_EMPTY'),
    ('show batch exceeds declared 64', 'MAX_BATCH = 64', 'MAX_BATCH = 65', 'SHOW_BATCH_IS_BOUNDED'),
    ('relative state uses current directory', 'state = path.parent / state', 'state = Path.cwd() / state', 'CLEAN_CONFIG_IS_SELF_CONTAINED'),
    ('adapter IO error treated as refusal', 'receipt = Receipt("OUTCOME_UNKNOWN" if adapter_called or requested_count else "REFUSED",', 'receipt = Receipt("REFUSED" if isinstance(error, OSError) and not requested_count else "OUTCOME_UNKNOWN" if adapter_called or requested_count else "REFUSED",', 'ADAPTER_EXCEPTION_IS_UNKNOWN'),
    ('wrong transport status accepted', 'receipt.status != expected_status', 'False', 'ADAPTER_MODE_MATCHES_TRANSPORT'),
    ('final journal failure ignored', 'return Receipt("REFUSED" if receipt.status == "REFUSED" else "OUTCOME_UNKNOWN",\n                           "Final receipt not recorded: " + failure_detail(error)[:200])', 'return receipt', 'FINAL_JOURNAL_FAILURE_IS_UNKNOWN'),
    ('shell/batch wrapper reaches executor', 'if self.runner is run_command:', 'if False:', 'COMMAND_WRAPPERS_REFUSED_BEFORE_REQUEST'),
    ('self loses client namespace', 'return left == right', 'return left is not None and right is not None and left.identity == right.identity', 'SELF_USES_CLIENT_AND_FULL_ID'),
    ('self uses only ID prefix', 'return left == right', 'return left is not None and right is not None and left.client == right.client and left.identity[:8] == right.identity[:8]', 'SELF_USES_CLIENT_AND_FULL_ID'),
    ('pending request permits replay', 'elif row.get("status") in TERMINAL | {"REQUESTED", "RESERVED"}:', 'elif row.get("status") in TERMINAL:', 'REQUESTED_IS_NOT_REPLAYED'),
    ('ring appears before letter', 'append_record(paths["jsonl"], dict(ts=now(), line=line))\n        paths["ring"].touch()', 'paths["ring"].touch()\n        append_record(paths["jsonl"], dict(ts=now(), line=line))', 'LETTER_PRECEDES_RING'),
    ('queue accepts another receiver', 'if code == 0 and match and match.group(2) == target.identity:', 'if code == 0 and match:', 'QUEUE_ECHO_IS_RECEIVER_BOUND'),
    ('Claude explicit mode uses inbox', 'if delivery is not None:', 'if False:', 'CLAUDE_EXPLICIT_MODE_REFUSED'),
    ('apparent success needs no reservation', 'requested_count != 1 or receipt.recipient_id != target.identity', 'False or receipt.recipient_id != target.identity', 'ADAPTER_RESERVATION_REQUIRED'),
    ('apparent success needs no receiver', 'requested_count != 1 or receipt.recipient_id != target.identity', 'requested_count != 1 or False', 'ADAPTER_RECEIVER_REQUIRED'),
    ('unique prefix removed', 'PREFIX = re.compile(r"[0-9a-f][0-9a-f-]{7,35}")', 'PREFIX = ID', 'UNIQUE_SHORT_PREFIX_RESOLVES'),
    ('encoded record cap removed', 'if len(encoded) > MAX_RECORD:', 'if False:', 'RECORD_BYTES_CAPPED'),
    ('pre-dispatch byte validation removed', '        encoded_record(dict(ts=now(), line=line))', '        pass  # no line byte preflight', 'BYTE_CAP_PRECEDES_RESERVATION'),
    ('field character caps removed', 'if len(key) > 256 or len(sender) > 100 or len(note) > 1024:', 'if False:', 'FIELD_CHAR_LIMITS'),
    ('legacy refusal permits replay', 'elif row.get("status") in TERMINAL | {"REQUESTED", "RESERVED"}:', 'elif row.get("status") in (TERMINAL - {"REFUSED"}) | {"REQUESTED", "RESERVED"}:', 'LEGACY_REFUSED_IS_HELD'),
    ('dedup ignores client namespace', 'if (row.get("client"), row.get("id"), row.get("key")) != (\n                target.client, target.identity, key):', 'if (row.get("id"), row.get("key")) != (target.identity, key):', 'DEDUP_CLIENT_NAMESPACE'),
    ('cursor precedes output', '            out.write(json.dumps({"hookSpecificOutput": {\n                "hookEventName": event, "additionalContext": text}},\n                ensure_ascii=True) + "\\n")\n            out.flush()\n            # A crash between these writes leaves an explicit cursor mismatch.\n            self.save_cursor(paths["identity"], json.dumps(identity))\n            self.save_cursor(paths["offset"], str(end))\n', '            self.save_cursor(paths["identity"], json.dumps(identity))\n            self.save_cursor(paths["offset"], str(end))\n            out.write(json.dumps({"hookSpecificOutput": {\n                "hookEventName": event, "additionalContext": text}},\n                ensure_ascii=True) + "\\n")\n            out.flush()\n            # A crash between these writes leaves an explicit cursor mismatch.\n', 'OUTPUT_PRECEDES_CURSOR'),
    ('file identity dropped', '"file": [stat.st_dev, stat.st_ino]', '"file": [0, 0]', 'INBOX_REPLACEMENT_IS_BOUND'),
    ('prefix witness dropped', '"prefix_sha256": hashlib.sha256(prefix).hexdigest()', '"prefix_sha256": "unchecked"', 'CONSUMED_PREFIX_IS_BOUND'),
    ('legacy cursor auto-bound', 'if (start and identity is None) or (identity is not None and (', 'if False or (identity is not None and (', 'LEGACY_CURSOR_NOT_AUTOBOUND'),
    ('cursor mismatch repaired silently', '            identity = json.loads(paths["identity"].read_text(encoding="utf-8"))', '            identity = json.loads(paths["identity"].read_text(encoding="utf-8"))\n            identity["offset"] = start', 'CURSOR_PAIR_MISMATCH_REFUSED'),
    ('pre-dispatch refusal holds key', 'not_started = not adapter_called or (\n                requested_count == 0 and (default_queue() or type(adapter) is Inbox))', 'not_started = False', 'PRE_DISPATCH_REFUSAL_CAN_RETRY'),
    ('child not-started treated as unknown', '        except NotStarted as error:\n            not_started = type(adapter) is CodexQueue and isinstance(error.__cause__, OSError)\n            receipt = Receipt("REFUSED", failure_detail(error))', '        except NotStarted as error:\n            not_started = False\n            receipt = Receipt("OUTCOME_UNKNOWN", failure_detail(error))', 'CHILD_START_IS_KNOWN_BOUNDARY'),
    ('all skipped CLI outcomes green', 'receipt.status == "SKIPPED" and receipt.held_status in\n                         {"STORED", "QUEUED", "THREAD_ACCEPTED"}', 'receipt.status == "SKIPPED"', 'SKIPPED_CLI_CODES_NAME_OUTCOME'),
    ('show output depends on console encoding', '                ensure_ascii=True) + "\\n")', '                ensure_ascii=False) + "\\n")', 'UNICODE_PIPE_IS_ASCII_JSON'),
    ('watch words reversed', 'print("MAIL_READY" if ready else "QUIET")', 'print("QUIET" if ready else "MAIL_READY")', 'WATCH_CLI_WORDS'),
    ('BOM config refused', 'data = json.loads(path.read_text(encoding="utf-8-sig"))', 'data = json.loads(path.read_text(encoding="utf-8"))', 'BOM_CONFIG_ACCEPTED'),
    ('ambiguous Windows name allowed', 'if os.name == "nt" and name != Path(executable).name.lower():', 'if False:', 'WINDOWS_NAME_NORMALISATION'),
    ('show bypasses watcher lock', 'with file_lock(paths["offset"]):\n            letters, end, identity = self._unread(target)', 'with __import__("contextlib").nullcontext():\n            letters, end, identity = self._unread(target)', 'OFFSET_ACCESS_SERIALISED'),
    ('append bypasses writer lock', 'with file_lock(path):\n        fd = os.open', 'path.parent.mkdir(parents=True, exist_ok=True)\n    with __import__("contextlib").nullcontext():\n        fd = os.open', 'APPEND_ACCESS_SERIALISED'),
    ('timed-out child not killed', '            process.kill()', '            pass  # no kill', 'TIMED_OUT_CHILD_KILLED'),
    ('negative offset misclassified', 'if start < 0:', 'if False:', 'NEGATIVE_OFFSET_TYPED_DIAGNOSTIC'),
    ('deleted inbox silently empty', 'if start or identity is not None:', 'if False:', 'DELETED_INBOX_REFUSED'),
    ('queue nonzero accepted', 'if code == 0 and match and match.group(2) == target.identity:', 'if match and match.group(2) == target.identity:', 'QUEUE_NONZERO_NOT_ACKNOWLEDGED'),
    ('queue extra output accepted', 'match = re.fullmatch(r"Queued message (" + ID.pattern +', 'match = re.search(r"Queued message (" + ID.pattern +', 'QUEUE_SURROUNDING_TEXT_NOT_ACKNOWLEDGED'),
    ('queue changed wording accepted', 'match = re.fullmatch(r"Queued message (" + ID.pattern +', 'match = re.fullmatch(r"(?:Queued|Enqueued) message (" + ID.pattern +', 'QUEUE_WORDING_NOT_ACKNOWLEDGED'),
    ('queue item id optional', 'match = re.fullmatch(r"Queued message (" + ID.pattern +\n                                 r") for thread (" + ID.pattern + r")\\.", detail)', 'match = re.fullmatch(r"Queued message ((?:" + ID.pattern +\n                                 r")?) for thread (" + ID.pattern + r")\\.", detail)', 'QUEUE_ITEM_ID_REQUIRED'),
    ('queue diagnostic discarded', '"No exact successful receiver/item echo (exit %s, output %r)" % (code, detail[:200])', '"No exact successful receiver/item echo"', 'QUEUE_UNKNOWN_DIAGNOSTIC_BOUNDED'),
    ('queue diagnostic unbounded', '(code, detail[:200])', '(code, detail)', 'QUEUE_UNKNOWN_DIAGNOSTIC_BOUNDED'),
    ('final refusal reported unknown', 'return Receipt("REFUSED" if receipt.status == "REFUSED" else "OUTCOME_UNKNOWN",', 'return Receipt("OUTCOME_UNKNOWN",', 'FINAL_JOURNAL_REFUSAL_RETAINED'),
    ('saved cursor truncation ignored', 'if start > stream.tell():', 'if False:', 'SAVED_CURSOR_TRUNCATION_REFUSED'),
    ('prefix witness cap mistaken for file size', 'if start > stream.tell():', 'if start > min(stream.tell(), 4096):', 'LARGE_CURSOR_GROWTH_READABLE'),
    ('lock deadline extended', 'deadline = time.monotonic() + 30', 'deadline = time.monotonic() + 300', 'LOCK_DEADLINE_30_SECONDS'),
    ('lock deadline shortened', 'deadline = time.monotonic() + 30', 'deadline = time.monotonic() + 20', 'LOCK_DEADLINE_30_SECONDS'),
    ('queue default timeout changed', 'def __init__(self, command, timeout=30, runner=None):', 'def __init__(self, command, timeout=120, runner=None):', 'QUEUE_DEFAULT_TIMEOUT_30_SECONDS'),
    ('configured timeout rejected', 'if not 0 < timeout <= 120:', 'if not 0 < timeout <= 30:', 'QUEUE_CONFIGURED_TIMEOUT_120_ALLOWED'),
    ('show flush removed', '            out.flush()\n            # A crash', '            pass  # output was not flushed\n            # A crash', 'SHOW_FLUSH_PRECEDES_CURSOR'),
    ('receipt output failure says refused', 'status=("RECEIPT_OUTPUT_FAILED" if receipt is not None else "REFUSED")', 'status="REFUSED"', 'CLI_WRITE_FAILURE_RETAINS_STORED'),
    ('completed receipt omitted from output failure', 'report["delivery_receipt"] = receipt.as_dict()', 'report["delivery_receipt"] = None', 'CLI_WRITE_FAILURE_RETAINS_STORED'),
    ('pre-send receipt not initialized', '    receipt = None\n    try:', '    # no initial receipt\n    try:', 'CLI_WRITE_FAILURE_RETAINS_STORED'),
    ('send receipt not flushed', 'print(json.dumps(receipt.as_dict(), ensure_ascii=True), flush=True)', 'print(json.dumps(receipt.as_dict(), ensure_ascii=True))', 'CLI_FLUSH_FAILURE_RETAINS_STORED'),
    ('held key masks unsupported mode', '        if delivery not in (None, "queue", "urgent"):\n            return Receipt("REFUSED", "Unsupported delivery mode")\n        if target.client == "claude":\n            if delivery is not None:\n                return Receipt("REFUSED", "Explicit Claude queue/urgent is unsupported")\n        lock_key = hashlib.sha256(json.dumps(\n            [target.client, target.identity, key], ensure_ascii=True,\n            separators=(",", ":")).encode("utf-8")).hexdigest()\n        with file_lock(self.state / "send" / lock_key):\n            return self._send_locked(target, key, sender, note, delivery)\n\n    def _send_locked(self, target, key, sender, note, delivery):\n        journal = self.state / "journal.jsonl"\n        held = held_outcome(records(journal), target, key)\n        if held is not None:\n            return Receipt("SKIPPED", "Held " + held["status"] + ": " +\n                           str(held.get("detail", ""))[:200], held_status=held["status"])\n', '        lock_key = hashlib.sha256(json.dumps(\n            [target.client, target.identity, key], ensure_ascii=True,\n            separators=(",", ":")).encode("utf-8")).hexdigest()\n        with file_lock(self.state / "send" / lock_key):\n            return self._send_locked(target, key, sender, note, delivery)\n\n    def _send_locked(self, target, key, sender, note, delivery):\n        journal = self.state / "journal.jsonl"\n        held = held_outcome(records(journal), target, key)\n        if held is not None:\n            return Receipt("SKIPPED", "Held " + held["status"] + ": " +\n                           str(held.get("detail", ""))[:200], held_status=held["status"])\n        if delivery not in (None, "queue", "urgent"):\n            return Receipt("REFUSED", "Unsupported delivery mode")\n        if target.client == "claude":\n            if delivery is not None:\n                return Receipt("REFUSED", "Explicit Claude queue/urgent is unsupported")\n', 'CLAUDE_HELD_MODE_PREFLIGHT'),
    ('short write acknowledged', 'if os.write(fd, encoded) != len(encoded):', 'if os.write(fd, encoded) < 0:', 'SHORT_WRITE_COUNT_NOT_ACKNOWLEDGED'),
    ('append after incomplete tail', 'if size and os.read(fd, 1) != b"\\n":', 'if False and size and os.read(fd, 1) != b"\\n":', 'INCOMPLETE_TAIL_BLOCKS_NEXT_APPEND'),
    ('all NotStarted releases hold', 'not_started = type(adapter) is CodexQueue and isinstance(error.__cause__, OSError)', 'not_started = True', 'URGENT_NOT_STARTED_PRESERVES_HOLD'),
    ('custom runner trusted as default', 'default_runner = runner is run_command', 'default_runner = True', 'CUSTOM_QUEUE_NOT_STARTED_PRESERVES_HOLD'),
    ('subclass trusted as default', 'not_started = type(adapter) is CodexQueue and isinstance(error.__cause__, OSError)', 'not_started = isinstance(adapter, CodexQueue) and isinstance(error.__cause__, OSError)', 'QUEUE_SUBCLASS_NOT_STARTED_PRESERVES_HOLD'),
    ('creation proof cause ignored', 'and isinstance(error.__cause__, OSError)', 'and True', 'DEFAULT_QUEUE_NOT_STARTED_REQUIRES_CAUSE'),
    ('empty notification fields accepted', 'if not key or not sender:', 'if False:', 'NONEMPTY_FIELDS_PREFLIGHT'),
    ('NUL notification fields accepted', 'if any("\\x00" in x for x in (key, sender, note)):', 'if False:', 'NUL_FIELDS_PREFLIGHT'),
    ('nonfinite timeout accepted', 'not math.isfinite(timeout) or timeout < 0', 'timeout < 0', 'WATCH_TIMEOUT_REQUIRES_FINITE'),
    ('nonfinite poll accepted', 'not math.isfinite(poll) or poll <= 0', 'poll <= 0', 'WATCH_POLL_REQUIRES_FINITE'),
    ('relative state moves with process cwd', 'self.state = Path(state).resolve()', 'self.state = Path(state)', 'RELATIVE_CONFIG_STATE_STAYS_BOUND'),
    ('queue argv NUL preflight removed', 'not isinstance(x, str) or not x or "\\x00" in x for x in command', 'not isinstance(x, str) or not x for x in command', 'QUEUE_NUL_BOUNDARY_PRECEDES_REQUEST'),
    ('queue direct notice NUL preflight removed', 'if not isinstance(line, str) or "\\x00" in line:', 'if not isinstance(line, str):', 'QUEUE_NUL_BOUNDARY_PRECEDES_REQUEST'),
    ('huge finite poll escapes typed refusal', 'except OverflowError:\n                raise Refused("Invalid watch interval") from None', 'except ZeroDivisionError:\n                raise Refused("Invalid watch interval") from None', 'HUGE_FINITE_POLL_TYPED_REFUSAL'),
    ('notice loses producer prefix', 'line = "Check %s from %s." % (key, sender)', 'line = "Check %s from %s." % (key.split("-", 1)[-1], sender)', 'DISTINCT_KEYS_RETAIN_ALL_ROWS'),
    ('supplied refusal trusts missing callback', 'not_started = not adapter_called or (\n                requested_count == 0 and (default_queue() or type(adapter) is Inbox))', 'not_started = not adapter_called or requested_count == 0', 'CUSTOM_REFUSED_WITHOUT_CALLBACK_HELD'),
    ('default preflight refusal loses retry', 'not_started = not adapter_called or (\n                requested_count == 0 and (default_queue() or type(adapter) is Inbox))', 'not_started = not adapter_called or (\n                requested_count == 0 and (False or type(adapter) is Inbox))', 'DEFAULT_PREFLIGHT_REFUSAL_RETRY'),
    ('inbox subclass trusted as builtin preflight', 'type(adapter) is Inbox', 'isinstance(adapter, Inbox)', 'CUSTOM_REFUSED_WITHOUT_CALLBACK_HELD'),
    ('runner origin read after invocation', '            if not default_runner:', '            if self.runner is not run_command:', 'CUSTOM_QUEUE_NOT_STARTED_PRESERVES_HOLD'),
    ('queue subclass trusted as builtin preflight', 'return type(adapter) is CodexQueue and adapter.runner is run_command', 'return isinstance(adapter, CodexQueue) and adapter.runner is run_command', 'CUSTOM_REFUSED_WITHOUT_CALLBACK_HELD'),
    ('foreign queue container accepted', 'if type(result) is not tuple and type(result) is not list:', 'if False:', 'QUEUE_RESULT_CONTAINER_PLAIN'),
    ('queue container subclass accepted', 'if type(result) is not tuple and type(result) is not list:', 'if not isinstance(result, (tuple, list)):', 'QUEUE_RESULT_CONTAINER_PLAIN'),
    ('foreign queue value methods accepted', 'if type(code) is not int or type(stdout) is not bytes or type(stderr) is not bytes:', 'if False:', 'QUEUE_RESULT_FIELDS_PLAIN'),
    ('queue formats supplied exception', 'raise Refused("Custom runner no-start is unverified") from error', 'raise Refused("Custom runner no-start is unverified: " + str(error)) from error', 'QUEUE_EXCEPTION_FORMATTING_INERT'),
    ('foreign error formatted', 'def failure_detail(error):', 'def failure_detail(error):\n    return str(error)[:300]', 'EXCEPTION_DETAIL_IS_INERT'),
    ('exception subclass args trusted', 'type(error) is Refused or type(error) is NotStarted or type(error) is ValueError', 'isinstance(error, Refused) or type(error) is NotStarted or type(error) is ValueError', 'EXCEPTION_DETAIL_IS_INERT'),
    ('exception string subclass sliced', 'type(args[0]) is str', 'isinstance(args[0], str)', 'EXCEPTION_DETAIL_IS_INERT'),
    ('supplied receipt subclass accepted', 'if type(receipt) is not Receipt:', 'if not isinstance(receipt, Receipt):', 'RECEIPT_SUBCLASS_IS_INERT'),
    ('supplied required receipt fields trusted', 'type(values[0]) is not str or type(values[1]) is not str', 'False', 'RECEIPT_FIELDS_ARE_DURABLE'),
    ('supplied optional receipt fields trusted', 'any(value is not None and type(value) is not str for value in values[2:])', 'False', 'RECEIPT_FIELDS_ARE_DURABLE'),
    ('supplied final payload not preflighted', '                encoded_record(dict(base, ts=now(), dispatch="POSSIBLE", **receipt.as_dict()))', '                pass  # final payload not preflighted', 'RECEIPT_FIELDS_ARE_DURABLE'),
    ('supplied receipt not snapshotted', 'receipt = Receipt(*values)', 'receipt = receipt', 'RECEIPT_FIELDS_ARE_DURABLE'),
    ('copied diagnostic not UTF8 checked', '                detail.encode("utf-8")', '                pass  # UTF8 check removed', 'EXCEPTION_DETAIL_UTF8_DURABLE'),
    ('honest Unicode diagnostic discarded', '                detail.encode("utf-8")', '                detail.encode("ascii")', 'EXCEPTION_DETAIL_UTF8_DURABLE'),
    ('own receipt validation treated as adapter refusal', 'raise ValueError("Invalid transport receipt size") from None', 'raise', 'RECEIPT_FIELDS_ARE_DURABLE')
]


# Process fixtures use disposable state and bounded handshakes, never a two-entry barrier.
_SEND_CHILD = r"""import errno,importlib.util,json,os,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('child',sys.argv[1]);m=importlib.util.module_from_spec(spec);sys.modules['child']=m;spec.loader.exec_module(m)
state,control,prefix,key,behaviour=Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4],sys.argv[5],sys.argv[6]
def signal(name,text='1'):(control/(prefix+'.'+name)).write_text(text,encoding='utf-8')
# Observe an actual nonblocking OS contention failure on the send lock, not elapsed time.
tracked=set();real_open=m.os.open

def opening(name,*args,**kwargs):
    fd=real_open(name,*args,**kwargs)
    if Path(name).parent==state/'send':tracked.add(fd)
    return fd
m.os.open=opening
if os.name=='nt':
    import msvcrt as backend
    attribute='locking'
else:
    import fcntl as backend
    attribute='flock'
real_lock=getattr(backend,attribute)
def locking(fd,*args):
    try:return real_lock(fd,*args)
    except OSError as error:
        if fd in tracked and error.errno in (errno.EACCES,errno.EAGAIN,errno.EDEADLK):signal('blocked')
        raise
setattr(backend,attribute,locking)
original_scan=m.Courier._send_locked
def scan(self,*args):
    signal('scan')
    return original_scan(self,*args)
m.Courier._send_locked=scan

def adapter(target,line,requested):
    signal('entry')
    if behaviour=='wait':
        deadline=time.monotonic()+60
        while not (control/'release').exists():
            if time.monotonic()>deadline:raise TimeoutError('parent did not release fixture')
            time.sleep(.01)
    if behaviour=='exit':
        rows=list(m.records(state/'journal.jsonl'))
        assert [r['status'] for r in rows]==['RESERVED'],'entry was not pre-held'
    m.append_record(control/'effects.jsonl',{'key':key})
    if behaviour=='exit':os._exit(17)
    requested()
    return m.Receipt('QUEUED','synthetic only',target.identity,'00000000-0000-0000-0000-000000000003')
c=m.Courier(state,{'reviewer':m.Address('codex','00000000-0000-0000-0000-000000000002')},queue=adapter)
signal('ready')
r=c.send('reviewer',key,'fixture')
signal('result',json.dumps(r.as_dict()))
"""


def _wait_for(predicate, seconds=15):
    deadline=time.monotonic()+seconds
    while not predicate():
        if time.monotonic()>deadline:return False
        time.sleep(.01)
    return True


def _send_child(m,state,control,prefix,key,behaviour):
    return subprocess.Popen([sys.executable,'-B','-c',_SEND_CHILD,m.__file__,str(state),str(control),prefix,key,behaviour],
                            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)


def _finish_children(children):
    errors=[]
    for process in children:
        try:
            out,err=process.communicate(timeout=20)
            errors.append((process.returncode,out,err))
        except subprocess.TimeoutExpired:
            process.kill();out,err=process.communicate(timeout=5)
            errors.append(('timeout',out,err))
    return errors


def _lock_is_held(path):
    import errno
    fd=os.open(str(path),os.O_RDWR)
    acquired=False
    try:
        if os.name=='nt':
            import msvcrt
            acquire=lambda:msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            release=lambda:msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            acquire=lambda:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            release=lambda:fcntl.flock(fd,fcntl.LOCK_UN)
        try:acquire();acquired=True
        except OSError as error:
            if error.errno in (errno.EACCES,errno.EAGAIN,errno.EDEADLK):return True
            raise
        return False
    finally:
        if acquired:release()
        os.close(fd)


def _send_contention(m,root,different):
    state,control=root/'state',root/'control';control.mkdir()
    children=[];early=False;blocked=False;held=False;before_scan=False
    try:
        children.append(_send_child(m,state,control,'first','shared','wait'))
        require(_wait_for(lambda:(control/'first.entry').exists()))
        locks=list((state/'send').glob('*.lock'))
        held=len(locks)==1 and _lock_is_held(locks[0])
        require(held)
        children.append(_send_child(m,state,control,'second','other' if different else 'shared','normal'))
        require(_wait_for(lambda:(control/'second.ready').exists()))
        require(_wait_for(lambda:(control/'second.blocked').exists() or (control/'second.result').exists()))
        blocked=(control/'second.blocked').exists()
        early=(control/'second.result').exists()
        before_scan=not (control/'second.scan').exists()
        if different:require(early and not blocked)
        else:require(blocked and not early and before_scan)
    finally:
        (control/'release').write_text('release',encoding='utf-8')
        outcomes=_finish_children(children)
    require(len(outcomes)==2 and all(row[0]==0 for row in outcomes))
    first=json.loads((control/'first.result').read_text())
    second=json.loads((control/'second.result').read_text())
    require(first['status']=='QUEUED')
    if different:
        require(second['status']=='QUEUED')
        require({r['key'] for r in m.records(control/'effects.jsonl')}=={'shared','other'})
        require(len(list(m.records(control/'effects.jsonl')))==2)
    else:
        require(second['status']=='SKIPPED' and second['held_status']=='QUEUED')
        require(not (control/'second.entry').exists())
        require(len(list(m.records(control/'effects.jsonl')))==1)
    require(all(not _lock_is_held(p) for p in (state/'send').glob('*.lock')))


def same_key_send_serialised(m,root):_send_contention(m,root,False)
def distinct_key_send_progress(m,root):_send_contention(m,root,True)


def supplied_entry_preheld(m,root):
    for kind in ('queue','urgent','queue-subclass','inbox-subclass'):
        state=root/kind;effects=[]
        def adapter(target,line,requested):
            require([r['status'] for r in m.records(state/'journal.jsonl')]==['RESERVED'])
            effects.append('synthetic effect')
            raise SystemExit(17)
        c=setup(m,state)
        recipient,mode='reviewer','queue'
        if kind=='queue':c.queue=adapter
        elif kind=='urgent':c.urgent=adapter;mode='urgent'
        elif kind=='queue-subclass':
            class Queue(m.CodexQueue):
                def __call__(self,*args):return adapter(*args)
            c.queue=Queue(['fixture'])
        else:
            class FileInbox(m.Inbox):
                def __call__(self,*args):return adapter(*args)
            c.inbox=FileInbox(state);recipient,mode='writer',None
        try:c.send(recipient,'exit','demo',delivery=mode)
        except SystemExit as error:require(error.code==17)
        else:require(False)
        c.queue=m.CodexQueue(['fixture'],runner=exact);c.inbox=m.Inbox(state)
        c.urgent=lambda t,l,r:m.Receipt('THREAD_ACCEPTED','must not enter',t.identity)
        second=c.send(recipient,'exit','different-sender',delivery=mode)
        require(second.status=='SKIPPED' and second.held_status=='RESERVED')
        require(effects==['synthetic effect'])
    # RESERVED precedes entry; only an actual callback appends REQUESTED and acknowledges success.
    for callbacks in (0,1):
        state=root/('callback'+str(callbacks))
        def adapter(target,line,requested):
            require([r['status'] for r in m.records(state/'journal.jsonl')]==['RESERVED'])
            if callbacks:requested()
            return m.Receipt('QUEUED','synthetic',target.identity,ITEM)
        c=setup(m,state);c.queue=adapter
        first=c.send('reviewer','callback','demo')
        require(first.status==('QUEUED' if callbacks else 'OUTCOME_UNKNOWN'))
        require([r['status'] for r in m.records(state/'journal.jsonl')]==['RESERVED']+['REQUESTED']*callbacks+[first.status])
    # A fresh process exits after a durable synthetic effect, then another process proves lock release and suppression.
    control=root/'process-control';control.mkdir();state=root/'process-state'
    child=_send_child(m,state,control,'dying','exit','exit')
    outcomes=_finish_children([child]);require(outcomes[0][0]==17)
    require(len(list(m.records(control/'effects.jsonl')))==1)
    require([r['status'] for r in m.records(state/'journal.jsonl')]==['RESERVED'])
    child=_send_child(m,state,control,'retry','exit','normal')
    outcomes=_finish_children([child]);require(outcomes[0][0]==0)
    retry=json.loads((control/'retry.result').read_text())
    require(retry['status']=='SKIPPED' and retry['held_status']=='RESERVED')
    require(not (control/'retry.entry').exists())
    require(len(list(m.records(control/'effects.jsonl')))==1)
    locks=list((state/'send').glob('*.lock'))
    require(len(locks)==1 and not _lock_is_held(locks[0]))


def prehold_failure_blocks_entry(m,root):
    c=setup(m,root);entries=[];original=m.append_record
    def adapter(target,line,requested):
        entries.append('entered')
        return m.Receipt('QUEUED','synthetic',target.identity,ITEM)
    c.queue=adapter
    def failure(path,row):
        if row.get('status')=='RESERVED':raise OSError('synthetic prehold failure')
        return original(path,row)
    m.append_record=failure
    try:
        first=c.send('reviewer','failure','demo')
        require(first.status=='REFUSED' and entries==[])
        require(all(r['status']!='RESERVED' for r in m.records(root/'journal.jsonl')))
    finally:m.append_record=original

CHECKS.extend([
    ('SAME_KEY_SEND_SERIALISED',same_key_send_serialised),
    ('DISTINCT_KEY_SEND_PROGRESS',distinct_key_send_progress),
    ('SUPPLIED_ENTRY_PREHELD',supplied_entry_preheld),
    ('PREHOLD_FAILURE_BLOCKS_ENTRY',prehold_failure_blocks_entry),
])
REGRESSIONS.extend([
    ('retained request appended twice', '            append_record(journal, dict(base, ts=now(), status="REQUESTED"))\n            requested_count += 1', '            append_record(journal, dict(base, ts=now(), status="REQUESTED"))\n            append_record(journal, dict(base, ts=now(), status="REQUESTED"))\n            requested_count += 1', 'ADAPTER_RESERVATION_REQUIRED'),
    ('honest exact list rejected', 'if type(result) is not tuple and type(result) is not list:', 'if type(result) is not tuple:', 'QUEUE_RESULT_CONTAINER_PLAIN'),
    ('same-key critical section bypassed', 'with file_lock(self.state / "send" / lock_key):', 'with __import__("contextlib").nullcontext():', 'SAME_KEY_SEND_SERIALISED'),
    ('all delivery keys share one lock', 'with file_lock(self.state / "send" / lock_key):', 'with file_lock(self.state / "send" / "global"):', 'DISTINCT_KEY_SEND_PROGRESS'),
    ('supplied entry reservation removed', '            if type(adapter) is not Inbox and type(adapter) is not CodexQueue:\n                append_record(journal, dict(base, ts=now(), status="RESERVED"))', '            if False:\n                append_record(journal, dict(base, ts=now(), status="RESERVED"))', 'SUPPLIED_ENTRY_PREHELD'),
    ('failed supplied prehold ignored', '            if type(adapter) is not Inbox and type(adapter) is not CodexQueue:\n                append_record(journal, dict(base, ts=now(), status="RESERVED"))', '            if type(adapter) is not Inbox and type(adapter) is not CodexQueue:\n                try:\n                    append_record(journal, dict(base, ts=now(), status="RESERVED"))\n                except OSError:\n                    pass', 'PREHOLD_FAILURE_BLOCKS_ENTRY'),
])



def ordinary_send_no_self_lock(m, root):
    # The normal exact Inbox callback must finish before a mistaken second send-lock acquire can time out.
    child="""import importlib.util,sys
spec=importlib.util.spec_from_file_location('child',sys.argv[1]);m=importlib.util.module_from_spec(spec);sys.modules['child']=m;spec.loader.exec_module(m)
c=m.Courier(sys.argv[2],{'writer':m.Address('claude',sys.argv[3])})
c.inbox.paths(c.resolve('writer'))['ring'].parent.mkdir(parents=True,exist_ok=True)
r=c.send('writer','ordinary','fixture')
print(r.status,flush=True)
"""
    process=subprocess.Popen([sys.executable,'-B','-c',child,m.__file__,str(root),A],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        out,err=process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill();process.communicate(timeout=5);require(False)
    require(process.returncode==0 and out.strip()==b'STORED' and not err)
    # Do not inspect callback-row count here: that belongs to ADAPTER_RESERVATION_REQUIRED.


def reserved_not_replayed(m, root):
    c=setup(m,root,exact)
    m.append_record(root/'journal.jsonl',dict(client='codex',id=B,key='preheld',attempt='fixture',status='RESERVED'))
    before=(root/'journal.jsonl').read_bytes()
    r=c.send('reviewer','preheld','fixture')
    require(r.status=='SKIPPED' and r.held_status=='RESERVED')
    require((root/'journal.jsonl').read_bytes()==before)


def adapter_type_identity(m, root):
    # Hostile equality is a separating control, not a claim to sandbox in-process code.
    comparisons=[];entries=[]
    class Meta(type):
        def __eq__(cls,other):
            comparisons.append('effect before entry')
            raise SystemExit(19)
    class Adapter(metaclass=Meta):
        def __call__(self,target,line,requested):
            entries.append(True);requested()
            return m.Receipt('QUEUED','synthetic',target.identity,ITEM)
    c=setup(m,root);c.queue=Adapter()
    try:r=c.send('reviewer','identity','fixture')
    except BaseException:require(False)
    require(r.status=='QUEUED' and comparisons==[] and entries==[True])
    require(c.send('reviewer','identity','fixture').status=='SKIPPED' and entries==[True])
    require([r['status'] for r in m.records(root/'journal.jsonl')]==['RESERVED','REQUESTED','QUEUED'])


def keyboard_interrupt_preheld(m, root):
    for kind in ('queue','urgent','queue-subclass','inbox-subclass'):
        state=root/kind;effects=[]
        def adapter(target,line,requested):
            effects.append(True);raise KeyboardInterrupt('synthetic interruption')
        c=setup(m,state);recipient,mode='reviewer','queue'
        if kind=='queue':c.queue=adapter
        elif kind=='urgent':c.urgent=adapter;mode='urgent'
        elif kind=='queue-subclass':
            class Queue(m.CodexQueue):
                def __call__(self,*args):return adapter(*args)
            c.queue=Queue(['fixture'])
        else:
            class FileInbox(m.Inbox):
                def __call__(self,*args):return adapter(*args)
            c.inbox=FileInbox(state);recipient,mode='writer',None
        try:c.send(recipient,'interrupted','fixture',delivery=mode)
        except KeyboardInterrupt:pass
        else:require(False)
        require([r['status'] for r in m.records(state/'journal.jsonl')]==['RESERVED'])
        r=c.send(recipient,'interrupted','fixture',delivery=mode)
        require(r.status=='SKIPPED' and r.held_status=='RESERVED' and effects==[True])


def refused_prehold_blocks_entry(m, root):
    c=setup(m,root);entries=[];original=m.append_record
    def adapter(target,line,requested):
        entries.append(True);requested();return m.Receipt('QUEUED','synthetic',target.identity,ITEM)
    c.queue=adapter
    def refused(path,row):
        if row.get('status')=='RESERVED':raise m.Refused('synthetic prehold refusal')
        return original(path,row)
    m.append_record=refused
    try:
        r=c.send('reviewer','prehold-refusal','fixture')
        require(r.status=='REFUSED' and entries==[])
        rows=list(m.records(root/'journal.jsonl'))
        require(len(rows)==1 and rows[0]['status']=='REFUSED' and rows[0]['dispatch']=='NOT_STARTED')
    finally:m.append_record=original
    require(c.send('reviewer','prehold-refusal','fixture').status=='QUEUED' and entries==[True])


def recursive_key_refuses(m, root):
    # Use a real second-handle OS denial, advance only the contention clock to its deadline.
    # If the broader key lock is removed, SAME_KEY_SEND_SERIALISED owns that regression.
    c=setup(m,root);entries=[];denials=[];nested=[]
    backend=__import__('msvcrt' if os.name=='nt' else 'fcntl')
    attr='locking' if os.name=='nt' else 'flock'
    real_lock=getattr(backend,attr);real_clock=m.time.monotonic;real_sleep=m.time.sleep
    clock=[0.0]
    def locking(*args):
        try:return real_lock(*args)
        except OSError as error:
            if error.errno in (m.errno.EACCES,m.errno.EAGAIN,m.errno.EDEADLK):
                denials.append(error.errno);clock[0]+=30.0
            raise
    def adapter(target,line,requested):
        entries.append(True)
        locks=list((root/'send').glob('*.lock'))
        if not locks or not _lock_is_held(locks[0]):
            requested();return m.Receipt('QUEUED','broader lock control',target.identity,ITEM)
        try:nested.append(c.send('reviewer','recursive','nested'))
        except m.Refused:nested.append('REFUSED');raise
        raise RuntimeError('recursive send did not refuse')
    c.queue=adapter
    setattr(backend,attr,locking);m.time.monotonic=lambda:clock[0];m.time.sleep=lambda delay:None
    try:r=c.send('reviewer','recursive','fixture')
    finally:setattr(backend,attr,real_lock);m.time.monotonic=real_clock;m.time.sleep=real_sleep
    if not denials:return  # Broader no-lock regression is attributed by its existing independent check.
    require(nested==['REFUSED'] and entries==[True] and r.status=='REFUSED')
    require(clock[0]==60.0)  # Independent probe denial plus actual recursive second-handle denial.
    rows=list(m.records(root/'journal.jsonl'))
    require(rows[-1]['status']=='REFUSED' and rows[-1]['dispatch']=='POSSIBLE')
    require(c.send('reviewer','recursive','fixture').status=='SKIPPED' and entries==[True])
    require(all(not _lock_is_held(p) for p in (root/'send').glob('*.lock')))


_CLI_SEND_CHILD=r"""import errno,importlib.util,json,os,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('child',sys.argv[1]);m=importlib.util.module_from_spec(spec);sys.modules['child']=m;spec.loader.exec_module(m)
state,control,prefix,key,kind,wait=Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4],sys.argv[5],sys.argv[6],sys.argv[7]=='wait'
def signal(name,text='1'):(control/(prefix+'.'+name)).write_text(text,encoding='utf-8')
tracked=set();real_open=m.os.open
def opening(name,*args,**kwargs):
 fd=real_open(name,*args,**kwargs)
 if Path(name).parent==state/'send':tracked.add(fd)
 return fd
m.os.open=opening
backend=__import__('msvcrt' if os.name=='nt' else 'fcntl');attr='locking' if os.name=='nt' else 'flock';real_lock=getattr(backend,attr)
def locking(fd,*args):
 try:return real_lock(fd,*args)
 except OSError as error:
  if fd in tracked and error.errno in (errno.EACCES,errno.EAGAIN,errno.EDEADLK):signal('blocked')
  raise
setattr(backend,attr,locking)
wrapper=m.Inbox if kind=='inbox' else m.CodexQueue
original=wrapper.__call__
def entered(self,*args):
 signal('entry')
 deadline=time.monotonic()+30
 while wait and not (control/'release').exists():
  if time.monotonic()>deadline:raise RuntimeError('parent did not release')
  time.sleep(.01)
 return original(self,*args)
wrapper.__call__=entered
def runner(argv,timeout):
 m.append_record(control/'effects.jsonl',{'key':key})
 return 0,('Queued message 00000000-0000-0000-0000-000000000003 for thread '+argv[-3]+'.').encode(),b''
m.run_command=runner
config=control/'config.json'
sys.argv=['courier.py','--config',str(config),'send','target','--key',key,'--sender','fixture']
signal('ready')
rc=m.main();signal('result',str(rc));raise SystemExit(rc)
"""


def cli_same_key_serialised(m, root):
    for kind in ('inbox','queue'):
        for different in (False,True):
            work=root/(kind+str(different));state=work/'state';control=work/'control';control.mkdir(parents=True)
            client='claude' if kind=='inbox' else 'codex'
            config={'state_dir':str(state),'recipients':{'target':{'client':client,'id':B}}}
            if kind=='queue':config['codex_queue_command']=['synthetic-executable']
            (control/'config.json').write_text(json.dumps(config),encoding='utf-8')
            children=[]
            def start(prefix,key,mode):
                return subprocess.Popen([sys.executable,'-B','-c',_CLI_SEND_CHILD,m.__file__,str(state),str(control),prefix,key,kind,mode],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                children.append(start('first','shared','wait'))
                require(_wait_for(lambda:(control/'first.entry').exists()))
                children.append(start('second','other' if different else 'shared','normal'))
                require(_wait_for(lambda:(control/'second.ready').exists()))
                require(_wait_for(lambda:(control/'second.blocked').exists() or (control/'second.result').exists()))
                if different:require((control/'second.result').exists() and not (control/'second.blocked').exists())
                else:require((control/'second.blocked').exists() and not (control/'second.entry').exists())
            finally:
                (control/'release').write_text('1',encoding='utf-8');outcomes=_finish_children(children)
            require(len(outcomes)==2 and all(x[0]==0 and not x[2] for x in outcomes))
            replies=[json.loads(x[1]) for x in outcomes]
            success='STORED' if kind=='inbox' else 'QUEUED'
            require(replies[0]['status']==success)
            require(replies[1]['status']==(success if different else 'SKIPPED'))
            if not different:require(replies[1]['held_status']==success)
            effect_file=state/'inbox'/(B+'.jsonl') if kind=='inbox' else control/'effects.jsonl'
            require(len(list(m.records(effect_file)))==(2 if different else 1))


CHECKS.insert(0,('ORDINARY_SEND_NO_SELF_LOCK',ordinary_send_no_self_lock))
CHECKS.insert(next(i for i,x in enumerate(CHECKS) if x[0]=='SAME_KEY_SEND_SERIALISED'),('RECURSIVE_KEY_REFUSES',recursive_key_refuses))
CHECKS.insert(next(i for i,x in enumerate(CHECKS) if x[0]=='SUPPLIED_ENTRY_PREHELD'),('RESERVED_IS_NOT_REPLAYED',reserved_not_replayed))
CHECKS.extend([
    ('ADAPTER_TYPE_IDENTITY',adapter_type_identity),
    ('KEYBOARD_INTERRUPT_PREHELD',keyboard_interrupt_preheld),
    ('REFUSED_PREHOLD_BLOCKS_ENTRY',refused_prehold_blocks_entry),
    ('CLI_SEND_SAME_KEY_SERIALISED',cli_same_key_serialised),
])

REGRESSIONS.extend([('ordinary send reacquires its own lock', '        with file_lock(self.state / "send" / lock_key):\n            return self._send_locked(target, key, sender, note, delivery)', '        with file_lock(self.state / "send" / lock_key):\n            with file_lock(self.state / "send" / lock_key):\n                return self._send_locked(target, key, sender, note, delivery)', 'ORDINARY_SEND_NO_SELF_LOCK'), ('recursive held fast path skips lock', '        with file_lock(self.state / "send" / lock_key):\n            return self._send_locked(target, key, sender, note, delivery)', '        early_hold = held_outcome(records(self.state / "journal.jsonl"), target, key)\n        if early_hold is not None and early_hold["status"] == "RESERVED":\n            return Receipt("SKIPPED", "Premature reserved fast path", held_status="RESERVED")\n        with file_lock(self.state / "send" / lock_key):\n            return self._send_locked(target, key, sender, note, delivery)', 'RECURSIVE_KEY_REFUSES'), ('reserved status omitted from replay fold', 'elif row.get("status") in TERMINAL | {"REQUESTED", "RESERVED"}:', 'elif row.get("status") in TERMINAL | {"REQUESTED"}:', 'RESERVED_IS_NOT_REPLAYED'), ('prehold recorded as callback', 'append_record(journal, dict(base, ts=now(), status="RESERVED"))', 'append_record(journal, dict(base, ts=now(), status="REQUESTED"))', 'ADAPTER_RESERVATION_REQUIRED'), ('adapter type equality invoked before hold', 'if type(adapter) is not Inbox and type(adapter) is not CodexQueue:', 'if type(adapter) not in (Inbox, CodexQueue):', 'ADAPTER_TYPE_IDENTITY'), ('keyboard interruption releases attempt', '        except NotStarted as error:\n            not_started = type(adapter) is CodexQueue', '        except KeyboardInterrupt:\n            append_record(journal, dict(base, ts=now(), status="REFUSED", dispatch="NOT_STARTED"))\n            raise\n        except NotStarted as error:\n            not_started = type(adapter) is CodexQueue', 'KEYBOARD_INTERRUPT_PREHELD'), ('prehold refusal ignored', '            if type(adapter) is not Inbox and type(adapter) is not CodexQueue:\n                append_record(journal, dict(base, ts=now(), status="RESERVED"))', '            if type(adapter) is not Inbox and type(adapter) is not CodexQueue:\n                try:\n                    append_record(journal, dict(base, ts=now(), status="RESERVED"))\n                except Refused:\n                    pass', 'REFUSED_PREHOLD_BLOCKS_ENTRY'), ('CLI dispatch bypasses shared send lock', 'receipt = courier.send(args.recipient, args.key, args.sender,\n                                   args.note, args.delivery)', 'receipt = courier._send_locked(courier.resolve(args.recipient), args.key, args.sender,\n                                           args.note, args.delivery)', 'CLI_SEND_SAME_KEY_SERIALISED')])


def run_checks(m):
    global CURRENT
    for name, operation in CHECKS:
        CURRENT = name
        with tempfile.TemporaryDirectory(prefix="offline-", dir=Path(__file__).parent) as folder:
            operation(m, Path(folder).resolve())
    return len(CHECKS)


def main():
    count = run_checks(courier)
    print("Functional checks: %d/%d" % (count, len(CHECKS)))
    source = (ROOT / "courier.py").read_text(encoding="utf-8")
    passed = 0
    for index, (label, before, after, expected) in enumerate(REGRESSIONS):
        if source.count(before) != 1:
            raise RuntimeError("Regression anchor is not unique: " + label)
        name = "_courier_regression_%d" % index
        mutant = types.ModuleType(name)
        sys.modules[name] = mutant
        try:
            with tempfile.TemporaryDirectory(prefix="mutant-", dir=Path(__file__).parent) as folder:
                subject = Path(folder) / "courier.py"
                subject.write_text(source.replace(before, after), encoding="utf-8")
                mutant.__file__ = str(subject)
                exec(compile(subject.read_text(encoding="utf-8"), str(subject), "exec"), mutant.__dict__)
                try:
                    run_checks(mutant)
                except AssertionError as error:
                    if str(error) != expected:
                        raise RuntimeError("Wrong first failure: %s -> %s" % (label, error))
                    passed += 1
                    print("Regression: %s -> %s" % (label, expected))
                else:
                    raise RuntimeError("Regression survived: " + label)

        finally:
            del sys.modules[name]
    print("Offline result: %d checks; %d/%d regressions detected first by their named FUNCTION check" %
          (count, passed, len(REGRESSIONS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
