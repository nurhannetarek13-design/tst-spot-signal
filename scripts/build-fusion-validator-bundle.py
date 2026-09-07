import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("validation/fusion")
SOURCES = {
    "freqtrade": "freqtrade-latest.json",
    "jesse": "jesse-latest.json",
    "vectorbt": "vectorbt-candidate-latest.json",
    "nautilus": "nautilus-latest.json",
    "forward": "forward-latest.json",
}

reports = {}
for name, filename in SOURCES.items():
    path = ROOT / filename
    reports[name] = json.loads(path.read_text())

bundle = {
    "bundleId": "TST_FUSION_VALIDATORS_BUNDLE_V1",
    "authorization": "RESEARCH_ONLY",
    "liveTrading": False,
    "generatedAt": datetime.now(timezone.utc).isoformat(),
    "reports": reports,
}

(ROOT / "validators-bundle-latest.json").write_text(
    json.dumps(bundle, indent=2, sort_keys=True) + "\n"
)
