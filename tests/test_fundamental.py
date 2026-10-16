import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from quant_assistant.analysis.fundamental import evaluate, INDEX_ROUTES, FIELDS
from quant_assistant.analysis.final_decision import build_intraday_final_decision
from quant_assistant.data.fetcher import DataFetcher
from quant_assistant.models import Market
from test_market_filter import scenario, decide, NOW


def observations(cheap=True, deteriorating=False, index="931160"):
    dates = pd.bdate_range(end="2026-09-30", periods=300)
    rows = []
    for i, date in enumerate(dates):
        pe = (10 if cheap else 100) if i >= 279 else 30 + i / 100
        earnings = 8 if deteriorating and i >= 279 else 10
        rows.append(dict(date=date.date().isoformat(), pe=pe, close=pe * earnings, constituents=50))
    return dict(index_code=index, as_of="2026-09-30", history=rows,
                source="synthetic", profit_yoy=0, roe=10)


def etf_case(cheap=True, deteriorating=False, **kwargs):
    values = scenario(**kwargs)
    pos, record, *_ = values
    pos.code, pos.market, pos.asset_subtype = "515880", Market.ETF, "EQUITY_ETF"
    record.update(asset_subtype="EQUITY_ETF", fundamental_date=NOW.date(),
                  fundamental_observations=observations(cheap, deteriorating))
    return values


def test_cheap_stable_relative_weakness_holds_until_reclaim():
    result = decide(etf_case(bench=101))
    assert result["final_action"] == "HOLD"
    assert result["valuation_state"] == "CHEAP"
    assert result["fundamental_state"] == "STABLE"
    assert result["low_buy_state"] == "LOW_BUY_WATCH"
    assert result["tech_vs_fundamental_conflict"]


def test_expensive_deteriorating_weakness_reduces():
    values = etf_case(cheap=False, deteriorating=True, bench=101)
    values[-1]["time_of_day_confidence"] = "HIGH"
    result = decide(values)
    assert result["final_action"] == "REDUCE"
    assert result["decision_confidence"] == "HIGH"
    assert "基本面代理恶化" in result["action_reason"]


def test_reclaimed_cheap_stable_can_add_with_all_risk_gates():
    result = decide(etf_case(low=95, high=97.1), total_assets=1000000,
                    cash=500000, equity_exposure=.1)
    assert result["final_action"] == "ADD"
    assert result["low_buy_state"] == "CONFIRMED"


def test_extreme_valuation_blocks_strong_trend_chase():
    values = etf_case(cheap=False)
    pos, record, q, ma, evidence = values
    ma["ma_state"] = "结构转强"
    evidence.update(trend_break_candidate=False, sell_confirmations=[], mean_reversion_candidate=False)
    record["final_decision"] = {"final_action": "ADD"}
    result = build_intraday_final_decision(pos, record, q, "已进入买入区", ma, evidence)
    assert result["valuation_state"] == "EXTREME"
    assert result["final_action"] == "HOLD"


def test_invalidation_precedes_cheap_stable():
    assert decide(etf_case(price=89))["final_action"] == "RISK_EXIT"


def test_unknown_blocks_low_buy():
    values = etf_case(low=95)
    values[1]["fundamental_observations"] = {}
    result = decide(values, total_assets=1000000, cash=500000, equity_exposure=.1)
    assert result["final_action"] == "HOLD"
    assert result["fundamental_state"] == result["valuation_state"] == "UNKNOWN"


@pytest.mark.parametrize("subtype", ["GOLD_ETF", "BOND_ETF", "QDII_ETF", "COMMODITY_ETF"])
def test_special_routes_ignore_fundamental_observations(subtype):
    values = etf_case()
    values[0].asset_subtype = subtype
    result = evaluate(values[0], observations(), NOW.date())
    assert result["fundamental_state"] == result["valuation_state"] == "UNKNOWN"


def test_stale_future_mismatched_and_short_history_unknown():
    pos = etf_case()[0]
    for change in ({"as_of": "2025-01-01"}, {"as_of": "2027-01-01"},
                   {"index_code": "wrong"}, {"history": []}):
        result = evaluate(pos, {**observations(), **change}, NOW.date())
        assert result["valuation_state"] == "UNKNOWN"


