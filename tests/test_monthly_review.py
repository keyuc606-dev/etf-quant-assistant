import datetime as dt

import pandas as pd

from quant_assistant.monthly_review import (
    build_monthly_review, months_for_review, write_monthly_review,
)
from quant_assistant.trading_calendar import annual_trade_dates


def _frame(start: dt.date, count: int = 30):
    sessions = sorted(day for day in annual_trade_dates(2026) if day > start)[:count]
    return pd.DataFrame({
        "日期": sessions,
        "开盘": [100 + i for i in range(count)],
        "最高": [101 + i for i in range(count)],
        "最低": [99 + i for i in range(count)],
        "收盘": [100 + i for i in range(count)],
    })


def _record(advice_id, action="ADD", **overrides):
    record = {
        "advice_id": advice_id, "as_of": "2026-09-01", "code": "510300",
        "name": "沪深300ETF", "market": "ETF", "action_tendency": action,
        "final_action": action, "final_decision_version": "final-decision-v1",
        "data_quality": "HIGH", "decision_confidence": "HIGH",
        "asset_subtype": "EQUITY_ETF", "suggested_quantity": 100,
        "suggested_amount": 10_000, "reference_close": 100.0,
        "buy_zone": [99.0, 100.0], "reduce_zone": [100.0, 101.0],
        "target_price": 105.0, "invalidation_price": 95.0,
        "advice_session": "10:30_EXECUTION", "generated_at": "2026-09-01T10:30:00+08:00",
        "rule_version": "routing-v2", "t_economics": None,
    }
    record.update(overrides)
    return record


def test_monthly_review_separates_advice_execution_and_unexecuted(tmp_path):
    records = [
        _record("add-1"),
        _record("reduce-t", "REDUCE", rule_version="routing-v1",
                reduction_reason_type="TACTICAL_T", t_economics={"net": 20}),
        _record("hold-1", "HOLD", suggested_quantity=0),
    ]
    executions = [
        {"related_plan_id": "reduce-t", "side": "BUY", "quantity": 100,
         "price": 100.0, "fee": 2.0, "realized_pnl": None},
        {"related_plan_id": "reduce-t", "side": "SELL", "quantity": 100,
         "price": 102.0, "fee": 3.0, "realized_pnl": 195.0},
    ]
    report = build_monthly_review(records, executions, {"510300": _frame(dt.date(2026, 9, 1))},
                                  "2026-09", dt.date(2026, 10, 31))
    assert report["advice_sample_count"] == 3
    assert report["complete_sample_count"] == 3
    assert report["real_execution_sample_count"] == 1
    assert report["by_action"]["ADD"]["samples"] == 1
    assert report["by_action"]["REDUCE"]["samples"] == 1
    assert report["by_action"]["HOLD"]["mean_action_return"]["20"] > 0
    assert report["unexecuted"]["samples"] == 1  # unexecuted ADD; HOLD is not actionable
    assert report["t_trading"]["gross_pnl"] == 200.0
    assert report["t_trading"]["fees"] == 5.0
    assert report["t_trading"]["net_pnl"] == 195.0
    assert set(report["by_rule_version"]) == {"routing-v1", "routing-v2"}
    assert report["overall"]["mean_return"]["20"] is not None

    csv_path, md_path = write_monthly_review(report, tmp_path)
    assert csv_path.name == "strategy_review_2026-09.csv"
    assert "未执行明确建议：1 条" in md_path.read_text(encoding="utf-8")


def test_month_selection_supports_explicit_month_and_backfill():
    records = [
        {"as_of": "2026-09-01"}, {"as_of": "2026-08-31"},
        {"as_of": "2026-09-20"}, {"as_of": "broken"},
    ]
    assert months_for_review(records, requested="2026-09") == ["2026-09"]
    assert months_for_review(records, backfill=True) == ["2026-08", "2026-09"]


def test_backfill_marks_partial_and_legacy_without_inventing_context():
    partial = _record("partial")
    partial.pop("decision_confidence")
    legacy = _record("legacy")
    legacy.pop("reference_close")
    legacy.pop("generated_at")
    report = build_monthly_review([partial, legacy], [], {"510300": _frame(dt.date(2026, 9, 1))},
                                  "2026-09", dt.date(2026, 10, 31))
    assert report["complete_sample_count"] == 0
    assert report["legacy_sample_count"] == 2
    assert report["partial_backfill_count"] == 1
    assert report["missing_context_count"] == 1
    legacy_row = next(row for row in report["rows"] if row["advice_id"] == "legacy")
    assert legacy_row["record_quality"] == "legacy_record"
    assert legacy_row["return_20d"] is None


def test_windows_count_real_sessions_not_calendar_days():
    record = _record("holiday-window", as_of="2026-09-24")
    frame = _frame(dt.date(2026, 9, 24), 30)
    report = build_monthly_review([record], [], {"510300": frame},
                                  "2026-09", dt.date(2026, 10, 31))
    row = report["rows"][0]
    assert row["sessions_5"] == 5
    # 9/25-27 and 10/1-7 never enter the five-session window.
    fifth_session = sorted(pd.to_datetime(frame["日期"]).dt.date)[:5][-1]
    assert fifth_session == dt.date(2026, 10, 9)
