"""State and receipt boundaries for the optional adapters; synthetic local states."""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import uuid

EXPECTED_TEETH = 13

def need(value, detail):
    if not value:
        raise AssertionError(detail)

def load(root):
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    import courier, coura_remote, coura_pair, coura_ux, observer
    spec = importlib.util.spec_from_file_location("boundary_remote_fixtures", root/"tests/check_remote.py")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    return courier, coura_remote, coura_pair, coura_ux, observer, fixtures

def plant(remote, fx, state, identity="task", subject="hello"):
    payload = fx.task_bytes(identity, subject, identity)
    remote.receive_bytes(state, fx.command_wire(fx.PHONE, uuid.uuid4().hex, payload),
                         "phone", "handset")
    return payload

def start_retained(modules):
    c, r, p, u, o, fx = modules
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder)
        plant(r, fx, state)
        original = r.run_check
        calls = []
        def interrupted(check, subject):
            calls.append(check)
            raise SystemExit(86)
        r.run_check = interrupted
        try:
            try:
                r._pending_executions(state/"remote")
            except SystemExit as error:
                need(error.code == 86, "wrong interruption")
        finally:
            r.run_check = original
        rows = r.exec_rows(state)
        need(any(row.get("disposition") == "started" for row in rows), "no durable start")
        def counted(check, subject):
            calls.append(check)
            return original(check, subject)
        r.run_check = counted
        try:
            pending = r._pending_executions(state/"remote")
        finally:
            r.run_check = original
        need(len(calls) == 1, "interrupted computation repeated")
        need(pending == [{"task_id":"task", "disposition":"execution_unknown"}], pending)
        fresh = state/"fresh"
        plant(r, fx, fresh, "fresh")
        healthy = r._pending_executions(fresh/"remote")
        need(healthy[0]["output"] == {"byte_length":5}, "fresh control refused")

def refuse_once(modules, folder):
    c, r, p, u, o, fx = modules
    devices = fx.Pair(folder)
    plant(r, fx, devices.phone_remote, "result")
    config = json.loads(devices.phone_config.read_text())
    config["transports"]["computer"]["data_only"] = False
    devices.phone_config.write_text(json.dumps(config))
    reports = devices.complete()
    need(reports[0]["send_status"] == "REFUSED", reports)
    rows = r.exec_rows(devices.phone_remote)
    journal = list(c.records(devices.phone_state/"journal.jsonl"))
    need(journal[-1].get("dispatch") == "NOT_STARTED", journal)
    return devices, config, rows

def return_retry(modules):
    c, r, p, u, o, fx = modules
    with tempfile.TemporaryDirectory() as folder:
        devices, config, rows = refuse_once(modules, folder)
        need(not any(row.get("disposition") == "result-sent" for row in rows),
             "refusal labelled sent")
        config["transports"]["computer"]["data_only"] = True
        devices.phone_config.write_text(json.dumps(config))
        reports = devices.complete()
        need(reports and reports[0]["send_status"] == "STORED", reports)
        need(len(fx.executed(devices.phone_remote)) == 1, "delivery retry recomputed")
        need(len(fx.payloads(devices.computer_remote)) == 1, "stored output was lost")
        need(devices.complete() == [], "confirmed result requeued")

def result_bytes(modules):
    c, r, p, u, o, fx = modules
    with tempfile.TemporaryDirectory() as folder:
        devices, config, rows = refuse_once(modules, folder)
        saved = [row for row in rows if row.get("disposition") == "result-prepared"]
        need(len(saved) == 1, "exact result not retained before refused send")
        raw = base64.b64decode(saved[0]["payload_b64"], validate=True)
        config["transports"]["computer"]["data_only"] = True
        devices.phone_config.write_text(json.dumps(config))
        reports = r.complete(devices.phone_remote, devices.phone_config, "desk",
                             "phone-1", b"\x99"*32)
        need(reports and reports[0]["send_status"] == "STORED", reports)
        need(fx.payloads(devices.computer_remote) == [raw], "retry re-signed stored output")
        need(len(fx.executed(devices.phone_remote)) == 1, "stored output recomputed")
        config["recipients"]["desk"]["id"] = "another"
        devices.phone_config.write_text(json.dumps(config))
        # Directly exercise the retained-address boundary after a confirmed send.
        instance = c.Courier.from_config(devices.phone_config)
        try:
            r._prepared_result(devices.phone_remote/"remote", fx.executed(devices.phone_remote)[0],
                               instance.resolve("desk"), "phone-1", b"\x99"*32)
        except r.RemoteError:
            pass
        else:
            raise AssertionError("stored bytes were rebound to a different recipient")

