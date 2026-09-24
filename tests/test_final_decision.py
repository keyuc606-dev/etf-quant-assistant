import datetime as dt
from types import SimpleNamespace

import pytest

from quant_assistant.advice_performance import make_records
from quant_assistant.analysis.final_decision import (
    FINAL_ACTIONS, FINAL_ACTION_PRIORITY, VERSION, build_final_decision, build_intraday_final_decision,
    estimate_t_economics,
)
from quant_assistant.asset_routing import BOND_ETF, GOLD_ETF, QDII_ETF, STOCK
from quant_assistant.data.fetcher import CN_TZ
from quant_assistant.models import Market, StockPosition
from quant_assistant.v3 import build_intraday


NOW = dt.datetime(2026, 9, 24, 14, 30, tzinfo=CN_TZ)


def position(shares=1000, price=10, market=Market.A_SH, subtype=STOCK):
    return StockPosition(
        code="600000" if market != Market.ETF else "510300",
        name="测试标的", market=market, shares=shares, cost_price=9,
        current_price=price, asset_type="ETF" if market == Market.ETF else "STOCK",
        asset_subtype=subtype,
    )


def inputs(pos=None, price=10, action="HOLD", buy=(9.5, 9.9), reduce=(10.5, 11),
           stop=9, target=11, ma_state="均线结构未触发", ma_hint="按规则观察",
           discipline_state="正常观察", forbidden="禁止仅凭成本价加仓"):
    pos = pos or position(price=price)
    pos.current_price = price
    view = {"position": pos, "available": True, "weight": .1,
            "asset_subtype": pos.asset_subtype}
    advice = {"code": pos.code, "action": action, "confidence": "中",
              "buy_range": buy, "reduce_range": reduce, "stop": stop,
              "target": target, "reasons": ["确定性测试信号"],
              "display_conflict": False}
    discipline = {"discipline_state": discipline_state, "forbidden_action": forbidden}
    ma = {"ma_state": ma_state, "ma_action_hint": ma_hint,
          "ma_forbidden_action": "", "ma_conflict_flag": False}
    return view, advice, discipline, ma


def test_final_action_enum_and_watch_semantics_collapse_to_no_action():
    assert FINAL_ACTIONS == ("HOLD", "ADD", "REDUCE", "RISK_EXIT", "NO_ACTION", "MANUAL_REVIEW")
    assert FINAL_ACTION_PRIORITY == ("RISK_EXIT", "REDUCE", "MANUAL_REVIEW", "ADD", "HOLD", "NO_ACTION")
    decision = build_final_decision(*inputs(action="WATCH"), total_assets=100000, cash=10000)
    assert decision["final_action"] == "NO_ACTION"
    assert "观望" not in decision["final_action_label"]
    assert decision["manual_confirmation_required"] is True


def test_buy_zone_with_ma20_break_never_adds():
    decision = build_final_decision(*inputs(
        price=9.7, action="ADD_SMALL", ma_state="MA20有效跌破/趋势破坏",
        ma_hint="趋势失效，优先减仓复核"), total_assets=100000, cash=20000)
    assert decision["final_action"] == "NO_ACTION"
    assert "禁止加仓" in decision["action_reason"]


def test_target_and_persistent_overheat_reduces_but_single_burst_holds():
    hot = build_final_decision(*inputs(
        price=11, ma_state="persistent_overheat", ma_hint="分批锁利复核"))
    assert hot["final_action"] == "REDUCE"
    assert hot["reduction_reason_type"] == "PROFIT_TAKING"
    burst = build_final_decision(*inputs(
        price=11, ma_state="single_day_momentum_burst",
        ma_hint="单日强势脉冲，等待次日确认"))
    assert burst["final_action"] == "HOLD"
    assert "不追高" in burst["action_reason"]


def test_small_tactical_trade_fails_dual_gate_and_large_trade_passes():
    small = estimate_t_economics(position(shares=1000, price=10), 10, 9.87)
    assert small["suggested_quantity"] == 500
    assert small["sell_amount"] == 5000
    assert small["status"] == "INSUFFICIENT"
    assert small["expected_gap_pct"] == pytest.approx(.013)
    large = estimate_t_economics(position(shares=10000, price=10), 10, 9.5)
    assert large["status"] == "SUFFICIENT"
    assert large["estimated_net_profit"] >= 100
    assert large["estimated_round_trip_cost"] > 0


def test_morning_tactical_window_uses_economics_gate():
    small_pos = position(shares=1000, price=10)
    small = build_final_decision(*inputs(
        pos=small_pos, price=10, buy=(9.5, 9.87), reduce=(10, 10.5), target=11),
        total_assets=10000, cash=0)
    assert small["final_action"] == "HOLD"
    assert small["reduction_reason_type"] == "TACTICAL_T"
    assert small["t_economics"]["sell_amount"] == 5000
    large_pos = position(shares=10000, price=10)
    large = build_final_decision(*inputs(
        pos=large_pos, price=10, buy=(9.2, 9.5), reduce=(10, 10.5), target=11),
        total_assets=100000, cash=0)
    assert large["final_action"] == "REDUCE"
    assert large["reduction_reason_type"] == "TACTICAL_T"
    assert large["t_economics"]["status"] == "SUFFICIENT"


