import datetime as dt

def refresh_rejection(rows, record, max_rows=100):
    rows=list(rows or [])
    fp=record.get("candidateFingerprint")
    found=False
    for i,old in enumerate(rows):
        if fp and old.get("candidateFingerprint")==fp:
            # Preserve any historical metadata not present in the fresh record,
            # but always refresh the failure evidence and timestamp.
            rows[i]={**old,**record}
            found=True
            break
    if not found:
        rows.append(dict(record))
    return rows[-max_rows:]

def active_rejection_fingerprints(rows, ttl_days, now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    cut=now-dt.timedelta(days=int(ttl_days))
    active=set()
    for x in rows or []:
        try:
            ts=dt.datetime.fromisoformat(str(x["rejectedAt"]).replace("Z","+00:00"))
            if ts>=cut and x.get("candidateFingerprint"):
                active.add(x["candidateFingerprint"])
        except Exception:
            pass
    return active
