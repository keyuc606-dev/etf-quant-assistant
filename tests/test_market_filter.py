"""Synthetic execution scenarios; no real account data or network calls."""
import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from quant_assistant.analysis.market_filter import (
    market_environment, execution_evidence, relative_strength, history_facts,
    previous_session, benchmark_route, DIAGNOSTIC_FIELDS,
)
from quant_assistant.analysis.final_decision import build_intraday_final_decision
from quant_assistant.analysis.ma_discipline import intraday_ma_discipline
from quant_assistant.data.fetcher import CN_TZ
from quant_assistant.models import Market, StockPosition
from quant_assistant.monthly_review import build_monthly_review, render_markdown
from quant_assistant.advice_performance import make_records
from quant_assistant.v3 import build_morning_execution, build_closing_execution, _decision_confidence

NOW = dt.datetime(2026, 10, 8, 10, 30, tzinfo=CN_TZ)
BENCHMARKS = ("510300", "510500", "159915", "588000")


def quote(price=97, previous=100, now=NOW, **extra):
    return {"price": price, "previous_close": previous, "open": price - .1,
            "low": price - .2, "high": price + .1, "volume": 250,
            "session_progress": .25, "provisional": True, "source": "test",
            "as_of": now.isoformat(), **extra}


def frame(last=100):
    dates = pd.bdate_range("2026-09-03", "2026-09-30")
    dates = dates[dates != pd.Timestamp("2026-09-25")]
    return pd.DataFrame({"日期": dates, "收盘": last, "最低": last - 4,
                         "最高": last + 1, "成交量": 1000})


def scenario(price=97, bench=97, support=96, regime=None, **extra):
    pos = StockPosition("000725", "测试权益", Market.A_SZ, 1000, 100, price,
                        asset_subtype="STOCK")
    facts = history_facts(frame())
    record = {**facts, "code": pos.code, "as_of": "2026-10-08", "advice_session": "10:30_EXECUTION",
              "asset_subtype": "STOCK", "confidence": "高", "data_quality": "HIGH",
              "technical_features": {"MA5": 99, "MA10": 98, "MA20": 100, "ATR": 4, "RSI14": 30},
              "reference_volume": 1000, "support_level": support, "invalidation_price": 90,
              "buy_zone": [96, 98], "reduce_zone": [110, 112], "target_price": 112,
              "final_decision": {"final_action": "HOLD", "confidence": "高"}, "discipline": {}}
    record.update(extra.pop("record", {}))
    q = quote(price, **extra)
    quotes = {code: quote(bench) for code in BENCHMARKS}
    environment = market_environment(quotes, NOW)
    if regime:
        environment["market_regime"] = regime
    ma = {"ma_state": "MA20有效跌破/趋势破坏"}
    evidence = execution_evidence(pos, record, q, ma, environment,
                                  {code: frame() for code in BENCHMARKS}, quotes, NOW)
    return pos, record, q, ma, evidence


def decide(values, **limits):
    pos, record, q, ma, evidence = values
    return build_intraday_final_decision(pos, record, q, "未触发", ma, evidence,
                                        **limits)


def test_broad_selloff_comparable_loss_only_ma20_holds_low():
    values = scenario()
    result = decide(values)
    assert result["market_regime"] == "BROAD_SELLOFF"
    assert result["relative_strength_state"] == "REL_NEUTRAL"
    assert result["ma20_break_only"] is True
    assert result["final_action"] == "HOLD"
    assert result["decision_confidence"] == "LOW"
    assert "单独破位不足以减仓" in result["action_reason"]
    assert _decision_confidence(result, "未触发", values[3]) == "LOW"


def test_broad_selloff_relative_weak_and_prior_low_break_reduces_high():
    result = decide(scenario(price=94, support=96))
    assert result["relative_strength_state"] == "REL_WEAK"
    assert result["support_break"] is True
    assert result["final_action"] == "REDUCE"
    assert result["decision_confidence"] == "HIGH"