def test_tactical_gate_rounding_and_unreliable_inputs_default_to_no_t():
    odd = estimate_t_economics(position(shares=350, price=20, market=Market.ETF), 20, 19)
    assert odd["trade_unit"] == 100
    assert odd["suggested_quantity"] == 100
    assert odd["suggested_quantity"] % 100 == 0
    missing = estimate_t_economics(position(shares=1000), 10, None)
    assert missing["status"] == "NOT_EVALUABLE"


def test_risk_exit_small_position_is_not_blocked_by_t_economics():
    pos = position(shares=100, price=8.9)
    decision = build_final_decision(*inputs(pos=pos, price=8.9, stop=9),
                                    total_assets=10000, cash=0)
    assert decision["final_action"] == "RISK_EXIT"
    assert decision["reduction_reason_type"] == "RISK_CONTROL"
    assert decision["suggested_quantity"] == 100
    assert decision["t_economics"] is None
    partial = build_final_decision(*inputs(
        pos=position(shares=100, price=10), price=10, buy=(8, 9), stop=8,
        ma_state="MA20有效跌破/趋势破坏"), total_assets=10000, cash=0)
    assert partial["final_action"] == "REDUCE"
    assert partial["suggested_quantity"] == 100
    assert partial["suggested_fraction"] == 1.0


@pytest.mark.parametrize("subtype", [GOLD_ETF, QDII_ETF])
def test_special_asset_routes_do_not_reenter_stock_logic(subtype):
    pos = position(market=Market.ETF, subtype=subtype)
    decision = build_final_decision(*inputs(pos=pos, price=10, action="WATCH",
                                             ma_state="MA20有效跌破/趋势破坏"))
    assert decision["final_action"] == "NO_ACTION"
    assert decision["reduction_reason_type"] is None
    assert decision["t_economics"] is None


def test_bond_route_never_uses_t_economics():
    pos = position(market=Market.ETF, subtype=BOND_ETF)
    decision = build_final_decision(*inputs(pos=pos, price=10, action="HOLD"))
    assert decision["final_action"] == "HOLD"
    assert decision["t_economics"] is None


def test_intraday_tactical_gate_blocks_small_trade_but_risk_exit_executes():
    pos = position(shares=1000, price=10)
    record = {
        "as_of": NOW.date().isoformat(), "code": pos.code, "asset_subtype": STOCK,
        "buy_zone": [9.5, 9.87], "reduce_zone": [10, 10.5],
        "invalidation_price": 9, "target_price": 11, "confidence": "中",
        "final_decision": {"final_action": "HOLD", "action_reason": "晨报持有"},
    }
    held = build_intraday_final_decision(
        pos, record, {"price": 10}, "已进入减仓区", {"ma_state": "均线结构未触发"})
    assert held["final_action"] == "HOLD"
    assert held["reduction_reason_type"] == "TACTICAL_T"
    assert held["t_economics"]["status"] == "INSUFFICIENT"
    exited = build_intraday_final_decision(
        pos, record, {"price": 8.9}, "已失效", {"ma_state": "MA20有效跌破/趋势破坏"})
    assert exited["final_action"] == "RISK_EXIT"
    assert exited["suggested_quantity"] == 1000
    assert exited["t_economics"] is None


def test_history_persists_final_decision_without_changing_old_tendency():
    pos = position(shares=1000, price=10)
    view, advice, discipline, ma = inputs(pos=pos, action="HOLD")
    advice.update(build_final_decision(view, advice, discipline, ma, 100000, 10000))
    report = {"advices": [advice], "views": [{**view, "data_date": "2026-09-23",
               "indicators": {"ATR": 1}}], "stock_data": {},
              "disciplines": {pos.code: {"version": "discipline-v1"}},
              "ma_disciplines": {pos.code: {"version": "ma-discipline-v2"}}}
    row = make_records(report, NOW, "test")[0]
    assert row["action_tendency"] == "HOLD"
    assert row["final_decision_version"] == VERSION
    assert row["final_action"] == "HOLD"
    assert "t_economics" in row and row["final_decision"]["trigger_condition"]


def test_intraday_compact_output_centers_final_action_and_t_economics():
    pos = position(shares=1000, price=10)
    record = {
        "as_of": NOW.date().isoformat(), "code": pos.code, "asset_subtype": STOCK,
        "asset_type": "STOCK", "buy_zone": [9.5, 9.87],
        "reduce_zone": [10, 10.5], "invalidation_price": 9, "target_price": 11,
        "reference_volume": 1000, "confidence": "中",
        "discipline": {"forbidden_action": "禁止仅凭成本价加仓"},
        "technical_features": {"MA5": 9.9, "MA10": 9.8, "MA20": 9.5, "ATR": .5},
        "ma_discipline": {"ma_state": "均线结构未触发", "ma_action_hint": "按规则观察"},
        "final_decision": {"final_action": "HOLD", "action_reason": "晨报持有"},
    }
    quote = {"price": 10, "change_pct": 1, "volume": 500, "amount": 50000,
             "as_of": NOW.isoformat(), "provisional": True, "source": "test"}
    fetcher = SimpleNamespace(fetch_intraday_quote=lambda *args: quote)
    text = build_intraday(SimpleNamespace(positions=[pos]), [record], fetcher, NOW)
    assert "最终结论：持有" in text
    assert "做T经济性：不足" in text
    assert "取消条件：" in text
