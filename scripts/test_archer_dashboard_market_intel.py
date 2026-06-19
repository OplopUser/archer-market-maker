import unittest

from dashboard.server import parse_market_intel_payload


class ArcherDashboardMarketIntelTests(unittest.TestCase):
    def test_parse_market_intel_payload_accepts_archer_scoped_wrapper(self) -> None:
        payload = {
            "consumer": "archer",
            "allowed_source_set": ["binance", "manifest"],
            "excluded_source_set": ["bot_dashboard"],
            "signal": {
                "mode": "normal",
                "recommendation": {
                    "quote_enabled": True,
                    "fair_value": "80.1",
                    "spread_add_bps": "12.0",
                    "size_multiplier": "0.75",
                    "bid_size_multiplier": "0.5",
                    "ask_size_multiplier": "1.0",
                    "reasons": ["test"],
                },
            },
        }

        parsed = parse_market_intel_payload(
            payload, "http://market-intel:8790/api/signals/scoped/archer/sol_usdc"
        )

        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["consumer"], "archer")
        self.assertEqual(parsed["excluded_source_set"], ["bot_dashboard"])
        self.assertEqual(parsed["mode"], "normal")
        self.assertEqual(parsed["fair_value"], "80.1")


if __name__ == "__main__":
    unittest.main()
