import datetime as dt
import io
import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from quant_assistant.advice_performance import (RULE_VERSION, append_immutable, correlate_execution,
                                                 evaluate, summarize,
                                                 render_summary)
from quant_assistant.data.fetcher import CN_TZ, DataFetcher
from quant_assistant.models import Market
from quant_assistant.v3 import build_intraday, classify_quote, recalculate, save_morning_advice


def record(version="v3-advice-1"):
    return {"advice_id": "a1", "as_of": "2026-01-05", "code": "510300",
            "market": "ETF", "rule_version": version, "action_tendency": "HOLD",
            "reference_close": 100, "target_price": 110, "invalidation_price": 90,
            "buy_zone": [95, 99], "reduce_zone": [106, 109]}


def frame(count=20, high=105, low=95, close=102):
    dates = pd.bdate_range("2026-01-06", periods=count)
    return pd.DataFrame({"日期": dates, "最高": high, "最低": low, "收盘": close})


def test_future_sessions_only_and_full_window():
    data = pd.concat([pd.DataFrame({"日期": [pd.Timestamp("2026-01-05")],
                                    "最高": [200], "最低": [1], "收盘": [100]}), frame()])
    result = evaluate(record(), data, dt.date(2026, 1, 12))
    assert result["windows"]["5"]["status"] == "neither"
    assert result["windows"]["10"]["status"] == "pending"
    assert result["windows"]["20"]["status"] == "pending"
    assert result["windows"]["5"]["end_return"] == pytest.approx(.02)


def test_same_day_touch_is_ambiguous_and_neither_is_explicit():
    data = frame()
    data.loc[1, ["最高", "最低"]] = [111, 89]
    result = evaluate(record(), data, dt.date(2026, 2, 10))
    assert all(result["windows"][str(n)]["status"] == "same_day_ambiguous"
               for n in (5, 10, 20))
    assert evaluate(record(), frame(), dt.date(2026, 2, 10))["windows"]["20"]["status"] == "neither"


def test_summary_sample_warning_version_filter_and_recompute():
    first = record()
    second = {**record("old"), "advice_id": "old"}
    outcomes = [evaluate(r, frame(), dt.date(2026, 2, 10)) for r in (first, second)]
    summary = summarize([first, second], outcomes, "v3-advice-1")
    assert summary["sample_count"] == summary["settled_count"] == 1
    assert summary["insufficient_sample"]
    rendered = render_summary(summary)
    assert "样本不足" in rendered
    assert "20日观察窗口（已结算样本）：目标先触及" in rendered
    assert summary == summarize([first, second], outcomes, "v3-advice-1")
    assert append_immutable([first], [first]) == [first]
    with pytest.raises(ValueError, match="拒绝覆盖"):
        append_immutable([first], [{**first, "target_price": 200}])


def test_execution_attribution_requires_explicit_advice_id():
    trade = {"code": "510300", "side": "SELL", "quantity": 10, "price": 110,
             "fee": 0, "realized_pnl": 100, "executed_at": "2026-01-10T10:00:00"}
    assert correlate_execution(record(), [trade])["status"] == "unknown"
    linked = correlate_execution(record(), [{**trade, "related_plan_id": "a1"}])
    assert linked["status"] == "linked"
    assert linked["realized_return"] == pytest.approx(.1)


