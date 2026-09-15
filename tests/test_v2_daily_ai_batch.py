import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from quant_assistant.analysis.account_advice import OpenAIAdviceProvider, build_rule_advice
from quant_assistant.analysis.indicators import add_all_indicators
from quant_assistant.cloud_snapshot import build_snapshot, encode_snapshot
from quant_assistant.cloud_state import CloudStateStore, new_cloud_state
from quant_assistant.data.fetcher import DataFetcher
from quant_assistant.models import Market, StockPosition
from quant_assistant.portfolio.holdings import PortfolioManager
from quant_assistant.portfolio.account_report import _position_views
from quant_assistant.trading.service import TradingService
from quant_assistant.trading.telegram_feedback import TelegramTradeFeedback, parse_trade_commands


def make_service(root: Path, cash=20_000.0):
    portfolio = {
        "cash": cash, "cash_flows": [],
        "positions": [{
            "code": "600104", "name": "上汽集团", "market": "上海", "asset_type": "STOCK",
            "shares": 500, "cost_price": 15.0, "current_price": 18.0, "sector": "汽车",
            "last_updated": None, "pe": 0.0, "pb": 0.0, "roe": 0.0, "market_cap": 0.0,
        }],
    }
    path = root / "portfolio.json"
    path.write_text(json.dumps(portfolio, ensure_ascii=False), encoding="utf-8")
    service = TradingService(root / "trading.sqlite3", path)
    service.initialize_from_portfolio()
    return service


class BatchTradeV2Test(unittest.TestCase):
    def test_parses_multiline_batch(self):
        items = parse_trade_commands("买入 600104 300股 18.72\n卖出 002074 500股 39.10\n买入 512890 1000份 1.23 手续费5")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[2].code, "512890")
        self.assertEqual(items[2].fee, 5.0)

    def test_512890_first_buy_is_identified_and_persists_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            resolver = lambda code: {"code": code, "name": "红利低波ETF", "asset_type": "ETF", "market": "ETF", "exchange": "上海", "reference_price": 1.23}
            feedback = TelegramTradeFeedback(service, resolver)
            pending = feedback.handle("买入 512890 1000份 1.23 手续费5", 7, "123", None)
            self.assertIn("红利低波ETF", pending.response)
            confirmed = feedback.handle("确认", 8, "123", pending.pending)
            self.assertTrue(confirmed.account_changed)
            position = next(item for item in service.rebuild_portfolio()["positions"] if item["code"] == "512890")
            self.assertEqual(position["asset_type"], "ETF")
            self.assertEqual(position["name"], "红利低波ETF")

    def test_invalid_second_trade_rejects_whole_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp), cash=1000.0)
            feedback = TelegramTradeFeedback(service)
            before = service.rebuild_portfolio()
            result = feedback.handle("卖出 600104 100股 18.00\n买入 600104 1000股 18.00", 1, "123", None)
            self.assertIsNone(result.pending)
            self.assertIn("第2笔", result.response)
            self.assertEqual(service.rebuild_portfolio(), before)
            self.assertEqual(service.recent_executions(), [])

    def test_cancel_discards_whole_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            feedback = TelegramTradeFeedback(service)
            before = service.rebuild_portfolio()
            pending = feedback.handle("买入 600104 10股 18\n卖出 600104 5股 18", 2, "123", None)
            cancelled = feedback.handle("取消", 3, "123", pending.pending)
            self.assertFalse(cancelled.account_changed)
            self.assertEqual(service.rebuild_portfolio(), before)


