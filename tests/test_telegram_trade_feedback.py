import copy
import json
import tempfile
import unittest
from pathlib import Path

from quant_assistant.cloud_snapshot import build_snapshot, decode_snapshot, encode_snapshot
from quant_assistant.cloud_state import CloudStateStore, new_cloud_state
from quant_assistant.telegram_trade_bot import TelegramTradeBot
from quant_assistant.trading.service import TradingService
from quant_assistant.trading.telegram_feedback import (
    TelegramTradeFeedback,
    parse_trade_command,
)


def portfolio(cash=10_000.0):
    return {
        "cash": cash,
        "cash_flows": [],
        "positions": [{
            "code": "600104", "name": "上汽集团", "market": "上海",
            "asset_type": "STOCK", "shares": 500, "cost_price": 15.0,
            "current_price": 18.0, "sector": "汽车", "last_updated": None,
            "pe": 0.0, "pb": 0.0, "roe": 0.0, "market_cap": 0.0,
        }],
    }


def make_service(root: Path, cash=10_000.0):
    portfolio_path = root / "portfolio.json"
    database_path = root / "trading.sqlite3"
    portfolio_path.parent.mkdir(parents=True, exist_ok=True)
    portfolio_path.write_text(json.dumps(portfolio(cash), ensure_ascii=False), encoding="utf-8")
    service = TradingService(database_path, portfolio_path)
    service.initialize_from_portfolio()
    return service


class FakeCloudFetcher:
    def __init__(self):
        self.item = None
        self.puts = []

    def get_github_repository_file(self, token, repository, path):
        return copy.deepcopy(self.item)

    def put_github_repository_file(self, token, repository, path, content, message, sha=None):
        new_sha = f"sha-{len(self.puts) + 1}"
        self.puts.append((token, repository, path, content, message, sha))
        self.item = {"content": content, "sha": new_sha}
        return new_sha


class MemoryStateStore:
    def __init__(self):
        self.state = None
        self.sha = None
        self.saves = 0

    def load(self, initial_snapshot_b64=""):
        if self.state is None:
            return new_cloud_state(initial_snapshot_b64), None
        return copy.deepcopy(self.state), self.sha

    def save(self, state, sha, message):
        self.saves += 1
        self.state = copy.deepcopy(state)
        self.sha = f"sha-{self.saves}"
        return self.sha


class FakeTelegramFetcher:
    def __init__(self, updates=None, send_error=None):
        self.updates = list(updates or [])
        self.send_error = send_error
        self.sent = []

    def fetch_telegram_updates(self, token, offset, limit=100):
        return [item for item in self.updates if item["update_id"] >= offset]

    def send_telegram_message(self, token, chat_id, text):
        if self.send_error:
            raise self.send_error
        self.sent.append((chat_id, text))
        return {"ok": True, "message_id": len(self.sent)}


def update(update_id, text, chat_id="123"):
    return {"update_id": update_id, "message": {"chat": {"id": int(chat_id)}, "text": text}}


class TradeParserTest(unittest.TestCase):
    def test_parses_chinese_and_english_with_optional_fee(self):
        buy = parse_trade_command("买入 600104 300股 18.72 手续费5")
        sell = parse_trade_command("SELL 002074 500 39.10")
        self.assertEqual((buy.side, buy.code, buy.quantity, buy.price, buy.fee),
                         ("BUY", "600104", 300, 18.72, 5.0))
        self.assertEqual((sell.side, sell.code, sell.quantity, sell.price, sell.fee),
                         ("SELL", "002074", 500, 39.10, 0.0))

    def test_rejects_malformed_trade_instead_of_guessing(self):
        with self.assertRaisesRegex(ValueError, "无法识别"):
            parse_trade_command("买入 600104 三百股 18.72")
        self.assertIsNone(parse_trade_command("你好"))


class TelegramFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = make_service(Path(self.temp.name))
        self.feedback = TelegramTradeFeedback(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def test_first_message_only_creates_pending_then_confirm_records(self):
        before = self.service.rebuild_portfolio()
        proposed = self.feedback.handle("买入 600104 100股 18.72 手续费5", 10, "123", None)
        self.assertIsNotNone(proposed.pending)
        self.assertFalse(proposed.account_changed)
        self.assertEqual(self.service.rebuild_portfolio(), before)

        confirmed = self.feedback.handle("确认", 11, "123", proposed.pending)
        self.assertTrue(confirmed.account_changed)
        self.assertIsNone(confirmed.pending)
        state = self.service.rebuild_portfolio()
        self.assertEqual(state["positions"][0]["shares"], 600)
        self.assertAlmostEqual(state["cash"], 8123.0)

    def test_cancel_leaves_account_unchanged(self):
        before = self.service.rebuild_portfolio()
        proposed = self.feedback.handle("卖出 600104 100股 18.72", 20, "123", None)
        cancelled = self.feedback.handle("取消", 21, "123", proposed.pending)
        self.assertIsNone(cancelled.pending)
        self.assertFalse(cancelled.account_changed)
        self.assertEqual(self.service.rebuild_portfolio(), before)

    def test_insufficient_cash_and_oversell_are_rejected_before_pending(self):
        too_large = self.feedback.handle("买入 600104 1000股 18.72", 1, "123", None)
        oversell = self.feedback.handle("卖出 600104 501股 18.72", 2, "123", None)
        self.assertIsNone(too_large.pending)
        self.assertIn("现金不足", too_large.response)
        self.assertIsNone(oversell.pending)
        self.assertIn("持仓不足", oversell.response)
        self.assertEqual(self.service.recent_executions(), [])

    def test_existing_pending_cannot_be_silently_replaced(self):
        proposed = self.feedback.handle("卖出 600104 100股 18.72", 30, "123", None)
        second = self.feedback.handle("卖出 600104 200股 18.72", 31, "123", proposed.pending)
        self.assertEqual(second.pending, proposed.pending)
        self.assertIn("已有一笔", second.response)

    @staticmethod
    def _resolver(code):
        instruments = {
            "000725": {"code": "000725", "name": "京东方A", "asset_type": "STOCK",
                       "asset_subtype": "STOCK", "market": "深圳", "exchange": "深圳",
                       "reference_price": None, "market_data_degraded": True,
                       "identity_source": "sina"},
            "512890": {"code": "512890", "name": "红利低波ETF", "asset_type": "ETF",
                       "asset_subtype": "EQUITY_ETF", "market": "ETF", "exchange": "上海",
                       "reference_price": 1.1, "market_data_degraded": False,
                       "identity_source": "eastmoney"},
        }
        return instruments.get(code)

    def test_first_buy_000725_enters_pending_when_realtime_is_degraded(self):
        feedback = TelegramTradeFeedback(self.service, self._resolver)
        proposed = feedback.handle("买入 000725 100股 4.20", 40, "123", None)
        self.assertIsNotNone(proposed.pending)
        command = proposed.pending["commands"][0]
        self.assertEqual(command["asset_type"], "STOCK")
        self.assertEqual(command["instrument"]["name"], "京东方A")
        self.assertEqual(command["instrument"]["exchange"], "深圳")
        self.assertTrue(command["instrument"]["market_data_degraded"])
        self.assertIn("实时行情暂不可用", proposed.response)
        confirmed = feedback.handle("确认", 41, "123", proposed.pending)
        self.assertTrue(confirmed.account_changed)
        position = next(item for item in self.service.rebuild_portfolio()["positions"]
                        if item["code"] == "000725")
        self.assertEqual((position["name"], position["asset_type"], position["asset_subtype"],
                          position["market"]),
                         ("京东方A", "STOCK", "STOCK", "深圳"))

    def test_batch_with_000725_and_512890_previews_atomically(self):
        feedback = TelegramTradeFeedback(self.service, self._resolver)
        proposed = feedback.handle(
            "买入 000725 100股 4.20\n买入 512890 100份 1.10", 41, "123", None)
        self.assertIsNotNone(proposed.pending)
        self.assertEqual(len(proposed.pending["commands"]), 2)
        self.assertEqual([item["asset_type"] for item in proposed.pending["commands"]],
                         ["STOCK", "ETF"])

    def test_genuinely_unknown_instrument_rejects_whole_batch(self):
        feedback = TelegramTradeFeedback(self.service, self._resolver)
        before = self.service.rebuild_portfolio()
        rejected = feedback.handle(
            "买入 000725 100股 4.20\n买入 000726 100股 4.20", 42, "123", None)
        self.assertIsNone(rejected.pending)
        self.assertIn("000726 无法从行情源识别", rejected.response)
        self.assertEqual(self.service.rebuild_portfolio(), before)


class CloudStateTest(unittest.TestCase):
    def test_private_repository_round_trip_and_optimistic_sha(self):
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            encoded = encode_snapshot(build_snapshot(service))
        fetcher = FakeCloudFetcher()
        store = CloudStateStore("token", "owner/private-state", fetcher=fetcher)
        state, sha = store.load(encoded)
        self.assertIsNone(sha)
        first_sha = store.save(state, sha, "Initialize")
        loaded, loaded_sha = store.load()
        self.assertEqual(first_sha, loaded_sha)
        self.assertEqual(decode_snapshot(loaded["account_snapshot_b64"])["positions"][0]["shares"], 500)
        self.assertIsNone(fetcher.puts[0][-1])


class TelegramBotEndToEndTest(unittest.TestCase):
    def test_batch_survives_ephemeral_runner_then_commits_all(self):
        with tempfile.TemporaryDirectory() as initial_temp:
            service = make_service(Path(initial_temp))
            initial_snapshot = encode_snapshot(build_snapshot(service))
        store = MemoryStateStore()
        with tempfile.TemporaryDirectory() as first_temp:
            bot = TelegramTradeBot(
                "token", "123", store,
                FakeTelegramFetcher([update(300, "卖出 600104 100股 18.00\n买入 600104 50股 17.00")]),
                Path(first_temp) / "ledger.sqlite3", Path(first_temp) / "portfolio.json",
            )
            self.assertEqual(bot.run_once(initial_snapshot)["status"], "message-processed")
        self.assertEqual(len(store.state["telegram"]["pending"]["commands"]), 2)
        with tempfile.TemporaryDirectory() as second_temp:
            bot = TelegramTradeBot(
                "token", "123", store, FakeTelegramFetcher([update(301, "确认")]),
                Path(second_temp) / "ledger.sqlite3", Path(second_temp) / "portfolio.json",
            )
            self.assertEqual(bot.run_once()["status"], "account-updated")
        snapshot = decode_snapshot(store.state["account_snapshot_b64"])
        self.assertEqual(snapshot["positions"][0]["shares"], 450)
        self.assertAlmostEqual(snapshot["account"]["cash"], 10_950.0)

    def test_simulated_trade_survives_two_ephemeral_runners(self):
        with tempfile.TemporaryDirectory() as initial_temp:
            service = make_service(Path(initial_temp))
            initial_snapshot = encode_snapshot(build_snapshot(service))
        store = MemoryStateStore()

        with tempfile.TemporaryDirectory() as first_temp:
            fetcher = FakeTelegramFetcher([update(100, "买入 600104 100股 18.72 手续费5")])
            bot = TelegramTradeBot(
                "token", "123", store, fetcher,
                Path(first_temp) / "ledger.sqlite3", Path(first_temp) / "portfolio.json",
            )
            self.assertEqual(bot.run_once(initial_snapshot)["status"], "message-processed")
        self.assertIsNotNone(store.state["telegram"]["pending"])
        self.assertEqual(decode_snapshot(store.state["account_snapshot_b64"])["positions"][0]["shares"], 500)

        with tempfile.TemporaryDirectory() as second_temp:
            fetcher = FakeTelegramFetcher([update(101, "确认")])
            bot = TelegramTradeBot(
                "token", "123", store, fetcher,
                Path(second_temp) / "ledger.sqlite3", Path(second_temp) / "portfolio.json",
            )
            self.assertEqual(bot.run_once()["status"], "account-updated")
        snapshot = decode_snapshot(store.state["account_snapshot_b64"])
        self.assertEqual(snapshot["positions"][0]["shares"], 600)
        self.assertAlmostEqual(snapshot["account"]["cash"], 8123.0)
        self.assertIsNone(store.state["telegram"]["pending"])

    def test_reply_failure_keeps_outbox_for_retry_without_reapplying_trade(self):
        with tempfile.TemporaryDirectory() as initial_temp:
            service = make_service(Path(initial_temp))
            initial_snapshot = encode_snapshot(build_snapshot(service))
        store = MemoryStateStore()
        store.state = new_cloud_state(initial_snapshot)
        store.state["telegram"]["pending"] = {
            "command": {"side": "SELL", "code": "600104", "quantity": 100,
                        "price": 18.72, "fee": 0.0, "asset_type": "STOCK"},
            "chat_id": "123", "origin_update_id": 200,
            "created_at": "2026-09-15T00:00:00+00:00",
        }
        store.sha = "sha-0"
        with tempfile.TemporaryDirectory() as temp:
            bot = TelegramTradeBot(
                "token", "123", store,
                FakeTelegramFetcher([update(201, "确认")], RuntimeError("offline")),
                Path(temp) / "ledger.sqlite3", Path(temp) / "portfolio.json",
            )
            with self.assertRaisesRegex(RuntimeError, "offline"):
                bot.run_once()
        self.assertIsNotNone(store.state["telegram"]["outbox"])
        shares = decode_snapshot(store.state["account_snapshot_b64"])["positions"][0]["shares"]
        self.assertEqual(shares, 400)

        retry_fetcher = FakeTelegramFetcher()
        with tempfile.TemporaryDirectory() as retry_temp:
            retry_bot = TelegramTradeBot(
                "token", "123", store, retry_fetcher,
                Path(retry_temp) / "ledger.sqlite3", Path(retry_temp) / "portfolio.json",
            )
            self.assertEqual(retry_bot.run_once()["status"], "outbox-delivered")
        self.assertEqual(len(retry_fetcher.sent), 1)
        self.assertIsNone(store.state["telegram"]["outbox"])
        self.assertEqual(decode_snapshot(store.state["account_snapshot_b64"])["positions"][0]["shares"], 400)


if __name__ == "__main__":
    unittest.main()
