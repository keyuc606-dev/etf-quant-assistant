import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quant_assistant.portfolio.holdings import PortfolioManager
from quant_assistant.trading.service import TradingService


def portfolio_data(cash=10000.0):
    return {
        "cash": cash,
        "cash_flows": [],
        "positions": [
            {
                "code": "510300",
                "name": "沪深300ETF",
                "market": "ETF",
                "shares": 1000,
                "cost_price": 4.0,
                "current_price": 4.2,
                "sector": "宽基指数",
                "last_updated": None,
                "pe": 0.0,
                "pb": 0.0,
                "roe": 0.0,
                "market_cap": 0.0,
            }
        ],
    }


class TradingServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.portfolio_path = root / "portfolio.json"
        self.database_path = root / "trading.sqlite3"
        self.portfolio_path.write_text(
            json.dumps(portfolio_data(), ensure_ascii=False), encoding="utf-8"
        )
        self.service = TradingService(self.database_path, self.portfolio_path)
        self.service.initialize_from_portfolio()

    def tearDown(self):
        self.temp_dir.cleanup()

    def load_portfolio(self):
        return json.loads(self.portfolio_path.read_text(encoding="utf-8"))

    def execution_count(self):
        connection = sqlite3.connect(self.database_path)
        try:
            return connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        finally:
            connection.close()

    def test_buy_new_etf(self):
        result = self.service.record_trade("BUY", "510500", 100, 5.0, fee=1.0)
        portfolio = self.load_portfolio()
        position = next(item for item in portfolio["positions"] if item["code"] == "510500")

        self.assertEqual(result.quantity, 100)
        self.assertAlmostEqual(result.cash, 9499.0)
        self.assertAlmostEqual(position["cost_price"], 5.01)

    def test_add_to_existing_etf(self):
        result = self.service.record_trade("BUY", "510300", 100, 6.0, fee=2.0)

        self.assertEqual(result.quantity, 1100)
        self.assertAlmostEqual(result.average_cost, (4000.0 + 602.0) / 1100)

    def test_partial_sell(self):
        result = self.service.record_trade("SELL", "510300", 100, 5.0, fee=1.0)

        self.assertEqual(result.quantity, 900)
        self.assertAlmostEqual(result.average_cost, 4.0)
        self.assertAlmostEqual(result.cash, 10499.0)

    def test_full_sell_removes_position(self):
        result = self.service.record_trade("SELL", "510300", 1000, 5.0, fee=1.0)
        codes = [item["code"] for item in self.load_portfolio()["positions"]]

        self.assertEqual(result.quantity, 0)
        self.assertNotIn("510300", codes)

    def test_insufficient_cash_rejects_whole_trade(self):
        before = self.portfolio_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "现金不足"):
            self.service.record_trade("BUY", "510500", 10000, 5.0, fee=1.0)

        self.assertEqual(self.execution_count(), 0)
        self.assertEqual(self.portfolio_path.read_bytes(), before)

    def test_oversell_rejects_whole_trade(self):
        before = self.portfolio_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "持仓不足"):
            self.service.record_trade("SELL", "510300", 1001, 5.0, fee=1.0)

        self.assertEqual(self.execution_count(), 0)
        self.assertEqual(self.portfolio_path.read_bytes(), before)

    def test_fee_reduces_cash_on_buy(self):
        result = self.service.record_trade("BUY", "510500", 100, 5.0, fee=7.0)

        self.assertAlmostEqual(result.cash, 9493.0)

    def test_buy_average_cost_includes_fee(self):
        result = self.service.record_trade("BUY", "510300", 100, 4.0, fee=11.0)

        self.assertAlmostEqual(result.average_cost, 4411.0 / 1100)

    def test_sell_realized_pnl_uses_original_average_cost_and_fee(self):
        result = self.service.record_trade("SELL", "510300", 100, 4.5, fee=3.0)

        self.assertAlmostEqual(result.execution["realized_pnl"], 47.0)

    def test_duplicate_external_id_is_idempotent(self):
        first = self.service.record_trade(
            "BUY", "510500", 100, 5.0, fee=1.0, external_id="broker-1"
        )
        second = self.service.record_trade(
            "BUY", "510500", 100, 5.0, fee=1.0, external_id="broker-1"
        )

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(self.execution_count(), 1)
        self.assertEqual(second.quantity, 100)
        self.assertAlmostEqual(second.cash, 9499.0)

    def test_duplicate_external_id_with_different_trade_is_rejected(self):
        self.service.record_trade(
            "BUY", "510500", 100, 5.0, external_id="broker-1"
        )
        with self.assertRaisesRegex(ValueError, "参数不一致"):
            self.service.record_trade(
                "BUY", "510500", 200, 5.0, external_id="broker-1"
            )
        self.assertEqual(self.execution_count(), 1)

    def test_projection_failure_rolls_back_sqlite_and_json(self):
        before = self.portfolio_path.read_bytes()
        with mock.patch.object(self.service, "_write_projection", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.service.record_trade("BUY", "510500", 100, 5.0)

        self.assertEqual(self.execution_count(), 0)
        self.assertEqual(self.portfolio_path.read_bytes(), before)

    def test_sqlite_rebuild_projects_to_portfolio_json_format(self):
        self.service.record_trade("BUY", "510500", 100, 5.0, fee=1.0)
        projected = self.service.rebuild_portfolio()

        self.assertIn("cash", projected)
        self.assertIn("cash_flows", projected)
        self.assertIn("positions", projected)
        for position in projected["positions"]:
            self.assertTrue({
                "code", "name", "market", "shares", "cost_price", "current_price",
                "sector", "last_updated", "pe", "pb", "roe", "market_cap",
            }.issubset(position))

    def test_reconcile_reports_ok_when_projection_matches(self):
        self.service.record_trade("BUY", "510500", 100, 5.0)

        self.assertTrue(self.service.reconcile()["ok"])

    def test_reconcile_allows_daily_market_price_updates(self):
        manager = PortfolioManager(self.portfolio_path)
        manager.update_price("510300", 4.8)

        self.assertTrue(self.service.reconcile()["ok"])

    def test_reconcile_detects_projection_mismatch_without_repair(self):
        data = self.load_portfolio()
        data["positions"][0]["shares"] = 999
        self.portfolio_path.write_text(json.dumps(data), encoding="utf-8")

        result = self.service.reconcile()

        self.assertFalse(result["ok"])
        self.assertTrue(any("数量不一致" in item for item in result["differences"]))
        self.assertEqual(self.load_portfolio()["positions"][0]["shares"], 999)

    def test_reconcile_repairs_only_with_explicit_flag(self):
        data = self.load_portfolio()
        data["cash"] = 1.0
        self.portfolio_path.write_text(json.dumps(data), encoding="utf-8")

        result = self.service.reconcile(repair=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["repaired"])
        self.assertAlmostEqual(self.load_portfolio()["cash"], 10000.0)

    def test_buy_and_sell_do_not_create_cash_events(self):
        self.service.record_trade("BUY", "510500", 100, 5.0)
        self.service.record_trade("SELL", "510500", 100, 5.1)
        connection = sqlite3.connect(self.database_path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM cash_events").fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(count, 0)
        self.assertEqual(self.load_portfolio()["cash_flows"], [])

    def test_projection_remains_compatible_with_portfolio_manager(self):
        self.service.record_trade("BUY", "510500", 100, 5.0)
        manager = PortfolioManager(self.portfolio_path)

        self.assertEqual(manager.get_position("510500").shares, 100)
        manager.update_price("510500", 5.2)
        self.assertAlmostEqual(PortfolioManager(self.portfolio_path).get_position("510500").current_price, 5.2)

    def test_initialization_creates_snapshot_without_fake_executions(self):
        connection = sqlite3.connect(self.database_path)
        try:
            executions = connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
            positions = connection.execute("SELECT COUNT(*) FROM opening_positions").fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(executions, 0)
        self.assertEqual(positions, 1)

    def test_invalid_trade_parameters_do_not_modify_data(self):
        before = self.portfolio_path.read_bytes()
        invalid = [
            ("BUY", "000001", 100, 4.0, 0.0),
            ("BUY", "510300", 0, 4.0, 0.0),
            ("BUY", "510300", 100, 0.0, 0.0),
            ("BUY", "510300", 100, 4.0, -1.0),
        ]
        for side, code, quantity, price, fee in invalid:
            with self.assertRaises(ValueError):
                self.service.record_trade(side, code, quantity, price, fee=fee)

        self.assertEqual(self.execution_count(), 0)
        self.assertEqual(self.portfolio_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
