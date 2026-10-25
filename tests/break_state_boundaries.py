"""Cause each state-boundary regression and verify the first named check."""
import argparse, hashlib, json, pathlib, shutil, subprocess, sys, tempfile, time
ROOT=pathlib.Path(__file__).resolve().parents[1]
MUTATIONS=[
 ("prefix-discarded","STATUS_PREFIX_RETAINED","coura_ux.py",
  '            if len(raw) > limit or not raw.endswith(b"\\n"):\n                return rows, True',
  '            if len(raw) > limit or not raw.endswith(b"\\n"):\n                return [], True',1),
 ("prefix-warning-hidden","STATUS_PREFIX_RETAINED","coura_ux.py",
  '            if len(raw) > limit or not raw.endswith(b"\\n"):\n                return rows, True',
  '            if len(raw) > limit or not raw.endswith(b"\\n"):\n                return rows, False',1),
 ("prefix-warning-overfire","STATUS_PREFIX_RETAINED","coura_ux.py",
  '            if not raw:\n                return rows, False',
  '            if not raw:\n                return rows, bool(rows)',1),
 ("duplicate-member-accepted","STATUS_CORRUPT_UNKNOWN","coura_ux.py",
  'value = json.loads(raw.decode("utf-8"), object_pairs_hook=courier.unique_object)',
  'value = json.loads(raw.decode("utf-8"))',1),
 ("start-hold","REMOTE_START_RETAINED","coura_remote.py",
  '"task_id": task_id, "disposition": "started",','"task_id": task_id, "disposition": "ignored",',1),
 ("refused-labelled-sent","REMOTE_RETURN_RETRY","coura_remote.py",
  'confirmed = receipt.status in {"STORED", "QUEUED", "THREAD_ACCEPTED"} or (',
  'confirmed = True or receipt.status in {"STORED", "QUEUED", "THREAD_ACCEPTED"} or (',1),
 ("result-resigned","REMOTE_RESULT_BYTES_RETAINED","coura_remote.py",
  '        if saved:\n','        if False and saved:\n',1),
 ("return-address-rebound","REMOTE_RESULT_BYTES_RETAINED","coura_remote.py",
  'if item.get("recipient") != address or item.get("key_id") != key_id:',
  'if item.get("key_id") != key_id:',1),
 ("conflict-labelled-repeat","REMOTE_CONFLICT_RETAINED","coura_remote.py",
  '"conflict": not same,','"conflict": False,',1),
 ("conflict-bytes-lost","REMOTE_CONFLICT_RETAINED","coura_remote.py",
  '"payload_sha256": digest, "payload_b64": base64.b64encode(payload).decode("ascii"),',
  '"payload_sha256": digest, "payload_b64": prior["payload_b64"],',1),
 ("id-only-peer-slot","PAIR_FULL_ADDRESS","coura_pair.py",
  'return address.client + ":" + address.identity','return address.identity',1),
 ("identity-key-accepted","PAIR_POINT_VALIDITY","coura_pair.py",
  'if account == (0, 1) or _scale(account, L) != (0, 1):',
  'if False:',1),
 ("noncanonical-identity","PAIR_POINT_VALIDITY","coura_pair.py",
  'if x == 0 and sign:','if False and x == 0 and sign:',1),
 ("non-atomic-state","PAIR_ATOMIC_STATE","coura_pair.py",
  '            stream.write(data)\n','            stream.write(data)\n            Path(path).write_bytes(data)\n',1),
 ("id-only-status","STATUS_RECIPIENT_BOUND","coura_ux.py",
  'slot = (row.get("client"), row.get("id"), key)','slot = (row.get("client"), "shared-id", key)',1),
 ("old-hold-hidden","STATUS_RECIPIENT_BOUND","coura_ux.py",
  'latest[slot] = courier.held_outcome(rows, target, slot[2]) or rows[-1]',
  'latest[slot] = rows[-1]',1),
 ("corrupt-skipped","STATUS_CORRUPT_UNKNOWN","coura_ux.py",
  '            except (UnicodeError, ValueError, courier.Refused):\n                return rows, True',
  '            except (UnicodeError, ValueError, courier.Refused):\n                continue',1),
 ("output-limit-removed","REMOTE_CAPTURE_BOUND","coura_remote.py",
  'if max(len(stdout), len(stderr)) > courier.MAX_RECORD:',
  'if False and max(len(stdout), len(stderr)) > courier.MAX_RECORD:',1),
 ("candidates-hidden","OBSERVER_CANDIDATE_COVERAGE","observer.py",
  'pending_sources = pending_sources or index.db.execute(\n            "SELECT 1 FROM candidates LIMIT 1").fetchone() is not None',
  'pending_sources = pending_sources',1),
 ("body-always-copied","OBSERVER_BODY_COPY_SWITCH","observer.py",
  'include = bool(courier.observer.get("include_body"))','include = True',1),
 ("survive-key-format",None,"coura_remote.py",
  'result_key = "result-" + record["task_id"]',
  'result_key = "result-{}".format(record["task_id"])',1),
 ("body-copy-overfire","OBSERVER_BODY_COPY_SWITCH","observer.py",
  'include = bool(courier.observer.get("include_body"))','include = False',1),
]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def run(root,status_only=False):
    result=subprocess.run([sys.executable,"-B",str(root/"tests/check_state_boundaries.py"),
                           "--root",str(root)]+(["--status-only"] if status_only else []),cwd=root,capture_output=True,timeout=180)
    text=(result.stdout+result.stderr).decode("utf-8","replace")
    red=[line.split(" ",2)[1] for line in text.splitlines() if line.startswith("FAIL ")]
    errors=[line for line in text.splitlines() if line.startswith("ERROR ")]
    return result.returncode, red, errors, text
