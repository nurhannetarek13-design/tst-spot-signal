"""Deterministic event ordering, gap detection, replay, and API budgets."""
from __future__ import annotations
from dataclasses import dataclass,field
from collections import defaultdict
import time

@dataclass
class SequenceGuard:
    last_seq: dict[str,int]=field(default_factory=dict)
    desynced: set[str]=field(default_factory=set)
    def accept(self,stream:str,seq:int,prev_seq:int|None=None):
        seq=int(seq); last=self.last_seq.get(stream)
        if stream in self.desynced:
            return {"action":"RESYNC_REQUIRED","accepted":False}
        if last is not None and seq<=last:
            return {"action":"DROP_OLD","accepted":False,"last_seq":last}
        if last is not None and prev_seq is not None and int(prev_seq)!=last:
            self.desynced.add(stream)
            return {"action":"GAP_RESYNC","accepted":False,"last_seq":last,"prev_seq":int(prev_seq),"seq":seq}
        self.last_seq[stream]=seq
        return {"action":"APPLY","accepted":True,"seq":seq}
    def resync(self,stream:str,snapshot_seq:int):
        self.last_seq[stream]=int(snapshot_seq); self.desynced.discard(stream)

def deterministic_replay(events, handler):
    """Preserve source order for equal timestamps; never invent event priority."""
    indexed=list(enumerate(events or []))
    ordered=sorted(indexed,key=lambda x:(int(x[1].get("ts_ms") or 0),x[0]))
    trace=[]
    for source_index,event in ordered:
        result=handler(dict(event))
        trace.append({"source_index":source_index,"ts_ms":int(event.get("ts_ms") or 0),
                      "type":event.get("type"),"result":result})
    return trace

class RateLimitBudgeter:
    """Local proactive budget. Critical reconciliation/protection outrank analytics."""
    PRIORITY={"PROTECTION":0,"RECONCILIATION":1,"ORDER":2,"USER_STREAM":3,"MARKET_DATA":4,"ANALYTICS":5}
    def __init__(self,capacity=100,window_sec=60,reserve_critical=20,clock=time.monotonic):
        self.capacity=int(capacity); self.window=float(window_sec); self.reserve=int(reserve_critical)
        self.clock=clock; self.used=defaultdict(list)
    def _prune(self,now):
        for k in list(self.used):
            self.used[k]=[t for t in self.used[k] if now-t<self.window]
    def allow(self,module:str,cost=1):
        now=self.clock(); self._prune(now); cost=int(cost)
        total=sum(len(v) for v in self.used.values())
        priority=self.PRIORITY.get(str(module).upper(),99)
        ceiling=self.capacity if priority<=2 else max(0,self.capacity-self.reserve)
        if cost<=0 or total+cost>ceiling:
            return False
        self.used[str(module).upper()].extend([now]*cost); return True
    def snapshot(self):
        now=self.clock(); self._prune(now)
        return {"capacity":self.capacity,"used":sum(len(v) for v in self.used.values()),
                "reserve_critical":self.reserve,"by_module":{k:len(v) for k,v in self.used.items()}}
