"""Production runtime hardening primitives. Fail closed; never authorizes live trading."""
from __future__ import annotations
import hashlib, json, os, time, uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DATA=Path(os.getenv("TST_RUNTIME_HARDENING_DIR","/data/runtime_hardening"))
DATA.mkdir(parents=True,exist_ok=True)

def _atomic(path:Path,payload:dict):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(payload,separators=(",",":"),sort_keys=True),encoding="utf-8")
    os.replace(tmp,path)

@dataclass
class LeaderLease:
    owner:str; token:int; expires_at:float
    @property
    def active(self): return self.expires_at>time.time()

class ExecutionOwnership:
    """Single executor lease with monotonically increasing fencing token."""
    def __init__(self,path:Path|None=None,ttl:float=30):
        self.path=path or DATA/"executor_lease.json"; self.ttl=ttl
    def acquire(self,owner:str,now:float|None=None):
        now=time.time() if now is None else now
        cur={}
        try: cur=json.loads(self.path.read_text())
        except Exception: pass
        if float(cur.get("expires_at",0))>now and cur.get("owner")!=owner:
            return {"ok":False,"status":"SPLIT_BRAIN_BLOCK","current_owner":cur.get("owner")}
        token=int(cur.get("token",0))+1
        row={"owner":owner,"token":token,"expires_at":now+self.ttl}
        _atomic(self.path,row); return {"ok":True,"status":"LEADER","fencing_token":token}
    def validate(self,owner:str,token:int,now:float|None=None):
        now=time.time() if now is None else now
        try: cur=json.loads(self.path.read_text())
        except Exception: return False
        return cur.get("owner")==owner and int(cur.get("token",0))==int(token) and float(cur.get("expires_at",0))>now

class AtomicCapital:
    """File-lock backed reservation ledger; one account-level source of truth."""
    def __init__(self,path:Path|None=None): self.path=path or DATA/"capital.json"
    def reserve(self,reservation_id:str,amount:float,free_quote:float):
        import fcntl
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.open("a+",encoding="utf-8") as f:
            fcntl.flock(f,fcntl.LOCK_EX); f.seek(0)
            try: s=json.load(f)
            except Exception: s={"reservations":{}}
            rs=s.setdefault("reservations",{})
            if reservation_id in rs: return {"ok":True,"status":"IDEMPOTENT","reserved":rs[reservation_id]}
            used=sum(float(v) for v in rs.values())
            if amount<=0 or used+amount>free_quote: return {"ok":False,"status":"CAPITAL_UNAVAILABLE","available":max(0,free_quote-used)}
            rs[reservation_id]=amount; f.seek(0); f.truncate(); json.dump(s,f,separators=(",",":")); f.flush(); os.fsync(f.fileno())
            return {"ok":True,"status":"RESERVED","reserved":amount}
    def release(self,reservation_id:str):
        import fcntl
        with self.path.open("a+",encoding="utf-8") as f:
            fcntl.flock(f,fcntl.LOCK_EX); f.seek(0)
            try:s=json.load(f)
            except Exception:s={"reservations":{}}
            value=s.setdefault("reservations",{}).pop(reservation_id,None)
            f.seek(0);f.truncate();json.dump(s,f,separators=(",",":"));f.flush();os.fsync(f.fileno())
            return value

READINESS_STEPS=("reconcile","l2_synced","indicators_warm","risk_loaded","metadata_current","clock_ok","quote_asset_ok")
def startup_readiness(checks:dict):
    missing=[x for x in READINESS_STEPS if checks.get(x) is not True]
    return {"ready":not missing,"status":"READY" if not missing else "NOT_READY","missing":missing}

def shutdown_plan(*,new_entries_stopped:bool,inflight:int,state_persisted:bool,protection_verified:bool,ownership_released:bool):
    pending=[]
    if not new_entries_stopped: pending.append("STOP_NEW_ENTRIES")
    if inflight: pending.append("SETTLE_INFLIGHT")
    if not state_persisted: pending.append("PERSIST_STATE")
    if not protection_verified: pending.append("VERIFY_PROTECTION")
    if not ownership_released: pending.append("RELEASE_OWNERSHIP")
    return {"safe_to_stop":not pending,"pending":pending}

def classify_account_activity(*,client_order_id:str|None,known_bot_ids:set[str],known_order:bool=False):
    if known_order or (client_order_id and client_order_id in known_bot_ids): return "BOT_OWNED"
    if client_order_id: return "EXTERNAL"
    return "UNKNOWN"

def resolve_strategy_conflict(intents:Iterable[dict]):
    """Account-level STP: at most one side per symbol; deterministic highest priority wins."""
    grouped={}
    for i in intents:
        grouped.setdefault(str(i["symbol"]).upper(),[]).append(i)
    accepted=[]; rejected=[]
    for symbol,rows in grouped.items():
        rows=sorted(rows,key=lambda x:(-float(x.get("priority",0)),str(x.get("strategy",""))))
        winner=rows[0]; accepted.append(winner)
        for r in rows[1:]:
            reason="SELF_TRADE_CONFLICT" if str(r.get("side")).upper()!=str(winner.get("side")).upper() else "DUPLICATE_STRATEGY_INTENT"
            rejected.append({**r,"reason":reason})
    return {"accepted":accepted,"rejected":rejected}

