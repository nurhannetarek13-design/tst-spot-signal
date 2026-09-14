#!/usr/bin/env python3
"""Freeze Candidate Factory V1 to the predeclared 81-candidate search space.

3 baselines x 3 confirmations x 3 participation filters x 1 conservative
volatility gate x 3 exits = 81. The larger component library remains available
for later research rounds but is intentionally not searched in V1.
"""
import nnfx_crypto_candidate_factory_v1 as factory

factory.VOLATILITY = ["atr_normal"]

if __name__ == "__main__":
    factory.main()
