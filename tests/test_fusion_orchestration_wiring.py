from pathlib import Path

VALIDATORS=[
    ".github/workflows/candidate-vectorbt-validator.yml",
    ".github/workflows/fusion-freqtrade-validator.yml",
    ".github/workflows/fusion-jesse-validator.yml",
    ".github/workflows/nautilus-validator.yml",
    ".github/workflows/forward-paper-public-edges.yml",
]

def test_validators_explicitly_dispatch_fusion_gate():
    for path in VALIDATORS:
        code=Path(path).read_text()
        assert "actions: write" in code, path
        assert "gh workflow run fusion-gate.yml --ref main" in code, path

def test_fusion_gate_explicitly_dispatches_rotation():
    code=Path(".github/workflows/fusion-gate.yml").read_text()
    assert "actions: write" in code
    assert "gh workflow run candidate-rotation.yml --ref main" in code

if __name__=="__main__":
    test_validators_explicitly_dispatch_fusion_gate()
    test_fusion_gate_explicitly_dispatches_rotation()
    print("explicit fusion orchestration wiring: OK")
