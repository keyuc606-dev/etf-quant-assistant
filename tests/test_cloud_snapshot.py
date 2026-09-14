import base64
import json
import tempfile
import unittest
from pathlib import Path

from quant_assistant.cloud_snapshot import (
    build_snapshot,
    decode_snapshot,
    encode_snapshot,
    restore_snapshot,
)
from quant_assistant.trading.service import TradingService


class CloudSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_portfolio = self.root / "source-portfolio.json"
        self.source_database = self.root / "source.sqlite3"
        self.source_portfolio.write_text(
            json.dumps({
                "cash": 12345.67,
                "cash_flows": [{"date": "2026-09-01", "amount": 1000.0, "note": "入金"}],
                "positions": [{
                    "code": "510300",
                    "name": "沪深300ETF",
                    "market": "ETF",
                    "asset_type": "ETF",
                    "shares": 1200,
                    "cost_price": 4.125,
                    "current_price": 4.2,
                    "sector": "宽基",
                    "last_updated": "2026-09-14T15:00:00",
                    "pe": 0.0,
                    "pb": 0.0,
                    "roe": 0.0,
                    "market_cap": 0.0,
                }],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        self.service = TradingService(self.source_database, self.source_portfolio)
        self.service.initialize_from_portfolio()

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_restores_fresh_sqlite_and_projection(self):
        encoded = encode_snapshot(build_snapshot(self.service))
        target_database = self.root / "cloud" / "trading.sqlite3"
        target_portfolio = self.root / "cloud" / "portfolio.json"

        result = restore_snapshot(encoded, target_database, target_portfolio)

        self.assertEqual(result["positions"], 1)
        restored = TradingService(target_database, target_portfolio)
        self.assertTrue(restored.is_initialized())
        self.assertTrue(restored.reconcile()["ok"])
        state = restored.rebuild_portfolio()
        self.assertEqual(state["cash"], 12345.67)
        self.assertEqual(state["positions"][0]["shares"], 1200)
        self.assertEqual(state["positions"][0]["cost_price"], 4.125)

    def test_corrupted_payload_fails_checksum(self):
        snapshot = build_snapshot(self.service)
        snapshot["account"]["cash"] = 1.0
        encoded = base64.b64encode(
            json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        with self.assertRaisesRegex(ValueError, "校验失败"):
            decode_snapshot(encoded)

    def test_invalid_base64_does_not_create_account_files(self):
        target_database = self.root / "cloud" / "trading.sqlite3"
        target_portfolio = self.root / "cloud" / "portfolio.json"
        with self.assertRaisesRegex(ValueError, "Base64 JSON"):
            restore_snapshot("not-valid-%%%", target_database, target_portfolio)
        self.assertFalse(target_database.exists())
        self.assertFalse(target_portfolio.exists())

    def test_restore_refuses_to_overwrite_existing_account_data(self):
        encoded = encode_snapshot(build_snapshot(self.service))
        target_database = self.root / "existing.sqlite3"
        target_portfolio = self.root / "existing.json"
        target_portfolio.write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "拒绝覆盖"):
            restore_snapshot(encoded, target_database, target_portfolio)
        self.assertEqual(target_portfolio.read_text(encoding="utf-8"), "keep me")


if __name__ == "__main__":
    unittest.main()
