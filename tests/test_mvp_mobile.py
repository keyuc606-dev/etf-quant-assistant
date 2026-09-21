import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from quant_assistant.notifications.telegram import (
    TelegramNotifier,
    daily_markdown_to_telegram,
    split_telegram_message,
)


class FakeTelegramFetcher:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def send_telegram_message(self, token, chat_id, text):
        self.calls.append((token, chat_id, text))
        if self.error:
            raise self.error
        return {"ok": True, "message_id": len(self.calls)}


class MvpMobileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.report = self.root / "my-portfolio-daily.md"
        self.report.write_text(
            "# 我的 ETF 账户日报\n\n"
            "行情截止：2026-09-11\n新闻截止：2026-09-13 06:12 北京时间\n\n"
            "## 一、账户概览\n\n- 总资产：￥100,000\n\n"
            "## 二、今日重点关注\n\n### 示例ETF（510300）\n\n- 状态：仅供复核\n\n"
            "## 五、其他持仓\n\n| 代码 | 名称 |\n|---|---|\n| 1 | 应被省略 |\n\n"
            "## 六、今日摘要\n\n- 不生成主动买卖建议\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_credentials_fails_safely_without_network(self):
        fetcher = FakeTelegramFetcher()
        with patch.dict(os.environ, {}, clear=True):
            result = TelegramNotifier(fetcher=fetcher).send_daily_report(self.report)
        self.assertFalse(result.success)
        self.assertEqual(fetcher.calls, [])
        self.assertIn("TELEGRAM_BOT_TOKEN", result.error)

    def test_telegram_send_success(self):
        fetcher = FakeTelegramFetcher()
        result = TelegramNotifier("token", "123", fetcher).send_daily_report(self.report)
        self.assertTrue(result.success)
        self.assertEqual(result.sent_parts, 1)
        self.assertEqual(fetcher.calls[0][:2], ("token", "123"))
        self.assertIn("行情截止", fetcher.calls[0][2])

    def test_chat_id_is_single_configured_numeric_whitelist(self):
        fetcher = FakeTelegramFetcher()
        result = TelegramNotifier("token", "123,456", fetcher).send_daily_report(self.report)
        self.assertFalse(result.success)
        self.assertEqual(fetcher.calls, [])
        self.assertIn("单个数字会话ID", result.error)

    def test_telegram_failure_does_not_damage_daily_report_or_leak_token(self):
        original = self.report.read_text(encoding="utf-8")
        fetcher = FakeTelegramFetcher(RuntimeError("request token=secret-token chat=123 failed"))
        result = TelegramNotifier("secret-token", "123", fetcher).send_daily_report(self.report)
        self.assertFalse(result.success)
        self.assertEqual(self.report.read_text(encoding="utf-8"), original)
        self.assertNotIn("secret-token", result.error)
        self.assertNotIn("123", result.error)

    def test_long_message_is_split_below_telegram_limit(self):
        parts = split_telegram_message("\n".join(["一" * 500 for _ in range(20)]))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 4096 for part in parts))
        self.assertTrue(all(part.startswith(f"（{index}/") for index, part in enumerate(parts, 1)))

    def test_daily_conversion_keeps_cutoffs_and_omits_other_holdings_table(self):
        text = daily_markdown_to_telegram(self.report.read_text(encoding="utf-8"))
        self.assertIn("行情截止：2026-09-11", text)
        self.assertIn("新闻截止：2026-09-13", text)
        self.assertIn("今日摘要", text)
        self.assertNotIn("应被省略", text)

    def test_notify_daily_runs_daily_before_telegram(self):
        from quant_assistant.__main__ import cmd_notify_daily

        notifier = Mock()
        notifier.send_daily_report.return_value = SimpleNamespace(success=True, sent_parts=1)
        with patch("quant_assistant.__main__.cmd_daily", return_value={"daily": self.report}) as daily, \
             patch("quant_assistant.v3.cloud_available", return_value=True), \
             patch("quant_assistant.v3.save_morning_advice"), \
             patch("quant_assistant.notifications.telegram.TelegramNotifier", return_value=notifier):
            cmd_notify_daily(Namespace(days=120))
        daily.assert_called_once()
        notifier.send_daily_report.assert_called_once_with(self.report)

    def test_notify_daily_backup_market_data_still_sends(self):
        from quant_assistant.__main__ import cmd_notify_daily

        notifier = Mock()
        notifier.send_daily_report.return_value = SimpleNamespace(success=True, sent_parts=1)
        reports = {"daily": self.report, "market_data_count": 1}
        with patch("quant_assistant.__main__.cmd_daily", return_value=reports), \
             patch("quant_assistant.v3.cloud_available", return_value=True), \
             patch("quant_assistant.v3.save_morning_advice") as save, \
             patch("quant_assistant.notifications.telegram.TelegramNotifier", return_value=notifier):
            cmd_notify_daily(Namespace(days=120))
        save.assert_called_once_with(reports)
        notifier.send_daily_report.assert_called_once_with(self.report)

    def test_notify_daily_no_market_data_is_fatal_before_persisting(self):
        from quant_assistant.__main__ import cmd_notify_daily

        with patch("quant_assistant.__main__.cmd_daily",
                   return_value={"daily": self.report, "market_data_count": 0}), \
             patch("quant_assistant.v3.save_morning_advice") as save, \
             patch("quant_assistant.notifications.telegram.TelegramNotifier") as notifier:
            with self.assertRaisesRegex(RuntimeError, "全部持仓行情不可用"):
                cmd_notify_daily(Namespace(days=120))
        save.assert_not_called()
        notifier.assert_not_called()

    def test_notify_daily_state_failure_is_fatal_before_telegram(self):
        from quant_assistant.__main__ import cmd_notify_daily

        with patch("quant_assistant.__main__.cmd_daily",
                   return_value={"daily": self.report, "market_data_count": 1}), \
             patch("quant_assistant.v3.cloud_available", return_value=True), \
             patch("quant_assistant.v3.save_morning_advice",
                   side_effect=RuntimeError("回读不一致")), \
             patch("quant_assistant.notifications.telegram.TelegramNotifier") as notifier:
            with self.assertRaisesRegex(RuntimeError, "建议/状态持久化失败.*回读不一致"):
                cmd_notify_daily(Namespace(days=120))
        notifier.assert_not_called()

    def test_notify_daily_cloud_credentials_missing_in_actions_is_fatal(self):
        from quant_assistant.__main__ import cmd_notify_daily

        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True), \
             patch("quant_assistant.__main__.cmd_daily",
                   return_value={"daily": self.report, "market_data_count": 1}), \
             patch("quant_assistant.v3.save_morning_advice") as save:
            with self.assertRaisesRegex(RuntimeError, "云端建议状态凭据缺失"):
                cmd_notify_daily(Namespace(days=120))
        save.assert_not_called()

    def test_notify_daily_telegram_failure_is_fatal(self):
        from quant_assistant.__main__ import cmd_notify_daily

        notifier = Mock()
        notifier.send_daily_report.return_value = SimpleNamespace(success=False, error="HTTP 502")
        with patch("quant_assistant.__main__.cmd_daily",
                   return_value={"daily": self.report, "market_data_count": 1}), \
             patch("quant_assistant.v3.cloud_available", return_value=True), \
             patch("quant_assistant.v3.save_morning_advice"), \
             patch("quant_assistant.notifications.telegram.TelegramNotifier", return_value=notifier):
            with self.assertRaisesRegex(RuntimeError, "Telegram 推送失败: HTTP 502"):
                cmd_notify_daily(Namespace(days=120))


if __name__ == "__main__":
    unittest.main()
