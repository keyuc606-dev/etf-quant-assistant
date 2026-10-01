import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd

from quant_assistant.etf_v2.backtest import run_walk_forward
from quant_assistant.etf_v2.engine import allocate_new_cash, scan_candidates
from quant_assistant.etf_v2.paper import new_paper_state, queue_allocation, settle_pending
from quant_assistant.etf_v2.report import render_compact


def frame(start="2025-01-02", periods=260, first=1.0, last=1.5, amount=80_000_000):
    dates = pd.bdate_range(start, periods=periods)
    close = np.linspace(first, last, periods)
    return pd.DataFrame({"日期": dates, "开盘": close * .999, "最高": close * 1.01,
                         "最低": close * .99, "收盘": close, "成交量": 50_000_000,
                         "成交额": amount})


def params(**overrides):
    base = {"min_history_days": 120, "min_avg_amount": 20_000_000,
            "min_candidate_score": 50, "max_candidates": 3, "single_etf_max": .15,
            "single_sector_max": .25, "total_equity_max": .60, "lot_size": 100,
            "commission_rate": .00025, "min_commission": 5, "slippage_rate": .001,
            "benchmark_code": "510300", "correlation_window": 60, "max_data_age_days": 7}
    base.update(overrides)
    return base


def test_scanner_filters_low_liquidity_and_keeps_cash_option():
    universe = {
        "510300": {"name": "宽基", "asset_class": "A股宽基", "sector": "宽基", "equity": True},
        "512480": {"name": "半导体", "asset_class": "A股行业", "sector": "半导体", "equity": True},
    }
    market = {"510300": frame(), "512480": frame(amount=1_000_000)}
    scan = scan_candidates(market, params=params(), universe=universe)
    assert scan["candidates"][0]["code"] == "510300"
    weak = next(row for row in scan["candidates"] if row["code"] == "512480")
    assert weak["eligible"] is False
    allocation = allocate_new_cash(scan, [], 100_000, params=params(), universe=universe)
    assert allocation["allocations"]
    assert allocation["cash_amount"] > 0
    assert "现金" in allocation["cash_reason"]


def test_stale_market_data_cannot_create_buy_candidate():
    universe = {"510300": {"name": "宽基", "asset_class": "A股宽基", "sector": "宽基", "equity": True}}
    scan = scan_candidates({"510300": frame()}, params=params(), universe=universe,
                           as_of_date=datetime.date(2026, 12, 31))
    assert scan["eligible"] == []
    assert "行情陈旧" in "；".join(scan["candidates"][0]["reasons"])


def test_equity_cap_blocks_more_equity_but_allows_defensive_etf():
    universe = {
        "510300": {"name": "宽基", "asset_class": "A股宽基", "sector": "宽基", "equity": True},
        "511010": {"name": "国债", "asset_class": "债券", "sector": "债券", "equity": False},
    }
    market = {code: frame() for code in universe}
    scan = scan_candidates(market, params=params(), universe=universe)
    existing = [SimpleNamespace(code="600000", shares=700, current_price=100,
                                sector="金融", asset_subtype="STOCK", cost_price=200)]
    allocation = allocate_new_cash(scan, existing, 30_000, params=params(), universe=universe)
    assert all(row["code"] != "510300" for row in allocation["allocations"])
    assert any(row["code"] == "511010" for row in allocation["allocations"])


def test_historical_cost_does_not_change_new_money_decision():
    universe = {"510300": {"name": "宽基", "asset_class": "A股宽基", "sector": "宽基", "equity": True}}
    scan = scan_candidates({"510300": frame()}, params=params(), universe=universe)
    low_cost = [SimpleNamespace(code="OTHER", shares=10, current_price=100,
                                sector="其他", asset_subtype="STOCK", cost_price=1)]
    high_cost = [SimpleNamespace(code="OTHER", shares=10, current_price=100,
                                 sector="其他", asset_subtype="STOCK", cost_price=1000)]
    first = allocate_new_cash(scan, low_cost, 100_000, params=params(), universe=universe)
    second = allocate_new_cash(scan, high_cost, 100_000, params=params(), universe=universe)
    assert first["allocations"] == second["allocations"]


def test_funds_subset_uses_full_account_cash_for_risk_denominator():
    universe = {"510300": {"name": "宽基", "asset_class": "A股宽基", "sector": "宽基", "equity": True}}
    scan = scan_candidates({"510300": frame()}, params=params(), universe=universe)
    existing = [SimpleNamespace(code="OTHER", shares=550, current_price=100,
                                sector="其他", asset_subtype="STOCK", cost_price=100)]
    without_account_cash = allocate_new_cash(scan, existing, 20_000, params=params(), universe=universe)
    with_account_cash = allocate_new_cash(scan, existing, 20_000, params=params(), universe=universe,
                                            portfolio_cash=100_000)
    assert without_account_cash["allocations"] == []
    assert with_account_cash["allocations"]


def test_paper_account_never_executes_on_signal_day():
    state = new_paper_state(100_000)
    allocation = {"allocations": [{"code": "510300", "amount": 10_000, "estimated_fee": 5}]}
    queue_allocation(state, allocation, "2025-01-03")
    same_day = frame(start="2025-01-03", periods=1)
    settle_pending(state, {"510300": same_day}, params())
    assert state["trades"] == []
    next_days = frame(start="2025-01-03", periods=2)
    settle_pending(state, {"510300": next_days}, params())
    assert state["trades"][0]["date"] > "2025-01-03"


def test_walk_forward_reports_out_of_sample_and_costs():
    market = {
        "510300": frame(periods=320, first=1, last=1.8),
        "511010": frame(periods=320, first=1, last=1.15),
        "518880": frame(periods=320, first=1, last=1.35),
    }
    result = run_walk_forward(market, initial_capital=100_000,
                              params=params(min_candidate_score=40))
    assert result["split_date"] > result["history"][0]["date"]
    assert result["out_of_sample"]["days"] > 20
    assert result["transaction_cost"] > 0
    assert result["trades"]


def test_compact_report_explains_manual_confirmation_and_old_loss_independence():
    scan = {"regime": {"state": "NEUTRAL", "reason": "测试"}}
    allocation = {"investable_cash": 20_000, "allocations": [], "cash_amount": 20_000,
                  "cash_reason": "没有 ETF 达到买入标准；当前不买"}
    text = render_compact(scan, allocation, 3)
    assert "历史亏损不参与" in text
    assert "系统不下单" in text
    assert "保留全部" in text
