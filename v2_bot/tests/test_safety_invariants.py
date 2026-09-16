from pathlib import Path
import unittest


class SafetyInvariantTests(unittest.TestCase):
    def test_v2_contains_no_withdrawal_endpoint(self):
        root = Path(__file__).resolve().parents[1]
        forbidden = (
            "/sapi/v1/capital/withdraw",
            "withdraw/apply",
            "withdrawal",
        )
        offenders = []
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for token in forbidden:
                if token.lower() in text:
                    offenders.append(f"{path.name}:{token}")
        self.assertEqual(offenders, [])

    def test_engine_does_not_wire_private_executor(self):
        engine = Path(__file__).resolve().parents[1] / "engine.py"
        text = engine.read_text(encoding="utf-8")
        self.assertNotIn("ProtectedSpotExecutor", text)
        self.assertNotIn("BinanceSignedSpotClient", text)
        self.assertIn("LIVE_EXECUTION_LOCKED", text)


if __name__ == "__main__":
    unittest.main()
