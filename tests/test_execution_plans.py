import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from quant_assistant.advice_performance import make_records
from quant_assistant.data.fetcher import CN_TZ
from quant_assistant.models import Market
from quant_assistant.v3 import (EXECUTION_SESSION, build_closing_execution,
                                build_morning_execution)


def _reports(count=1, data_quality="HIGH", news_degraded=False):
    positions = []
    advices = []
    views = []
    for index in range(count):
        code = f"000{index + 1:03d}"
        position = SimpleNamespace(
            code=code, name=f"测试{index + 1}", market=Market.A_SZ,
            asset_type="STOCK", asset_subtype="STOCK", shares=1000,
            current_price=100.0, cost_price=90.0, market_value=100_000.0,
        )
        positions.append(position)
        decision = {
            "final_decision_version": "final-decision-v1", "final_action": "ADD",
            "final_action_label": "加仓", "action_reason": "买入区与账户闸门均已通过",
            "action_size": "100股≈￥10,000（10%）", "suggested_quantity": 100,
            "suggested_amount": 10_000.0, "suggested_fraction": .1,
            "trigger_condition": "进入买入区", "cancel_condition": "跌破失效位",
            "confidence": "中", "decision_confidence": "MEDIUM",
            "data_quality": data_quality, "conflict_note": "无",
            "reduction_reason_type": None, "t_economics": None,
            "manual_confirmation_required": True,
        }
        advices.append({
            "code": code, "action": "ADD_SMALL", "buy_range": (95.0, 99.0),
            "reduce_range": (106.0, 109.0), "stop": 90.0, "target": 110.0,
            "confidence": "中", "data_quality": data_quality,
            "decision_confidence": "MEDIUM", "ai_note": None, **decision,
        })
        views.append({
            "position": position, "available": True, "weight": .1,
            "data_date": "2026-09-25",
            "indicators": {"MA5": 96.0, "MA10": 95.0, "MA20": 94.0,
                           "ATR": 2.0, "量比": 1.0},
        })
    return {
        "advices": advices, "views": views, "stock_data": {},
        "disciplines": {}, "ma_disciplines": {},
        "news_result": {"degraded": news_degraded},
    }, SimpleNamespace(positions=positions)


def _quote(price=97.0, source="sina"):
    return {
        "price": price, "open": 98.0, "high": 99.0, "low": 96.0,
        "previous_close": 100.0, "change_pct": price - 100.0,
        "volume": 10_000.0, "amount": price * 10_000,
        "as_of": "2026-09-28T10:30:00+08:00", "source": source,
        "provisional": True,
    }


def test_morning_is_capped_at_five_and_uses_no_chase_language():
    reports, _pm = _reports(6)
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = _quote()
    text = build_morning_execution(
        reports, fetcher, dt.datetime(2026, 9, 28, 10, 30, tzinfo=CN_TZ))
    assert text.count("未成交：不追价，14:35复核。") == 5
    assert "其余1只：无明确早盘动作" in text
    assert "【10:30 早盘执行策略】" in text


def test_low_data_quality_blocks_strong_action():
    reports, _pm = _reports(data_quality="LOW")
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = _quote()
    text = build_morning_execution(
        reports, fetcher, dt.datetime(2026, 9, 28, 10, 30, tzinfo=CN_TZ))
    assert "1. 测试" not in text
    assert "数据质量低已折叠，不给强执行动作" in text


def test_morning_rejects_stale_provisional_timestamp():
    reports, _pm = _reports()
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = {
        **_quote(), "as_of": "2026-09-25T10:30:00+08:00",
    }
    text = build_morning_execution(
        reports, fetcher, dt.datetime(2026, 9, 28, 10, 30, tzinfo=CN_TZ))
    assert "1. 测试" not in text
    assert "数据质量低已折叠" in text


def test_partial_context_does_not_lower_explicit_risk_exit_confidence():
    reports, _pm = _reports(data_quality="MEDIUM", news_degraded=True)
    reports["views"][0]["indicators"]["MA20"] = 95.0
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = _quote(89.0)
    text = build_morning_execution(
        reports, fetcher, dt.datetime(2026, 9, 28, 10, 30, tzinfo=CN_TZ))
    assert "结论：风险退出" in text
    assert "决策高｜数据中" in text


def test_closing_cancels_unfilled_plan_and_never_assumes_fill():
    reports, pm = _reports()
    morning_fetcher = Mock()
    morning_fetcher.fetch_intraday_quote.return_value = _quote()
    now = dt.datetime(2026, 9, 28, 10, 30, tzinfo=CN_TZ)
    build_morning_execution(reports, morning_fetcher, now)
    records = make_records(reports, now, "test", EXECUTION_SESSION)
    closing_fetcher = Mock()
    closing_fetcher.fetch_intraday_quote.return_value = {
        **_quote(103.0), "as_of": "2026-09-28T14:35:00+08:00",
    }
    text = build_closing_execution(
        pm, records, closing_fetcher,
        dt.datetime(2026, 9, 28, 14, 35, tzinfo=CN_TZ),
    )
    assert "取消原挂单/不再追价" in text
    assert "若早盘订单未成交：取消原挂单，不再追价" in text
    assert "若已成交：请以已录入成交为准；系统不假设订单已执行" in text


def test_closing_without_1030_record_is_only_risk_snapshot():
    reports, pm = _reports()
    old_records = make_records(
        reports, dt.datetime(2026, 9, 28, 9, 20, tzinfo=CN_TZ), "old")
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = _quote(89.0)
    text = build_closing_execution(
        pm, old_records, fetcher,
        dt.datetime(2026, 9, 28, 14, 35, tzinfo=CN_TZ),
    )
    assert "仅为风险快照，不伪造早盘计划" in text
    assert "结论：风险退出" not in text


def test_new_history_fields_coexist_with_legacy_records():
    reports, _pm = _reports()
    moment = dt.datetime(2026, 9, 28, 10, 30, tzinfo=CN_TZ)
    legacy = make_records(reports, moment, "old")
    current = make_records(reports, moment, "new", EXECUTION_SESSION)
    assert legacy[0]["advice_session"] == "09:20_LEGACY"
    assert current[0]["advice_session"] == EXECUTION_SESSION
    assert legacy[0]["advice_id"] != current[0]["advice_id"]
    assert current[0]["data_quality"] == "HIGH"
    assert current[0]["decision_confidence"] == "MEDIUM"


def test_workflow_has_only_1030_and_1435_user_schedules():
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    morning = (workflows / "account-daily-telegram.yml").read_text()
    closing = (workflows / "account-intraday-telegram.yml").read_text()
    assert 'cron: "30 2 * * 1-5"' in morning
    assert 'cron: "35 6 * * 1-5"' in closing
    assert 'cron: "20 1 * * 1-5"' not in morning
    assert "notify-morning-execution" in morning
    assert "group: account-cloud-state" in morning and "group: account-cloud-state" in closing
