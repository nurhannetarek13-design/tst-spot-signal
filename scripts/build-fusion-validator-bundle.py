import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("validation/fusion")
MANIFEST = ROOT / "candidate-manifest.json"
SOURCES = {
    "freqtrade": "freqtrade-latest.json",
    "jesse": "jesse-latest.json",
    "vectorbt": "vectorbt-candidate-latest.json",
    "nautilus": "nautilus-latest.json",
    "forward": "forward-latest.json",
}

manifest = json.loads(MANIFEST.read_text())
expected_id = manifest.get("candidateId")
expected_fp = manifest.get("candidateFingerprint")

reports = {}
for name, filename in SOURCES.items():
    path = ROOT / filename
    raw = json.loads(path.read_text())
    row = dict(raw)
    id_match = bool(expected_id) and row.get("candidateId") == expected_id
    fp_match = bool(expected_fp) and row.get("candidateFingerprint") == expected_fp
    candidate_match = id_match and fp_match

    # Never let a stale validator remain "usable" simply because the artifact
    # was internally valid for an older candidate. The bundle is canonical for
    # exactly one manifest candidate at a time.
    row["candidateIdMatch"] = id_match
    row["candidateFingerprintMatch"] = fp_match
    row["candidateMatch"] = candidate_match
    row["reportedUsable"] = raw.get("usable")
    row["usable"] = bool(candidate_match and raw.get("usable", True))
    if not candidate_match:
        row["bundleStatus"] = "STALE_CANDIDATE_ARTIFACT"
        row["usable"] = False
    else:
        row["bundleStatus"] = "CURRENT_CANDIDATE"
    reports[name] = row

all_current = bool(expected_id and expected_fp) and all(v.get("candidateMatch") is True for v in reports.values())

bundle = {
    "bundleId": "TST_FUSION_VALIDATORS_BUNDLE_V2",
    "authorization": "RESEARCH_ONLY",
    "liveTrading": False,
    "candidateId": expected_id,
    "candidateFingerprint": expected_fp,
    "allValidatorsCurrentCandidate": all_current,
    "generatedAt": datetime.now(timezone.utc).isoformat(),
    "reports": reports,
}

(ROOT / "validators-bundle-latest.json").write_text(
    json.dumps(bundle, indent=2, sort_keys=True) + "\n"
)