TERMINAL={"FILLED","CANCELED","EXPIRED","EXPIRED_IN_MATCH","REJECTED"}
KNOWN_ORDER_STATES={"NEW","PENDING_NEW","PARTIALLY_FILLED","FILLED","PENDING_CANCEL","CANCELED","REJECTED","EXPIRED","EXPIRED_IN_MATCH","PENDING_REPLACE"}
def order_semantics(status:str,*,executed_qty:float=0,orig_qty:float=0,prevented_qty:float=0,list_status:str|None=None):
    s=str(status).upper()
    if s not in KNOWN_ORDER_STATES: return {"known":False,"terminal":False,"status":"UNKNOWN_EXCHANGE_STATE","block":True}
    residual=max(0,float(orig_qty)-float(executed_qty)-float(prevented_qty))
    return {"known":True,"terminal":s in TERMINAL,"status":s,"residual_qty":residual,"list_status":list_status,"block":False}

def protection_effectiveness(*,protected_qty:float,position_qty:float,stop_triggered:bool,executed_qty:float,expected_stop:float|None=None,avg_fill:float|None=None):
    residual=max(0,float(position_qty)-float(executed_qty))
    slippage_bps=None
    if expected_stop and avg_fill: slippage_bps=(float(expected_stop)-float(avg_fill))/float(expected_stop)*10000
    effective=protected_qty>=position_qty and (not stop_triggered or residual<=1e-12)
    action="OK" if effective else ("EMERGENCY_EXIT" if stop_triggered else "PROTECTION_ONLY")
    return {"effective":effective,"residual_qty":residual,"stop_slippage_bps":slippage_bps,"action":action}

def market_sanity(*,last:float,mid:float,book_ticker:float,vwap:float,max_deviation_bps:float=35):
    vals=[float(last),float(mid),float(book_ticker),float(vwap)]
    if min(vals)<=0:return {"passed":False,"status":"BAD_MARKET_DATA","quarantine":True}
    center=sum(vals)/len(vals); dev=max(abs(v-center)/center*10000 for v in vals)
    return {"passed":dev<=max_deviation_bps,"status":"SANE" if dev<=max_deviation_bps else "ABNORMAL_TICK","max_deviation_bps":dev,"quarantine":dev>max_deviation_bps}

def infrastructure_backpressure(*,cpu_pct:float,ram_pct:float,disk_pct:float,db_pool_pct:float,backlog:int,worker_lag_ms:float):
    bad={"cpu":cpu_pct>=90,"ram":ram_pct>=90,"disk":disk_pct>=90,"db_pool":db_pool_pct>=90,"backlog":backlog>=1000,"worker_lag":worker_lag_ms>=1000}
    active=[k for k,v in bad.items() if v]
    return {"healthy":not active,"status":"OK" if not active else "BACKPRESSURE","reasons":active,"allow_new_entries":not active}

class DurableQueue:
    def __init__(self,path:Path|None=None): self.path=path or DATA/"events.jsonl"; self.dlq=self.path.with_name("dead_letter.jsonl")
    def append(self,event:dict):
        row={"event_id":event.get("event_id") or str(uuid.uuid4()),"attempts":int(event.get("attempts",0)),**event}
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.open("a",encoding="utf-8") as f:f.write(json.dumps(row,separators=(",",":"))+"\n")
        return row
    def fail(self,event:dict,max_attempts:int=3):
        row={**event,"attempts":int(event.get("attempts",0))+1}
        if row["attempts"]>=max_attempts:
            with self.dlq.open("a",encoding="utf-8") as f:f.write(json.dumps(row,separators=(",",":"))+"\n")
            return {"status":"DEAD_LETTER","event":row}
        self.append(row); return {"status":"RETRY","event":row}

def quote_asset_risk(*,usdt_usd:float,spread_bps:float,depth_usd:float):
    dev=abs(float(usdt_usd)-1.0)*10000
    if dev>=100 or spread_bps>=50 or depth_usd<100000: mode="PROTECTION_ONLY"
    elif dev>=30 or spread_bps>=20 or depth_usd<500000: mode="CAUTION"
    else: mode="NORMAL"
    return {"mode":mode,"depeg_bps":dev,"allow_new_entries":mode=="NORMAL"}

def clock_drift(*,local_ms:int,exchange_ms:int,warn_ms:int=300,block_ms:int=1000):
    drift=int(local_ms)-int(exchange_ms); a=abs(drift)
    status="BLOCK" if a>=block_ms else ("WARN" if a>=warn_ms else "OK")
    return {"drift_ms":drift,"status":status,"allow_microstructure_entries":status=="OK"}

def production_gate(*,readiness:dict,ownership_ok:bool,capital_ok:bool,market:dict,infra:dict,quote:dict,clock:dict):
    reasons=[]
    if not readiness.get("ready"):reasons.append("STARTUP_NOT_READY")
    if not ownership_ok:reasons.append("NO_EXECUTION_OWNERSHIP")
    if not capital_ok:reasons.append("CAPITAL_NOT_RESERVED")
    if not market.get("passed"):reasons.append("MARKET_SANITY")
    if not infra.get("allow_new_entries"):reasons.append("BACKPRESSURE")
    if not quote.get("allow_new_entries"):reasons.append("QUOTE_ASSET_RISK")
    if not clock.get("allow_microstructure_entries"):reasons.append("CLOCK_DRIFT")
    return {"allow_entry":not reasons,"status":"PASS" if not reasons else "BLOCK","reasons":reasons}