def test_morning_advice_is_read_back_from_private_store_before_report_send(tmp_path):
    class FakeStore:
        def __init__(self):
            self.state = {"advice_records": []}
        def load(self, _initial):
            return json.loads(json.dumps(self.state)), "sha"
        def save(self, state, _sha, _message):
            self.state = json.loads(json.dumps(state))

    pos = SimpleNamespace(code="510300", name="测试ETF", market=Market.ETF,
                          asset_type="ETF", cost_price=100, current_price=100)
    report = {"daily": tmp_path / "daily.md", "detail": tmp_path / "detail.md",
              "advices": [{"code": "510300", "action": "HOLD", "buy_range": (95, 99),
                           "reduce_range": (106, 109), "stop": 90, "target": 110,
                           "confidence": "中", "ai_note": None}],
              "views": [{"position": pos, "available": True, "weight": .2,
                         "data_date": "2026-09-16", "indicators": {"MA20": 98}}],
              "stock_data": {}}
    report["daily"].write_text("日报\n", encoding="utf-8")
    report["detail"].write_text("详细\n", encoding="utf-8")
    fake = FakeStore()
    with patch("quant_assistant.v3.TradingService") as service, \
         patch("quant_assistant.v3.recalculate", return_value=([], summarize([], [], RULE_VERSION))):
        service.return_value.repository.list_executions.return_value = []
        save_morning_advice(report, dt.datetime(2026, 9, 17, 9, 20, tzinfo=CN_TZ), fake)
        save_morning_advice(report, dt.datetime(2026, 9, 17, 9, 20, tzinfo=CN_TZ), fake)
    assert len(fake.state["advice_records"]) == 1
    assert fake.state["advice_records"][0]["buy_zone"] == [95, 99]
    assert "建议效果追踪" in report["detail"].read_text(encoding="utf-8")


def test_recalculate_fetches_oldest_history_and_excludes_incomplete_today():
    fetcher = Mock()
    fetcher.fetch_hist.return_value = frame(20)
    records = [record(), {**record(), "advice_id": "later", "as_of": "2026-01-08"}]
    outcomes, summary = recalculate(records, dt.datetime(2026, 2, 5, 14, 30, tzinfo=CN_TZ), fetcher)
    assert fetcher.fetch_hist.call_count == 1
    assert summary["settled_count"] == 1
    assert outcomes[1]["windows"]["20"]["status"] == "pending"


def test_intraday_success_and_missing_quote_never_uses_previous_close():
    advice = record()
    advice["as_of"] = "2026-09-17"
    position = Mock(code="510300", name="测试ETF", market=Market.ETF, current_price=100)
    pm = Mock(positions=[position])
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = {"price": 97, "change_pct": -1,
                                                  "volume": 1000, "amount": 97000,
                                                  "provisional": True}
    now = dt.datetime(2026, 9, 17, 14, 30, tzinfo=CN_TZ)
    text = build_intraday(pm, [advice], fetcher, now)
    assert "已进入买入区" in text and "97.00" in text
    fetcher.fetch_intraday_quote.return_value = None
    missing = build_intraday(pm, [advice], fetcher, now)
    assert "实时数据不可用" in missing and "100.000" not in missing
    assert "无可验证的盘中报价" in missing
    assert "仅为持仓盘中风险快照" in build_intraday(pm, [], fetcher, now)


def test_provisional_quote_does_not_touch_daily_cache(tmp_path):
    fetcher = DataFetcher.__new__(DataFetcher)
    fetcher._spot_cache = {}
    fetcher._lookup_spot = Mock(return_value={"code": "510300", "price": 100,
        "change_pct": 1, "volume": 100, "amount": 10000,
        "quote_updated_at": pd.Timestamp("2026-09-17 14:29:00", tz=CN_TZ)})
    quote = fetcher.fetch_intraday_quote("510300", Market.ETF,
        dt.datetime(2026, 9, 17, 14, 30, tzinfo=CN_TZ))
    assert quote["provisional"] is True
    assert list(tmp_path.iterdir()) == []
    fetcher._lookup_spot.assert_called_once()


def test_stale_eastmoney_quote_falls_back_and_never_uses_yesterday():
    fetcher = DataFetcher.__new__(DataFetcher)
    fetcher._lookup_spot = Mock(return_value={"code": "510300", "price": 100,
        "change_pct": 0, "volume": 100, "amount": 10000,
        "quote_updated_at": pd.Timestamp("2026-09-16 14:30:00", tz=CN_TZ)})
    fetcher._sina_intraday_quote = Mock(return_value=None)
    fetcher._tencent_intraday_quote = Mock(return_value=None)
    now = dt.datetime(2026, 9, 17, 14, 30, tzinfo=CN_TZ)
    assert fetcher.fetch_intraday_quote("510300", Market.ETF, now) is None
    fallback = {"price": 101, "provisional": True, "source": "sina"}
    fetcher._sina_intraday_quote.return_value = fallback
    assert fetcher.fetch_intraday_quote("510300", Market.ETF, now) == fallback
    fetcher._lookup_spot.reset_mock()
    assert fetcher.fetch_intraday_quote("600000", Market.A_SH, now) == fallback
    fetcher._lookup_spot.assert_not_called()
    fetcher._sina_intraday_quote.return_value = None
    fetcher._tencent_intraday_quote.return_value = {**fallback, "source": "tencent"}
    assert fetcher.fetch_intraday_quote("600000", Market.A_SH, now)["source"] == "tencent"