def conflict_bytes(modules):
    c, r, p, u, o, fx = modules
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder)
        first = plant(r, fx, state, "same", "first")
        same = r.receive_bytes(state, fx.command_wire(fx.PHONE, "b"*32, first),
                               "phone", "handset")
        changed = fx.task_bytes("same", "x"*5000, "same")
        ack = r.receive_bytes(state, fx.command_wire(fx.PHONE, "c"*32, changed),
                              "phone", "handset")
        rows = r.repeat_rows(state)
        need(same["status"] == ack["status"] == "STORED", "durable audit ack")
        need(rows[0].get("conflict") is False, "honest repeat accused")
        need(rows[1].get("conflict") is True, "changed task not distinguished")
        need(base64.b64decode(rows[1].get("payload_b64", ""), validate=True) == changed,
             "conflict acknowledged without exact bytes")
        need(rows[1]["recipient"] == fx.PHONE and rows[1]["attempt"] == "c"*32,
             "conflict lost address or attempt")
        need(fx.payloads(state) == [first], "conflict replaced first task")
        pending = r._pending_executions(state/"remote")
        need(pending[0]["output"] == {"byte_length":5}, "conflicting subject executed")

def full_pair_address(modules):
    c, r, p, u, o, fx = modules
    need(hasattr(p, "peer_key"), "full-address slot missing")
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder); desk = root/"desk"
        for kind in ("phone", "worker"):
            code = p.create_offer(desk, "computer", "desk", 600, 1700000000)
            payload = p.join_payload(root/kind, code, kind, "same")
            p.commit_join(desk, payload, 1700000000)
        rows = json.loads(p._peers_path(desk).read_text())
        need(set(rows) == {p.peer_key("phone", "same"), p.peer_key("worker", "same")}
             and len(rows) == 2, rows)
        joined = root/"joined"
        for kind in ("phone", "worker"):
            offer_state = root/("offer-"+kind)
            code = p.create_offer(offer_state, kind, "same", 600, 1700000000)
            payload = p.join_payload(joined, code, "computer", "joined")
            reply = p.confirm_join(offer_state, payload, 1700000000)
            p.confirm_accept(joined, reply)
        rows = json.loads(p._peers_path(joined).read_text())
        need(len(rows) == 2 and {row["kind"] for row in rows.values()} == {"phone","worker"},
             "accept path merged full addresses")

def point_validity(modules):
    c, r, p, u, o, fx = modules
    public = b"\x01"+b"\x00"*31
    signature = public+b"\x00"*32
    need(not p.verify(public, b"first", signature), "identity forgery verified")
    need(not p.verify(public, b"another", signature), "message-independent forgery verified")
    noncanonical = public[:-1]+b"\x80"
    need(not p.verify(noncanonical, b"first", signature), "noncanonical identity verified")
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    key = p.public_key(seed); signed = p.sign(seed, b"control")
    need(p.verify(key, b"control", signed), "valid signing control refused")
    need(not p.verify(key, b"changed", signed), "tampered message verified")
    for raw in (b'{"signature":"00"}',
                b'{"signature":"00","public_key":1}',
                b'{"signature":"00","public_key":"","public_key":""}'):
        try:
            p._open_signed(raw)
        except p.PairError:
            pass
        else:
            raise AssertionError("malformed signature did not fail closed")

