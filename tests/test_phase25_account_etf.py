import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from quant_assistant.config import ETF_POOL
from quant_assistant.dashboard import generate_dashboard
from quant_assistant.data.fetcher import DataFetcher
from quant_assistant.models import Market
from quant_assistant.portfolio.holdings import PortfolioManager
from quant_assistant.portfolio.risk_engine import RiskEngine
from quant_assistant.trading.service import TradingService

from test_phase1_trading import portfolio_data


class AccountEtfSeparationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.portfolio_path = root / "portfolio.json"
        self.database_path = root / "trading.sqlite3"
        data = portfolio_data()
        import json
        self.portfolio_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        self.service = TradingService(self.database_path, self.portfolio_path)
        self.service.initialize_from_portfolio()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_non_strategy_etf_can_be_recorded_without_changing_strategy_pool(self):
        pool_before = copy.deepcopy(ETF_POOL)

        result = self.service.record_trade("BUY", "561550", 100, 1.5, fee=1.0)

        self.assertEqual(result.quantity, 100)
        self.assertEqual(result.execution["code"], "561550")
        self.assertNotIn("561550", ETF_POOL)
        self.assertEqual(ETF_POOL, pool_before)

    def test_non_strategy_opening_position_keeps_account_name(self):
        data = portfolio_data()
        data["positions"].append({
            "code": "159866", "name": "日经ETF工银", "market": "ETF",
            "shares": 1000, "cost_price": 1.752, "current_price": 1.752,
            "sector": "海外权益", "last_updated": None,
            "pe": 0.0, "pb": 0.0, "roe": 0.0, "market_cap": 0.0,
        })
        import json
        other_root = Path(self.temp_dir.name) / "other"
        other_root.mkdir()
        portfolio_path = other_root / "portfolio.json"
        portfolio_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        service = TradingService(other_root / "trading.sqlite3", portfolio_path)
        service.initialize_from_portfolio()

        result = service.record_trade("BUY", "159866", 100, 1.8)
        projected = service.rebuild_portfolio()
        position = next(item for item in projected["positions"] if item["code"] == "159866")

        self.assertEqual(result.quantity, 1100)
        self.assertEqual(position["name"], "日经ETF工银")
        self.assertNotIn("159866", ETF_POOL)

    def test_invalid_security_code_is_rejected_without_writes(self):
        before = self.portfolio_path.read_bytes()

        with self.assertRaisesRegex(ValueError, "STOCK代码格式无效"):
            self.service.record_trade("BUY", "123456", 100, 10.0)

        self.assertEqual(self.service.recent_executions(), [])
        self.assertEqual(self.portfolio_path.read_bytes(), before)

    def test_missing_price_is_reported_without_false_stop_loss(self):
        manager = PortfolioManager(self.portfolio_path)
        manager.positions[0].current_price = 0.0

        alerts = RiskEngine().run_all_checks(manager)

        matching = [item for item in alerts if item.stock_code == "510300"]
        self.assertTrue(any(item.rule_name == "行情数据不可用" for item in matching))
        self.assertFalse(any(item.rule_name == "个股止损线" for item in matching))
        self.assertFalse(any(item.rule_name == "总亏损限制" for item in alerts))

    def test_etf_history_falls_back_to_sina_and_normalizes_columns(self):
        sina = pd.DataFrame({
            "date": ["2026-09-10", "2026-09-11"],
            "open": [1.0, 1.1], "high": [1.2, 1.2], "low": [0.9, 1.0],
            "close": [1.1, 1.15], "volume": [1000, 1200], "amount": [1100, 1380],
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            fetcher = DataFetcher(Path(temp_dir))
            fetcher.max_retries = 0
            with mock.patch.object(fetcher, "_maybe_refresh_trade_calendar"), \
                    mock.patch("quant_assistant.data.fetcher.ak.fund_etf_hist_em", return_value=None), \
                    mock.patch("quant_assistant.data.fetcher.ak.fund_etf_hist_sina", return_value=sina), \
                    mock.patch("quant_assistant.data.fetcher.time.sleep"):
                frame = fetcher.fetch_hist("561550", Market.ETF, days=120)

        self.assertEqual(list(frame["收盘"]), [1.1, 1.15])
        self.assertIn("涨跌幅", frame.columns)

    def test_dashboard_marks_missing_price_as_unavailable(self):
        manager = PortfolioManager(self.portfolio_path)
        manager.positions[0].current_price = 0.0
        output = Path(self.temp_dir.name) / "dashboard.html"

        generate_dashboard(manager, output_path=output)

        html = output.read_text(encoding="utf-8")
        self.assertIn("已定价总资产", html)
        self.assertIn('"price_available": false', html)


if __name__ == "__main__":
    unittest.main()
