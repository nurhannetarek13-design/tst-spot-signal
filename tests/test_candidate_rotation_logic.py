import datetime as dt
from scripts.candidate_rotation_lib import refresh_rejection, active_rejection_fingerprints

def test_refreshes_expired_duplicate_instead_of_reselecting_it():
    now=dt.datetime(2026,9,25,tzinfo=dt.timezone.utc)
    old={
        "candidateId":"PUBLIC_EDGE_LAB:HOLOUSDT:TS_MOMENTUM",
        "candidateFingerprint":"fp-holo",
        "rejectedAt":"2026-09-12T07:18:01+00:00",
        "hardFailValidators":["vectorbt","freqtrade"],
    }
    fresh={
        "candidateId":"PUBLIC_EDGE_LAB:HOLOUSDT:TS_MOMENTUM",
        "candidateFingerprint":"fp-holo",
        "rejectedAt":now.isoformat(),
        "hardFailValidators":["vectorbt","freqtrade","nautilus"],
    }
    rows=refresh_rejection([old],fresh)
    assert len(rows)==1
    assert rows[0]["rejectedAt"]==now.isoformat()
    assert rows[0]["hardFailValidators"]==["vectorbt","freqtrade","nautilus"]
    active=active_rejection_fingerprints(rows,7,now=now)
    assert active=={"fp-holo"}

def test_keeps_unrelated_rejections_and_appends_new_one():
    now=dt.datetime(2026,9,25,tzinfo=dt.timezone.utc)
    rows=refresh_rejection(
        [{"candidateFingerprint":"old","rejectedAt":"2026-09-24T00:00:00+00:00"}],
        {"candidateFingerprint":"new","rejectedAt":now.isoformat()},
    )
    assert [x["candidateFingerprint"] for x in rows]==["old","new"]
    assert active_rejection_fingerprints(rows,7,now=now)=={"old","new"}

if __name__=="__main__":
    test_refreshes_expired_duplicate_instead_of_reselecting_it()
    test_keeps_unrelated_rejections_and_appends_new_one()
    print("candidate rotation cooldown tests: OK")