def test_index_membership_count_change_degrades_earnings_proxy():
    data = observations()
    data["history"][-20]["constituents"] = 51
    assert evaluate(etf_case()[0], data, NOW.date())["fundamental_state"] == "UNKNOWN"


def test_missing_mapping_is_explicit():
    pos = etf_case()[0]
    pos.code = "999999"
    assert evaluate(pos, observations(), NOW.date())["fundamental_mapping_degraded"]


def test_cache_timeout_cooldown_and_stale_snapshot(tmp_path):
    fetcher = DataFetcher(tmp_path)
    pos = etf_case()[0]
    path = tmp_path / "fundamental_931160.json"
    old = {**observations(), "as_of": "2025-01-01"}
    path.write_text(json.dumps({"data": old}))
    with patch("urllib.request.urlopen", side_effect=TimeoutError) as request:
        assert fetcher.fetch_fundamental(pos, NOW) == old
        assert fetcher.fetch_fundamental(pos, NOW) == old
        assert request.call_count == 1
        assert request.call_args.kwargs["timeout"] == 12
    assert evaluate(pos, old, NOW.date())["valuation_state"] == "UNKNOWN"


def test_schedule_and_audit_fields():
    workflow = Path(".github/workflows/account-intraday-telegram.yml").read_text()
    assert 'schedule:' not in workflow
    assert 'workflow_dispatch:' in workflow
    assert "cancel-in-progress: false" in workflow
    assert {"fundamental_state", "valuation_percentile", "low_buy_state",
            "tech_vs_fundamental_conflict"} <= set(FIELDS)


def test_stock_earnings_quality_is_deterministic_without_invented_valuation():
    pos = scenario()[0]
    result = evaluate(pos, {"as_of": "2026-06-30", "profit_yoy": -20,
                            "roe": 10, "source": "synthetic"}, NOW.date())
    assert result["fundamental_state"] == "DETERIORATING"
    assert result["valuation_state"] == "UNKNOWN"
    assert result["fundamental_score"] is None


def test_report_fetches_fundamentals_and_saves_audit_without_raw_history():
    from types import SimpleNamespace
    from test_market_filter import reports, quote, frame, BENCHMARKS
    from quant_assistant.v3 import build_morning_execution, build_closing_execution
    from quant_assistant.advice_performance import make_records
    from quant_assistant.monthly_review import build_monthly_review
    report = reports(1)
    pos = report["views"][0]["position"]
    pos.code, pos.market, pos.asset_subtype = "515880", Market.ETF, "EQUITY_ETF"
    report["advices"][0]["code"] = pos.code
    fetcher = SimpleNamespace(fetch_hist=lambda *a, **k: frame(),
        fetch_intraday_quote=lambda code, market, now: quote(101 if code in BENCHMARKS else 97, now=now),
        fetch_fundamental=lambda *a: observations())
    detail = []
    text = build_morning_execution(report, fetcher, NOW, detail=detail)
    assert "结论：减仓" not in text
    assert "基本面 STABLE；估值 CHEAP；低吸状态 LOW_BUY_WATCH" in "\n".join(detail)
    saved = make_records(report, NOW, "synthetic", "10:30_EXECUTION")
    assert saved[0]["valuation_percentile"] is not None
    assert "fundamental_observations" not in saved[0]
    old = {"advice_id": "legacy", "as_of": "2026-10-08", "code": "515880", "reference_close": 97}
    review = build_monthly_review(saved + [old], [], {}, "2026-10", NOW.date())
    assert review["rows"][0]["fundamental_record_quality"] == "complete"
    assert review["rows"][1]["fundamental_record_quality"] == "legacy"
    assert review["rows"][1]["fundamental_state"] is None
    later = NOW.replace(hour=14, minute=10)
    fetcher.fetch_intraday_quote = lambda *a: quote(price=89, now=NOW)
    detail = []
    text = build_closing_execution(SimpleNamespace(positions=[pos], cash=100000), saved,
                                   fetcher, later, detail=detail)
    assert "结论：风险退出" not in text
    assert "数据降级：1只行情不足，暂不执行" in text
    assert "行情过旧" in "\n".join(detail)