def main():
    parser=argparse.ArgumentParser();parser.add_argument("--out",required=True);parser.add_argument("--status-only",action="store_true")
    args=parser.parse_args();out=pathlib.Path(args.out);out.mkdir(parents=True,exist_ok=True)
    names=["courier.py","observer.py","coura_remote.py","coura_pair.py","coura_ux.py",
           "tests/check_remote.py","tests/check_state_boundaries.py"]
    before={name:sha(ROOT/name) for name in names}
    record={"status":"RUNNING","cases":[],"source_before":before,"started":time.time()}
    def save(): (out/"result.json").write_text(json.dumps(record,indent=2)+"\n")
    save()
    code,reds,errors,text=run(ROOT,args.status_only)
    (out/"baseline.log").write_text(text)
    if code or reds or errors:raise RuntimeError("BASELINE_NOT_GREEN:"+str(reds)+str(errors))
    selected=[m for m in MUTATIONS if not args.status_only or (m[1] or "").startswith("STATUS_")]
    for slug,expected,name,old,new,count in selected:
        with tempfile.TemporaryDirectory() as folder:
            copy=pathlib.Path(folder)/"source"
            for item in names:
                dest=copy/item;dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(ROOT/item,dest)
            path=copy/name;source=path.read_text()
            if source.count(old)!=count:raise RuntimeError("MUTATION_ANCHOR:"+slug+":"+str(source.count(old)))
            path.write_text(source.replace(old,new),encoding="utf-8")
            code,reds,errors,text=run(copy,args.status_only)
            (out/(slug+".log")).write_text(text)
            good=(code==0 and not reds and not errors) if expected is None else (
                code==1 and bool(reds) and reds[0]==expected and not errors)
            item={"name":slug,"expected_first":expected,"first_red":reds[0] if reds else None,
                  "exit":code,"errors":errors,"status":"PASS" if good else "FAIL"}
            record["cases"].append(item);save()
            print(("PASS " if good else "FAIL ")+slug+" first="+str(item["first_red"]),flush=True)
            if not good:raise RuntimeError("MUTATION_FIRST_RED:"+slug)
    after={name:sha(ROOT/name) for name in names}
    if after!=before:raise RuntimeError("SOURCE_CHANGED")
    record.update(status="PASS",source_after=after,finished=time.time())
    save();print("STATE_MUTATIONS PASS "+str(len(selected)),flush=True)
    return 0
if __name__=="__main__":raise SystemExit(main())