def test_gap_and_volume_flags():
    quote = {"price": 97, "change_pct": -2, "open": 96,
             "previous_close": 100, "volume": 200}
    result = classify_quote(record(), quote, avg_volume=100)
    assert result["status"] == "已进入买入区"
    assert result["flags"] == ["向下跳空4.0%"]
    assert result["volume_note"] == "成交量判断不可用"


@pytest.mark.parametrize("price, expected", [
    (97, "已进入买入区"),
    (107, "已进入减仓区"),
    (90, "已失效"),
    (110, "目标已达"),
])
def test_intraday_status_uses_morning_advice_levels(price, expected):
    quote = {"price": price, "change_pct": (price / 100 - 1) * 100,
             "volume": 100, "amount": price * 10000}
    assert classify_quote(record(), quote)["status"] == expected


def test_intraday_report_uses_only_todays_advice():
    position = Mock(code="510300", name="测试ETF", market=Market.ETF)
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = {
        "price": 97, "change_pct": -3, "volume": 100,
        "amount": 970000, "as_of": "2026-09-17T14:29:00+08:00",
        "source": "sina", "provisional": True,
    }
    now = dt.datetime(2026, 9, 17, 14, 30, tzinfo=CN_TZ)
    yesterday = {**record(), "as_of": "2026-09-16"}
    today = {**record(), "as_of": "2026-09-17"}
    assert "已进入买入区" in build_intraday(Mock(positions=[position]), [yesterday, today], fetcher, now)
    stale = build_intraday(Mock(positions=[position]), [yesterday], fetcher, now)
    assert "仅为持仓盘中风险快照" in stale
    assert "已进入买入区" not in stale


def test_tencent_fallback_requires_fresh_timestamp():
    fetcher = DataFetcher.__new__(DataFetcher)
    fields = [""] * 38
    fields[1], fields[3], fields[4], fields[5] = "测试", "101", "100", "96"
    fields[30], fields[36], fields[37] = "20260917142900", "1200", "20"
    raw = ('v_sh600000="' + "~".join(fields) + '";').encode("gbk")
    now = dt.datetime(2026, 9, 17, 14, 30, tzinfo=CN_TZ)
    with patch("quant_assistant.data.fetcher.urllib.request.urlopen", return_value=io.BytesIO(raw)):
        result = fetcher._tencent_intraday_quote("600000", now)
    assert result["source"] == "tencent" and result["volume"] == 1200
    assert result["amount"] == 200000
    fields[30] = "20260916142900"
    raw = ('v_sh600000="' + "~".join(fields) + '";').encode("gbk")
    with patch("quant_assistant.data.fetcher.urllib.request.urlopen", return_value=io.BytesIO(raw)):
        assert fetcher._tencent_intraday_quote("600000", now) is None


def test_workflow_utc_and_shared_lock():
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    intraday = (workflows / "account-intraday-telegram.yml").read_text()
    assert 'cron: "30 6 * * 1-5"' in intraday
    for filename in ("account-daily-telegram.yml", "account-trade-telegram.yml",
                     "account-intraday-telegram.yml"):
        assert "group: account-cloud-state" in (workflows / filename).read_text()
    daily = (workflows / "account-daily-telegram.yml").read_text()
    notify_step = daily.split("- name: Generate and send account daily report", 1)[1]
    assert "ACCOUNT_STATE_TOKEN:" in notify_step and "ACCOUNT_STATE_REPO:" in notify_step
    assert "inputs.mode == 'intraday'" in daily
    assert "run: python -m quant_assistant notify-intraday" in daily