class SecurityIdentificationV2Test(unittest.TestCase):
    def test_fetcher_identifies_512890_as_etf(self):
        fetcher = DataFetcher(Path(tempfile.mkdtemp()))
        table = pd.DataFrame({"代码": ["512890"], "名称": ["红利低波ETF"], "最新价": [1.234]})
        with mock.patch.object(fetcher, "_spot_table", side_effect=lambda key: table if key == "ETF" else pd.DataFrame()):
            item = fetcher.identify_security("512890")
        self.assertEqual(item["asset_type"], "ETF")
        self.assertEqual(item["exchange"], "上海")

    def test_unknown_or_suspended_security_is_rejected(self):
        fetcher = DataFetcher(Path(tempfile.mkdtemp()))
        table = pd.DataFrame({"代码": ["512890"], "名称": ["红利低波ETF"], "最新价": [0]})
        with mock.patch.object(fetcher, "_spot_table", return_value=table):
            self.assertIsNone(fetcher.identify_security("512890"))


class ConcurrencyV2Test(unittest.TestCase):
    def test_cloud_state_conflict_fails_without_silent_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            state = new_cloud_state(encode_snapshot(build_snapshot(service)))
        class ConflictFetcher:
            def get_github_repository_file(self, *args):
                return None
            def put_github_repository_file(self, *args):
                raise RuntimeError("GitHub 状态文件并发冲突")
        store = CloudStateStore("token", "owner/private", fetcher=ConflictFetcher())
        with self.assertRaisesRegex(RuntimeError, "并发冲突"):
            store.save(state, "stale-sha", "test")


class AdviceV2Test(unittest.TestCase):
    def _advice(self):
        dates = pd.date_range("2026-06-01", periods=80, freq="B")
        close = pd.Series([10 + index * 0.02 for index in range(80)])
        frame = pd.DataFrame({
            "日期": dates, "开盘": close, "最高": close + 0.2, "最低": close - 0.2,
            "收盘": close, "成交量": [10000 + index for index in range(80)],
        })
        frame = add_all_indicators(frame)
        position = StockPosition("600104", "上汽集团", Market.A_SH, 100, 10.0, float(close.iloc[-1]), "汽车")
        manager = PortfolioManager(Path("missing.json"))
        manager.positions, manager.cash = [position], 10_000.0
        view = _position_views(manager, {"600104": frame})[0]
        return build_rule_advice(view, frame)

    def test_rule_layer_generates_traceable_bounded_numbers(self):
        advice = self._advice()
        self.assertIn(advice["action_label"], ("持有", "观望", "减仓", "小幅加仓"))
        self.assertLess(advice["buy_range"][0], advice["buy_range"][1])
        self.assertLess(advice["stop"], advice["target"])

    def test_ai_can_only_add_text_and_order_not_change_numbers(self):
        original = self._advice()
        class FakeFetcher:
            def request_openai_json(self, key, model, payload):
                return {"order": ["600104"], "notes": [{"code": "600104", "note": "趋势平稳，维持规则倾向。"}], "target": 999999}
        enhanced, mode = OpenAIAdviceProvider(FakeFetcher(), api_key="test-key").enhance([dict(original)])
        self.assertEqual(enhanced[0]["target"], original["target"])
        self.assertEqual(enhanced[0]["ai_note"], "趋势平稳，维持规则倾向。")
        self.assertIn("数字仍来自规则层", mode)

        class NumericFetcher:
            def request_openai_json(self, *args):
                return {"order": ["600104"], "notes": [{"code": "600104", "note": "目标价 999"}]}
        rejected, _ = OpenAIAdviceProvider(NumericFetcher(), api_key="key").enhance([dict(original)])
        self.assertIsNone(rejected[0]["ai_note"])

    def test_missing_key_and_invalid_response_degrade(self):
        advice = self._advice()
        no_key, mode = OpenAIAdviceProvider(object(), api_key="").enhance([dict(advice)])
        self.assertIn("未配置", mode)
        class BrokenFetcher:
            def request_openai_json(self, *args):
                raise RuntimeError("failure")
        degraded, mode = OpenAIAdviceProvider(BrokenFetcher(), api_key="key").enhance([dict(advice)])
        self.assertEqual(degraded[0]["target"], advice["target"])
        self.assertIn("自动降级", mode)


if __name__ == "__main__":
    unittest.main()