def test_independent_weakness_in_strong_market_reduces_high():
    result = decide(scenario(price=97, bench=101))
    assert result["market_regime"] == "UP"
    assert result["final_action"] == "REDUCE"
    assert result["decision_confidence"] == "HIGH"


def test_invalidation_always_exits_even_with_forced_mean_reversion_candidate():
    values = scenario(price=89)
    values[-1]["mean_reversion_candidate"] = True
    result = decide(values, total_assets=1000000, cash=500000, equity_exposure=.1)
    assert result["final_action"] == "RISK_EXIT"
    assert result["suggested_quantity"] == 1000


def test_strict_reclaimed_oversold_candidate_can_add_under_ma20():
    values = scenario(low=95, high=97.1)
    assert values[-1]["mean_reversion_candidate"] is True
    result = decide(values, total_assets=1000000, cash=500000, equity_exposure=.1)
    assert result["final_action"] == "ADD"
    assert result["decision_confidence"] == "MEDIUM"
    assert "未成交不追价" in result["cancel_condition"]
    assert result["suggested_quantity"] % 100 == 0


@pytest.mark.parametrize("extra", [{"volume": 600}, {"price": 94}, {"open": 98}, {"high": 100}])
def test_falling_high_volume_or_relative_weak_never_adds(extra):
    values = scenario(low=93, **extra)
    assert values[-1]["mean_reversion_candidate"] is False
    assert decide(values, total_assets=1000000, cash=500000, equity_exposure=.1)["final_action"] != "ADD"


@pytest.mark.parametrize("limits", [{"cash": 0}, {"equity_exposure": .35},
                                   {"total_assets": 10000}, {"equity_exposure": None}])
def test_mean_reversion_requires_account_gates(limits):
    kwargs = dict(total_assets=1000000, cash=500000, equity_exposure=.1)
    kwargs.update(limits)
    assert decide(scenario(low=95), **kwargs)["final_action"] == "HOLD"


@pytest.mark.parametrize("subtype", ["BOND_ETF", "GOLD_ETF", "QDII_ETF", "COMMODITY_ETF"])
def test_non_a_share_routes_never_use_relative_weakness(subtype):
    values = scenario(price=89)
    pos, record, q, ma, _ = values
    pos.market, pos.asset_subtype = Market.ETF, subtype
    record["asset_subtype"] = subtype
    evidence = execution_evidence(pos, record, q, ma)
    result = build_intraday_final_decision(pos, record, q, "已失效", ma, evidence)
    assert evidence["filter_applicability"] == "not_applicable"
    assert evidence["relative_strength_state"] == "UNKNOWN"
    assert result["final_action"] not in ("REDUCE", "RISK_EXIT", "ADD")


def test_benchmark_mapping_fallback_and_stale_unknown():
    values = scenario()
    pos = values[0]
    pos.code, pos.asset_subtype, pos.market = "159782", "EQUITY_ETF", Market.ETF
    assert benchmark_route(pos) == ("159915", False)
    pos.code = "599999"
    assert benchmark_route(pos) == ("510500", True)
    quotes = {code: quote() for code in BENCHMARKS}
    quotes["510500"]["as_of"] = "2026-09-30T10:30:00+08:00"
    env = market_environment(quotes, NOW)
    assert env["market_regime"] == "UNKNOWN"
    assert env["market_breadth"] == {"status": "UNKNOWN"}
    rel = relative_strength(values[1], frame(), values[2], quotes["510500"], NOW)
    assert rel["relative_strength_state"] == "UNKNOWN"