def atomic_pair_state(modules):
    c, r, p, u, o, fx = modules
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)/"peers.json"
        p._write_json(path, {"before":1})
        prior = path.read_bytes()
        original = p.os.replace
        def interrupted(src, dst):
            raise OSError("replace interrupted")
        p.os.replace = interrupted
        try:
            try:p._write_json(path, {"after":2})
            except OSError:pass
        finally:
            p.os.replace = original
        need(path.read_bytes() == prior, "interrupted write destroyed previous state")
        p._write_json(path, {"after":2})
        need(json.loads(path.read_text()) == {"after":2}, "normal write failed")
        need(sorted(x.name for x in path.parent.iterdir()) == ["peers.json"], "temporary leaked")

def make_status(modules, root):
    c, r, p, u, o, fx = modules
    config = root/"coura.json"; state=root/"state"
    u.write_setup(config,state,"desk","computer",["a=worker:a","b=worker:b"],False)
    return config, state

def prefix_status(modules):
    c, r, p, u, o, fx = modules
    with tempfile.TemporaryDirectory() as folder:
        config,state=make_status(modules,Path(folder))
        journal=state/"journal.jsonl"
        prefix=(json.dumps(dict(client="worker",id="a",key="kept-key",
                       attempt="a"*32,status="OUTCOME_UNKNOWN"))+"\n").encode("utf-8")
        journal.write_bytes(prefix)
        honest=u.status_text(config)
        need("kept-key" in honest and "Uncertain: 1" in honest
             and "Journal: unreadable" not in honest,"honest prefix falsely accused")
        for tail in (b'{"status":', b"x"*8200):
            raw=prefix+tail
            journal.write_bytes(raw)
            text=u.status_text(config)
            need("kept-key" in text,"complete retained hold disappeared: "+text)
            need("Journal: unreadable" in text and "Uncertain: unreadable" in text
                 and "Queued: unreadable" in text and "Last discrepancies: unreadable" in text,
                 "incomplete coverage hidden: "+text)
            need(journal.read_bytes()==raw,"status modified the journal")

def recipient_status(modules):
    c, r, p, u, o, fx = modules
    with tempfile.TemporaryDirectory() as folder:
        config,state=make_status(modules,Path(folder))
        c.append_record(state/"journal.jsonl",dict(client="worker",id="a",key="shared",
                        attempt="a"*32,status="OUTCOME_UNKNOWN"))
        c.append_record(state/"journal.jsonl",dict(client="worker",id="b",key="shared",
                        attempt="b"*32,status="STORED"))
        c.append_record(state/"journal.jsonl",dict(client="worker",id="a",key="shared",
                        attempt="c"*32,status="REFUSED",dispatch="NOT_STARTED"))
        text = u.status_text(config)
        need("Uncertain: 1" in text and "worker:a key shared" in text,
             "a different recipient hid the uncertain attempt: "+text)

def corrupt_status(modules):
    c, r, p, u, o, fx = modules
    with tempfile.TemporaryDirectory() as folder:
        config,state=make_status(modules,Path(folder))
        healthy = u.status_text(config)
        need("Uncertain: none" in healthy and "Journal: unreadable" not in healthy,
             "honest empty journal accused")
        for raw in (b"not-json\n", b'{"status":', b"[]\n", b"{}\n", b"x"*8200+b"\n",
                    b'{"client":"worker","id":"a","key":"dup","status":"OUTCOME_UNKNOWN","status":"STORED"}\n'):
            (state/"journal.jsonl").write_bytes(raw)
            text=u.status_text(config)
            need("Journal: unreadable" in text and "Uncertain: unreadable" in text
                 and "Queued: unreadable" in text and "Last discrepancies: unreadable" in text,
                 "corrupt state looked healthy: "+text)

def output_bound(modules):
    c, r, p, u, o, fx = modules
    for stream in ("stdout","stderr"):
        argv=[sys.executable,"-c","import sys;sys."+stream+".buffer.write(b'x'*262144)"]
        try:r.forward(argv,b"{}")
        except r.RemoteError as error:
            need("output exceeds" in str(error), "wrong refusal")
        else:raise AssertionError("oversized output accepted")
    code,out,err=r.forward([sys.executable,"-c","import sys;sys.stdout.buffer.write(b'ok')"],b"{}")
    need((code,out,err)==(0,b"ok",b""),"small output control refused")

