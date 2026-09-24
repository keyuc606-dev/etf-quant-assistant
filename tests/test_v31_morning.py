"""Morning close-only gate and compact Telegram presentation."""

import datetime as dt
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from quant_assistant.analysis.indicators import add_all_indicators
from quant_assistant.data.fetcher import CN_TZ
from quant_assistant.data.fetcher import DataFetcher
from quant_assistant.models import Market, StockPosition
from quant_assistant.notifications.telegram import TelegramNotifier
from quant_assistant.pipeline import run_daily_pipeline
from quant_assistant.portfolio.account_report import generate_account_reports
from quant_assistant.portfolio.holdings import PortfolioManager


MORNING = dt.datetime(2026, 9, 22, 9, 20, tzinfo=CN_TZ)


def frame(provisional=True):
    days = pd.bdate_range(end="2026-09-21", periods=70)
    rows = pd.DataFrame({"日期": days, "开盘": [20 + i * .05 for i in range(70)],
                         "收盘": [20 + i * .05 for i in range(70)],
                         "最高": [20.2 + i * .05 for i in range(70)],
                         "最低": [19.8 + i * .05 for i in range(70)],
                         "成交量": [1000] * 70})
    if provisional:
        rows.loc[len(rows)] = [pd.Timestamp("2026-09-22"), 100, 100, 101, 99, 0]
    return rows


def manager(tmp_path, count=1):
    pm = PortfolioManager(tmp_path / "portfolio.json")
    pm.cash = 10000
    pm.positions = [StockPosition(code=f"159{i:03d}", name=f"测试ETF{i}", market=Market.ETF,
                                  shares=1000, cost_price=30 if i < 3 else 20,
                                  current_price=100 if i == 0 else 23,
                                  sector="同一主题" if i < 2 else f"主题{i}") for i in range(count)]
    return pm


def test_0920_discards_provisional_bar_before_indicators_and_prices(tmp_path):
    pm = manager(tmp_path)
    fetched = type("Fetcher", (), {"fetch_hist": lambda self, *a, **kw: frame(),
                                    "_last_completed_trading_day": lambda self: dt.date(2026, 9, 21)})()
    with patch("quant_assistant.pipeline.DataFetcher", return_value=fetched), \
         patch("quant_assistant.pipeline.MarketCalendar._last_completed_trading_day",
               return_value=dt.date(2026, 9, 21)):
        result = run_daily_pipeline(pm)
    calculated = result["stock_data"]["159000"]
    assert calculated.iloc[-1]["日期"].date() == dt.date(2026, 9, 21)
    assert calculated.iloc[-1]["量比"] == 1.0
    assert pm.positions[0].current_price == frame(False).iloc[-1]["收盘"]
    reports = generate_account_reports(pm, result["stock_data"], report_dir=tmp_path,
                                       generated_at=MORNING)
    assert reports["advices"][0]["target"] < 100
    daily = reports["daily"].read_text(encoding="utf-8")
    assert "日期：2026-09-22" in daily
    assert "技术行情截止：2026-09-21 收盘" in daily
    assert "已验证截至上一完整交易日" in daily
    assert "量比=0.0，极度缩量" not in daily


def test_report_defense_filters_today_and_degrades_fallback_news(tmp_path):
    pm = manager(tmp_path)
    news = {"items": [], "news_as_of": "2026-09-22T01:10:00+00:00",
            "degraded": True, "source_status": {"bing_news_rss": "partial"}}
    reports = generate_account_reports(pm, {"159000": add_all_indicators(frame())},
                                       report_dir=tmp_path, generated_at=MORNING,
                                       news_result=news, degraded_sources=["159000"])
    assert reports["stock_data"]["159000"].iloc[-1]["日期"].date() == dt.date(2026, 9, 21)
    assert reports["advices"][0]["confidence"] != "高"
    telegram = reports["telegram"].read_text(encoding="utf-8")
    detail = reports["detail"].read_text(encoding="utf-8")
    assert "最终结论：" in telegram and "触发：" in telegram and "取消：" in telegram
    assert "warning/degraded" in telegram and "新闻不完整" in telegram
    assert "备用源接管" in detail and "全部技术指标" in detail


def test_compact_focus_risk_and_weight_and_conflict(tmp_path):
    pm = manager(tmp_path, 7)
    data = {p.code: add_all_indicators(frame(False)) for p in pm.positions}
    def conflict(view, *args):
        return {"discipline_state": "趋势破坏", "discipline_action": "人工复核",
                "forbidden_action": "禁止越跌越补、继续摊平", "t_opportunity": "不建议做T",
                "reentry_condition": "等待", "cash_defense_note": "正常", "reason": "测试",
                "sale_status": "unknown", "version": "discipline-v1"}
    with patch("quant_assistant.portfolio.account_report.build_discipline", side_effect=conflict), \
         patch("quant_assistant.portfolio.account_report.build_rule_advices") as build:
        from quant_assistant.analysis.account_advice import build_rule_advices
        build.side_effect = lambda views, frames: [dict(a, action="ADD_SMALL", action_label="小幅加仓",
                                                       position_change="增加 1–2 个百分点")
                                                   for a in build_rule_advices(views, frames)]
        reports = generate_account_reports(pm, data, report_dir=tmp_path, generated_at=MORNING)
    message = reports["telegram"].read_text(encoding="utf-8")
    assert len(message) < 1800
    assert "风险重点" in message and "仓位重点" in message
    assert 3 <= message.count("｜置信") <= 5
    assert "冲突，人工复核" in message
    assert "小幅加仓" not in message
    assert "详细新闻" in message
    assert "## 完整持仓" in reports["detail"].read_text(encoding="utf-8")
    sent, files = [], []
    fetcher = type("Sender", (), {
        "send_telegram_message": lambda self, token, chat, text: sent.append(text),
        "send_telegram_document": lambda self, token, chat, path: files.append(Path(path).read_text(encoding="utf-8")),
    })()
    result = TelegramNotifier("test-token", "123", fetcher).send_daily_report(
        reports["telegram"], reports["detail"])
    assert result.success and result.sent_parts == 2
    assert len(sent[0]) < 1800 and "|---|" not in sent[0]
    assert "## 完整持仓" in files[0]


def test_detail_upload_is_multipart_to_configured_chat(tmp_path):
    detail = tmp_path / "detail.md"
    detail.write_text("# 私有详细报告\n", encoding="utf-8")
    response = MagicMock()
    response.read.return_value = json.dumps({"ok": True, "result": {"message_id": 7}}).encode()
    context = MagicMock()
    context.__enter__.return_value = response
    with patch("quant_assistant.data.fetcher.urllib.request.urlopen", return_value=context) as send:
        result = DataFetcher(cache_dir=tmp_path).send_telegram_document("test-token", "123", detail)
    request = send.call_args.args[0]
    assert result["message_id"] == 7
    assert request.get_method() == "POST"
    assert request.full_url.endswith("/sendDocument")
    assert b'name="chat_id"' in request.data and b"123" in request.data
    assert detail.read_bytes() in request.data
