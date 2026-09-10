#!/usr/bin/env python3
"""September 2026 corrected-data wrapper for canonical valid-block L2 research."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import research.true_l2_round_v3 as v3

# Corrected collector generation: crypto-lob-stream >=0.9.2.
v3.DATASET='Goooddy/crypto-lob-stream'
v3.BASE='https://huggingface.co/datasets/Goooddy/crypto-lob-stream/resolve/main/'
v3.MONTH='2026-09'

if __name__=='__main__':
    v3.main()
