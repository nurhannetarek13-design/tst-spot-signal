from __future__ import annotations

import json,os,tempfile,time
from pathlib import Path

PATH=Path(os.getenv('TST_EXECUTION_HEALTH_PATH','/data/tst_execution_health.json'))


def _atomic(row:dict)->None:
    PATH.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=PATH.name+'.',dir=str(PATH.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(row,f,separators=(',',':')); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,PATH)
    finally:
        try:
            if os.path.exists(tmp): os.unlink(tmp)
        except Exception: pass


def mark_ok(**extra)->None:
    now=time.time(); old=snapshot()
    _atomic({'ok':True,'last_ok_at':now,'last_check_at':now,'last_error':None,'consecutive_errors':0,
             'previous_last_ok_at':old.get('last_ok_at'),**extra})


def mark_error(error:str,**extra)->None:
    now=time.time(); old=snapshot()
    _atomic({'ok':False,'last_ok_at':old.get('last_ok_at'),'last_check_at':now,
             'last_error':str(error)[:240],'consecutive_errors':int(old.get('consecutive_errors') or 0)+1,**extra})


def snapshot()->dict:
    try:
        row=json.loads(PATH.read_text(encoding='utf-8'))
        return row if isinstance(row,dict) else {}
    except Exception: return {}


def healthy(max_stale_sec:float=180.0,max_errors:int=2)->tuple[bool,str]:
    row=snapshot(); now=time.time()
    if not row: return False,'execution-health-missing'
    last=float(row.get('last_ok_at') or 0); errors=int(row.get('consecutive_errors') or 0)
    if last<=0 or now-last>max_stale_sec: return False,f'execution-health-stale-{int(now-last) if last else 999999}s'
    if errors>=max_errors: return False,f'execution-api-errors-{errors}'
    return True,'execution-health-ok'