def test_relative_returns_align_actual_sessions_and_do_not_fill_missing_bars():
    record = history_facts(frame(98))
    rel = relative_strength(record, frame(), quote(98), quote(97), NOW)
    assert rel["relative_return_day"] == pytest.approx(.01)
    assert rel["relative_return_5d"] == pytest.approx(0)
    assert rel["relative_return_10d"] == pytest.approx(0)
    record["history_dates"].pop(-3)
    record["history_closes"].pop(-3)
    rel = relative_strength(record, frame(), quote(98), quote(97), NOW)
    assert rel["relative_return_5d"] is None
    assert previous_session(NOW.date()) == dt.date(2026, 9, 30)


def test_afternoon_reclaim_clears_stale_ma20_and_cancels_risk_reduce():
    pos, record, q, _, _ = scenario()
    record["ma_discipline"] = {"ma_state": "MA20有效跌破/趋势破坏"}
    record["final_decision"] = {"final_action": "REDUCE", "confidence": "高"}
    q.update(price=101, high=101, low=96)
    ma = intraday_ma_discipline(record, q, "未触发")
    assert "MA20有效跌破" not in ma["ma_state"]
    assert build_intraday_final_decision(pos, record, q, "未触发", ma)["final_action"] == "NO_ACTION"


def test_monthly_new_diagnostics_and_legacy_unchanged():
    values = scenario()
    result = decide(values)
    record = {**values[1], **result, "advice_id": "new", "reference_close": 97,
              "rule_version": "test", "generated_at": NOW.isoformat(), "market": "A_SZ"}
    legacy = {"advice_id": "old", "as_of": "2026-10-08", "code": "000725", "reference_close": 97}
    report = build_monthly_review([record, legacy], [], {}, "2026-10", dt.date(2026, 10, 8))
    assert report["rows"][0]["ma20_break_only"] is True
    assert report["rows"][0]["final_action"] == "HOLD"
    assert report["rows"][1]["market_regime"] is None
    assert report["rows"][1]["filter_record_quality"] == "legacy"
    assert "market_regime" in render_markdown(report)
    assert not any(field in legacy for field in DIAGNOSTIC_FIELDS)


def reports(count=6):
    views, advices, data = [], [], {}
    for index in range(count):
        pos, record, _, ma, _ = scenario()
        pos.code = f"000{index+1:03d}"
        data[pos.code] = frame()
        views.append({"position": pos, "available": True, "weight": .02,
                      "data_date": "2026-09-30", "indicators": record["technical_features"]})
        advices.append({"code": pos.code, "action": "HOLD", "confidence": "高", "data_quality": "HIGH",
                        "buy_range": [96, 98], "reduce_range": [110, 112], "stop": 90, "target": 112,
                        "final_action": "HOLD", "decision_confidence": "HIGH"})
    return {"views": views, "advices": advices, "stock_data": data, "execution_cash": 1000000}


def test_reports_hide_ma20_only_and_cap_confirmed_reductions_at_five():
    report = reports()
    fetcher = SimpleNamespace(fetch_hist=lambda *a, **kw: frame(),
        fetch_intraday_quote=lambda code, market, now: quote(now=now))
    text = build_morning_execution(report, fetcher, NOW)
    assert "结论：减仓" not in text
    assert all(row["final_action"] == "HOLD" and row["decision_confidence"] == "LOW" for row in report["advices"])
    fetcher.fetch_intraday_quote = lambda code, market, now: quote(97 if code in BENCHMARKS else 94, now=now)
    report = reports()
    text = build_morning_execution(report, fetcher, NOW)
    assert text.count("结论：减仓") == 5
    saved = make_records(report, NOW, "test", "10:30_EXECUTION")
    assert saved[0]["market_regime"] == "BROAD_SELLOFF"
    assert saved[0]["final_decision"]["relative_strength_state"] == "REL_WEAK"
    later = NOW.replace(hour=14, minute=35)
    pm = SimpleNamespace(positions=[v["position"] for v in report["views"]], cash=1000000)
    text = build_closing_execution(pm, saved, fetcher, later)
    assert text.count("结论：减仓") == 5
    assert "显著跑输基准" in text
