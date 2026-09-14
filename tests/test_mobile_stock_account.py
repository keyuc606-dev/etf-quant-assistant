import datetime
import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from quant_assistant.analysis.indicators import add_all_indicators
from quant_assistant.config import ETF_POOL, TARGET_WEIGHTS
from quant_assistant.data.news import NewsDataService
from quant_assistant.models import Market, StockPosition
from quant_assistant.news.analysis import build_theme_observations
from quant_assistant.news.themes import STOCK_THEME_MAP
from quant_assistant.pipeline import run_daily_pipeline
from quant_assistant.portfolio.account_report import generate_account_reports
from quant_assistant.portfolio.holdings import PortfolioManager
from quant_assistant.trading.service import TradingService
from quant_assistant.trading.sqlite_repository import SQLiteTradingRepository


NOW = datetime.datetime(2026, 9, 14, 12, 0, tzinfo=datetime.timezone.utc)


def price_frame(last_price=20.0):
    dates = pd.date_range("2026-05-01", periods=100, freq="B")
    closes = [10.0 + index * (last_price - 10.0) / 99 for index in range(100)]
    return pd.DataFrame({
        "日期": dates, "开盘": closes, "收盘": closes,
        "最高": [value * 1.01 for value in closes],
        "最低": [value * 0.99 for value in closes],
        "成交量": [100000 + index for index in range(100)],
        "涨跌幅": pd.Series(closes).pct_change().fillna(0) * 100,
    })


class FakeMarketFetcher:
    next_price = 20.0

    def __init__(self):
        pass

    def fetch_hist(self, code, market, days=120):
        self.seen = (code, market, days)
        return price_frame(self.next_price)


class FakeStockNewsFetcher:
    def fetch_public_news_rss(self, _query):
        return None

    def fetch_stock_news_metadata(self, code):
        return [{
            "title": f"{STOCK_THEME_MAP[code]['name']}发布经营进展",
            "published_at": "2026-09-14T02:00:00+00:00",
            "source": "测试公开源",
            "url": "https://example.com/company-news",
        }]


class MobileStockAccountTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.portfolio = self.root / "portfolio.json"
        self.database = self.root / "trading.sqlite3"
        self.portfolio.write_text(json.dumps({
            "cash": 10000.0,
            "cash_flows": [],
            "positions": [{
                "code": "002698", "name": "博实股份", "market": "深圳",
                "asset_type": "STOCK", "shares": 100, "cost_price": 15.0,
                "current_price": 0.0, "sector": "工业自动化", "last_updated": None,
                "pe": 0.0, "pb": 0.0, "roe": 0.0, "market_cap": 0.0,
            }],
        }, ensure_ascii=False), encoding="utf-8")
        self.service = TradingService(self.database, self.portfolio)
        self.service.initialize_from_portfolio()

    def tearDown(self):
        self.temp.cleanup()

    def test_stock_opening_snapshot_keeps_asset_type(self):
        positions = self.service.repository.load_opening_positions()
        self.assertEqual(positions[0]["asset_type"], "STOCK")
        self.assertEqual(positions[0]["market"], "深圳")

    def test_stock_record_trade_updates_cash_quantity_and_cost(self):
        result = self.service.record_trade(
            "BUY", "002698", 100, 20.0, fee=5.0, asset_type="STOCK"
        )
        projected = self.service.rebuild_portfolio()
        position = projected["positions"][0]
        self.assertEqual(result.quantity, 200)
        self.assertAlmostEqual(result.average_cost, 17.525)
        self.assertEqual(position["asset_type"], "STOCK")
        self.assertAlmostEqual(result.cash, 7995.0)

    def test_stocks_are_not_added_to_strategy_etf_pool(self):
        pool = deepcopy(ETF_POOL)
        weights = deepcopy(TARGET_WEIGHTS)
        self.service.record_trade("BUY", "002698", 100, 20.0, asset_type="STOCK")
        self.assertEqual(ETF_POOL, pool)
        self.assertEqual(TARGET_WEIGHTS, weights)
        self.assertTrue(set(STOCK_THEME_MAP).isdisjoint(ETF_POOL))

    def test_stock_market_data_updates_without_trade_feedback(self):
        manager = PortfolioManager(self.portfolio)
        original_shares = manager.positions[0].shares
        with patch("quant_assistant.pipeline.DataFetcher", FakeMarketFetcher):
            FakeMarketFetcher.next_price = 20.0
            first = run_daily_pipeline(manager)
            FakeMarketFetcher.next_price = 22.0
            second = run_daily_pipeline(manager)
        self.assertEqual(manager.positions[0].shares, original_shares)
        self.assertAlmostEqual(manager.positions[0].current_price, 22.0)
        self.assertIn("002698", first["stock_data"])
        self.assertIn("002698", second["stock_data"])

    def test_stock_technical_indicators_are_available(self):
        frame = add_all_indicators(price_frame())
        required = {"MA5", "MA20", "DIF", "DEA", "MACD", "RSI14", "K", "D", "J", "ATR", "ATR_PCT"}
        self.assertTrue(required.issubset(frame.columns))
        self.assertTrue(frame.iloc[-1][list(required)].notna().all())

    def test_stock_company_news_and_theme_analysis(self):
        service = NewsDataService(self.root / "news.json", FakeStockNewsFetcher())
        bundle = service.fetch_for_instruments(["002698"], now=NOW)
        observations = build_theme_observations(["002698"], bundle["items"], NOW)
        self.assertEqual(bundle["items"][0]["matched_instruments"], ["002698"])
        self.assertEqual(observations["002698"]["recent_news_count"], 1)
        self.assertEqual(observations["002698"]["theme"], "工业自动化")

    def test_report_distinguishes_stock_and_etf(self):
        manager = PortfolioManager(self.root / "missing.json")
        manager.cash = 1000.0
        manager.positions = [
            StockPosition("002698", "博实股份", Market.A_SZ, 100, 15.0, 20.0,
                          asset_type="STOCK"),
            StockPosition("510300", "沪深300ETF", Market.ETF, 100, 4.0, 4.1,
                          asset_type="ETF"),
        ]
        frames = {position.code: add_all_indicators(price_frame(position.current_price))
                  for position in manager.positions}
        reports = generate_account_reports(manager, frames, report_dir=self.root)
        daily = reports["daily"].read_text(encoding="utf-8")
        self.assertIn("股票市值", daily)
        self.assertIn("ETF 市值", daily)
        self.assertIn("股票 1、ETF 1", daily)
        self.assertIn("原量化策略基准", daily)

    def test_schema_v1_is_migrated_to_asset_type_schema_v2(self):
        path = self.root / "legacy.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript("""
        CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES(1, '2026-01-01T00:00:00Z');
        CREATE TABLE opening_positions(
          code TEXT PRIMARY KEY, name TEXT NOT NULL, market TEXT NOT NULL,
          quantity INTEGER NOT NULL, cost_price REAL NOT NULL, current_price REAL NOT NULL,
          sector TEXT NOT NULL DEFAULT '', last_updated TEXT,
          pe REAL NOT NULL DEFAULT 0, pb REAL NOT NULL DEFAULT 0,
          roe REAL NOT NULL DEFAULT 0, market_cap REAL NOT NULL DEFAULT 0);
        CREATE TABLE executions(
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL UNIQUE,
          external_id TEXT, side TEXT NOT NULL, code TEXT NOT NULL, quantity INTEGER NOT NULL,
          price REAL NOT NULL, fee REAL NOT NULL, executed_at TEXT NOT NULL, source TEXT NOT NULL,
          related_plan_id TEXT, note TEXT, realized_pnl REAL, created_at TEXT NOT NULL);
        """)
        connection.close()
        repository = SQLiteTradingRepository(path)
        self.assertEqual(repository.get_schema_version(), 2)
        reopened = sqlite3.connect(path)
        try:
            opening_columns = {row[1] for row in reopened.execute("PRAGMA table_info(opening_positions)")}
            execution_columns = {row[1] for row in reopened.execute("PRAGMA table_info(executions)")}
        finally:
            reopened.close()
        self.assertIn("asset_type", opening_columns)
        self.assertIn("asset_type", execution_columns)


if __name__ == "__main__":
    unittest.main()
