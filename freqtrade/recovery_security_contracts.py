"""Disaster-recovery snapshots and schema/security contracts.

Backups are local artifacts intended for replication by the deployment platform.
Restore is explicit and checksum-verified; no automatic live authorization.
"""
from __future__ import annotations
from pathlib import Path
import hashlib,hmac,json,os,shutil,time

CRITICAL=("tst_live_positions.json","tst_execution_reservations.json","tst_execution_ledger.json","tst_trade_events.jsonl")
def snapshot(data_dir="/data", backup_dir="/data/dr_backups"):
    src=Path(data_dir); root=Path(backup_dir)/time.strftime("%Y%m%dT%H%M%SZ",time.gmtime()); root.mkdir(parents=True,exist_ok=False)
    manifest={"created_at":time.time(),"files":{},"rpo_target_sec":300,"rto_target_sec":900}
    for name in CRITICAL:
        p=src/name
        if not p.exists(): continue
        dst=root/name; shutil.copy2(p,dst); raw=dst.read_bytes()
        manifest["files"][name]={"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw)}
    (root/"manifest.json").write_text(json.dumps(manifest,separators=(",",":")),encoding="utf-8")
    return {"path":str(root),"manifest":manifest,"requires_offsite_replication":True}

def verify_snapshot(path):
    root=Path(path); m=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    bad=[]
    for name,meta in m.get("files",{}).items():
        p=root/name
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest()!=meta.get("sha256"): bad.append(name)
    return {"ok":not bad,"bad_files":bad,"may_authorize_live":False}

def validate_binance_payload(payload, required):
    if not isinstance(payload,dict):return {"ok":False,"errors":["NOT_OBJECT"]}
    errors=[]
    for key,typ in (required or {}).items():
        if key not in payload: errors.append("MISSING:"+key)
        elif typ and not isinstance(payload[key],typ): errors.append("TYPE:"+key)
    return {"ok":not errors,"errors":errors,"fail_closed":bool(errors)}

def verify_signed_webhook(raw:bytes, *, signature:str, timestamp:int, nonce:str, secret:str, seen_nonces:set, now=None, max_age_sec=30):
    now=int(now or time.time())
    if not nonce or nonce in seen_nonces:return {"ok":False,"reason":"REPLAY"}
    if abs(now-int(timestamp))>int(max_age_sec):return {"ok":False,"reason":"STALE"}
    expected=hmac.new(secret.encode(),str(timestamp).encode()+b"." + nonce.encode()+b"." + raw,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,str(signature)):return {"ok":False,"reason":"BAD_SIGNATURE"}
    return {"ok":True,"nonce":nonce,"audit_required":True}
