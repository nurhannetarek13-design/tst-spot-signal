#!/usr/bin/env bash
set -euo pipefail

USER_DATA="/freqtrade/user_data"
STRATEGY_DIR="$USER_DATA/strategies"
NFI_DIR="$USER_DATA/nfi-pinned"
PROTECTED_FILE="$STRATEGY_DIR/NFIProtectedX7.py"
BRIDGE_FILE="/freqtrade/telegram_signal_bridge.py"
CONFIG_FILE="$USER_DATA/config_nfi_dryrun.json"
NFI_COMMIT="da50440bd5f8a829af9dc768822fa31cfe4b7867"
NFI_ARCHIVE="https://github.com/iterativv/NostalgiaForInfinity/archive/${NFI_COMMIT}.tar.gz"
PROTECTED_URL="https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/freqtrade/user_data/strategies/NFIProtectedX7.py"
BRIDGE_URL="https://raw.githubusercontent.com/nurhannetarek13-design/tst-spot-signal/main/freqtrade/telegram_signal_bridge.py"

mkdir -p "$STRATEGY_DIR" "$USER_DATA/logs" "$USER_DATA/signals"

echo "[ready-bot] fetching complete pinned NostalgiaForInfinity @ ${NFI_COMMIT}"
python - <<PY
from pathlib import Path
from urllib.request import Request, urlopen
import io, os, shutil, tarfile

nfi_dir = Path("${NFI_DIR}")
archive_url = "${NFI_ARCHIVE}"
req = Request(archive_url, headers={"User-Agent": "tst-ready-bot/2.0"})
with urlopen(req, timeout=120) as r:
    data = r.read()
if len(data) < 1_000_000:
    raise SystemExit(f"NFI archive download too small: {len(data)}")

tmp = Path("/tmp/nfi-ready-extract")
shutil.rmtree(tmp, ignore_errors=True)
tmp.mkdir(parents=True)
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
    members = tf.getmembers()
    for m in members:
        target = (tmp / m.name).resolve()
        if not str(target).startswith(str(tmp.resolve()) + os.sep):
            raise SystemExit("Unsafe archive member")
    tf.extractall(tmp)
roots = [p for p in tmp.iterdir() if p.is_dir()]
if len(roots) != 1:
    raise SystemExit("Unexpected NFI archive layout")
root = roots[0]
x7 = root / "NostalgiaForInfinityX7.py"
legacy = root / "legacy" / "NostalgiaForInfinityNextGen.py"
if not x7.exists() or x7.stat().st_size < 1_000_000 or not legacy.exists():
    raise SystemExit("Pinned NFI package validation failed")
shutil.rmtree(nfi_dir, ignore_errors=True)
shutil.move(str(root), str(nfi_dir))
shutil.rmtree(tmp, ignore_errors=True)
print(f"[ready-bot] NFI package ready: {nfi_dir}")

def fetch(url: str, out: str, min_size: int, needle: bytes):
    req = Request(url, headers={"User-Agent": "tst-ready-bot/2.0"})
    with urlopen(req, timeout=60) as r:
        payload = r.read()
    if len(payload) < min_size or needle not in payload:
        raise SystemExit(f"Download failed validation: {url}")
    Path(out).write_bytes(payload)
    print(f"[ready-bot] ready: {out} ({len(payload)} bytes)")

fetch("${PROTECTED_URL}", "${PROTECTED_FILE}", 500, b"class NFIProtectedX7")
fetch("${BRIDGE_URL}", "${BRIDGE_FILE}", 1000, b"def send_opportunity")
PY

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "[ready-bot] missing config: $CONFIG_FILE" >&2
  exit 2
fi

export PYTHONPATH="$NFI_DIR:${PYTHONPATH:-}"

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  echo "[ready-bot] TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required" >&2
  exit 3
fi

# Signal bridge only. Market-wide scanner is intentionally separated from this
# service so NFI has the full Railway memory budget.
python -u "$BRIDGE_FILE" &
BRIDGE_PID=$!
trap 'kill "$BRIDGE_PID" 2>/dev/null || true' EXIT

exec freqtrade trade \
  --logfile "$USER_DATA/logs/freqtrade.log" \
  --db-url "sqlite:///$USER_DATA/tradesv3.sqlite" \
  --config "$CONFIG_FILE" \
  --strategy-path "$STRATEGY_DIR" \
  --strategy NFIProtectedX7