def observer_instance(modules,state,include=False):
    c, r, p, u, o, fx = modules
    o.bind(c)
    return c.Courier(state,{"beta":c.Address("worker","beta"),"agenda":c.Address("worker","agenda")},
                     adapters={"worker":c.Adapter(c.DataInbox(state),
                       frozenset({"notification","data","data_only"}))},
                     observer={"destination":"agenda","include_body":include,"producer_id":"coura-local"})

def candidate_coverage(modules):
    c, r, p, u, o, fx = modules
    with tempfile.TemporaryDirectory() as folder:
        state=Path(folder);app=observer_instance(modules,state)
        for number in range(80):
            c.append_record(state/"journal.jsonl",dict(client="worker",id="beta",key=str(number),
                            attempt=("%032x"%number),status="RESERVED"))
        for _pass in range(3):
            partial=o.inspect_events(app,64,"all")
        need(partial["source_recovery_pending"] is True,
             "remaining candidates hidden at byte EOF")
        complete=None
        for _pass in range(4):
            complete=o.inspect_events(app,64,"all")
        need(complete["source_recovery_pending"] is False and complete["counts"].get("pending")==80,
             "candidate recovery did not finish")
        need(not (state/"data-inbox").exists(),"inspection delivered")

def body_copy_switch(modules):
    c, r, p, u, o, fx = modules
    with tempfile.TemporaryDirectory() as folder:
        text="COURA PRIVATE SAMPLE BODY 0001"
        row=dict(client="worker",id="beta",key="private",attempt="a"*32,status="STORED")
        off=observer_instance(modules,Path(folder)/"off",False)
        on=observer_instance(modules,Path(folder)/"on",True)
        private=o.build_event(off,"journal.jsonl",0,row,body_text=text)
        visible=o.build_event(on,"journal.jsonl",0,row,body_text=text)
        need(text not in json.dumps(private),"disabled body copy leaked")
        need(visible["body"]==text,"enabled body copy refused ordinary plaintext")

CHECKS=[
 ("REMOTE_START_RETAINED",start_retained),
 ("REMOTE_RETURN_RETRY",return_retry),
 ("REMOTE_RESULT_BYTES_RETAINED",result_bytes),
 ("REMOTE_CONFLICT_RETAINED",conflict_bytes),
 ("PAIR_FULL_ADDRESS",full_pair_address),
 ("PAIR_POINT_VALIDITY",point_validity),
 ("PAIR_ATOMIC_STATE",atomic_pair_state),
 ("STATUS_PREFIX_RETAINED",prefix_status),
 ("STATUS_RECIPIENT_BOUND",recipient_status),
 ("STATUS_CORRUPT_UNKNOWN",corrupt_status),
 ("REMOTE_CAPTURE_BOUND",output_bound),
 ("OBSERVER_CANDIDATE_COVERAGE",candidate_coverage),
 ("OBSERVER_BODY_COPY_SWITCH",body_copy_switch),
]
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--root",default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--only")
    parser.add_argument("--status-only",action="store_true")
    args=parser.parse_args()
    modules=load(args.root)
    if len(CHECKS)!=EXPECTED_TEETH:raise SystemExit("CHECK_POPULATION_DRIFT")
    selected=[(name,func) for name,func in CHECKS
              if (args.only is None or args.only==name)
              and (not args.status_only or name.startswith("STATUS_"))]
    if not selected:raise SystemExit("EMPTY_CHECK_SELECTION")
    for name, func in selected:
        try:func(modules)
        except AssertionError as error:
            print("FAIL "+name+" "+str(error),flush=True)
            return 1
        except BaseException as error:
            print("ERROR "+name+" "+type(error).__name__+":"+str(error),flush=True)
            return 2
        print("PASS "+name,flush=True)
    print("STATE_BOUNDARIES PASS "+str(len(selected)),flush=True)
    return 0
if __name__=="__main__":raise SystemExit(main())
