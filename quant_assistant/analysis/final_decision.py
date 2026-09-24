"""Single deterministic final action for account advice; never places orders."""

from __future__ import annotations

import math
from typing import Any

from ..asset_routing import (BOND_ETF, COMMODITY_ETF, GOLD_ETF, QDII_ETF,
                             subtype_for)
from ..config import FINAL_DECISION_PARAMS


VERSION = FINAL_DECISION_PARAMS["version"]
FINAL_ACTIONS = ("HOLD", "ADD", "REDUCE", "RISK_EXIT", "NO_ACTION", "MANUAL_REVIEW")
FINAL_ACTION_PRIORITY = ("RISK_EXIT", "REDUCE", "MANUAL_REVIEW", "ADD", "HOLD", "NO_ACTION")
ACTION_LABELS = {
    "HOLD": "持有", "ADD": "加仓", "REDUCE": "减仓",
    "RISK_EXIT": "风险退出", "NO_ACTION": "暂不操作",
    "MANUAL_REVIEW": "人工复核",
}
REDUCTION_REASON_TYPES = ("RISK_CONTROL", "PROFIT_TAKING", "TACTICAL_T")

_PARTIAL_SUBTYPES = {GOLD_ETF, COMMODITY_ETF, QDII_ETF}


def _number(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _market_name(position) -> str:
    market = getattr(position, "market", "")
    return getattr(market, "name", str(market))


def trade_unit(position) -> int:
    return int(FINAL_DECISION_PARAMS[
        "etf_lot_size" if _market_name(position) == "ETF" else "stock_lot_size"
    ])


def executable_quantity(shares: int | float, fraction: float, unit: int) -> int:
    raw = max(0, int(float(shares) * fraction))
    return min(int(shares), (raw // unit) * unit)


def estimate_t_economics(position, sell_price: float | None, buyback_price: float | None,
                         fraction: float | None = None) -> dict:
    """Estimate one sell/buyback round trip in CNY using centralized costs."""
    cfg = FINAL_DECISION_PARAMS
    sell = _number(sell_price)
    buy = _number(buyback_price)
    fraction = float(fraction if fraction is not None else cfg["tactical_t_fraction"])
    unit = trade_unit(position)
    quantity = executable_quantity(getattr(position, "shares", 0), fraction, unit)
    shares = int(getattr(position, "shares", 0) or 0)
    actual_fraction = quantity / shares if shares else 0.0
    base = {
        "status": "NOT_EVALUABLE", "label": "不可评估",
        "trade_unit": unit, "suggested_fraction": actual_fraction,
        "suggested_quantity": quantity, "sell_amount": None,
        "expected_gap_pct": None, "required_gap_pct": None,
        "estimated_round_trip_cost": None, "estimated_gross_profit": None,
        "estimated_net_profit": None,
        "min_net_profit": cfg["t_min_net_profit"],
        "reason": "缺少可靠卖出价、回补价或可执行整手数量，默认不做T",
    }
    if not sell or not buy or sell <= 0 or buy <= 0 or buy >= sell or quantity < unit:
        return base
    sell_amount = quantity * sell
    buy_amount = quantity * buy
    commission_rate = cfg["commission_rate"]
    sell_commission = max(sell_amount * commission_rate, cfg["min_commission"])
    buy_commission = max(buy_amount * commission_rate, cfg["min_commission"])
    stock = _market_name(position) != "ETF"
    stamp_tax = sell_amount * cfg["stock_stamp_tax_rate"] if stock else 0.0
    transfer = ((sell_amount + buy_amount) * cfg["sh_transfer_fee_rate"]
                if _market_name(position) == "A_SH" else 0.0)
    slippage = (sell_amount + buy_amount) * cfg["slippage_rate_per_side"]
    cost = sell_commission + buy_commission + stamp_tax + transfer + slippage
    gross = quantity * (sell - buy)
    net = gross - cost
    gap = (sell - buy) / sell
    cost_rate = cost / sell_amount
    required_gap = max(cfg["t_gap_floor"], cfg["t_cost_safety_multiple"] * cost_rate)
    sufficient = gap >= required_gap and net >= cfg["t_min_net_profit"]
    return {
        **base,
        "status": "SUFFICIENT" if sufficient else "INSUFFICIENT",
        "label": "值得" if sufficient else "不足",
        "sell_amount": round(sell_amount, 2),
        "expected_gap_pct": gap,
        "required_gap_pct": required_gap,
        "estimated_round_trip_cost": round(cost, 2),
        "estimated_gross_profit": round(gross, 2),
        "estimated_net_profit": round(net, 2),
        "reason": ("预计价差与净收益均达到保守门槛" if sufficient else
                   "预计价差或净收益未达到保守门槛，战术减仓不执行"),
    }


def _decision(action: str, reason: str, confidence: str, trigger: str,
              cancel: str, conflict: str = "无", reduction_type: str | None = None,
              quantity: int = 0, amount: float = 0.0, fraction: float = 0.0,
              economics: dict | None = None) -> dict:
    if action not in FINAL_ACTIONS:
        raise ValueError(f"未知最终动作: {action}")
    if reduction_type is not None and reduction_type not in REDUCTION_REASON_TYPES:
        raise ValueError(f"未知减仓目的: {reduction_type}")
    size = "0股 / ￥0 / 0%"
    if quantity or amount or fraction:
        size = f"{quantity:,}股≈￥{amount:,.0f}（{fraction:.0%}）"
    return {
        "final_decision_version": VERSION,
        "final_action": action,
        "final_action_label": ACTION_LABELS[action],
        "action_reason": reason,
        "action_size": size,
        "suggested_quantity": int(quantity),
        "suggested_amount": round(float(amount), 2),
        "suggested_fraction": float(fraction),
        "trigger_condition": trigger,
        "cancel_condition": cancel,
        "confidence": confidence,
        "conflict_note": conflict,
        "reduction_reason_type": reduction_type,
        "t_economics": economics,
        "manual_confirmation_required": True,
    }


def _sell_size(position, price: float, fraction: float) -> tuple[int, float, float]:
    shares = int(getattr(position, "shares", 0) or 0)
    unit = trade_unit(position)
    quantity = executable_quantity(shares, fraction, unit)
    # Risk/locking-profit reductions must remain executable when the theoretical
    # fraction is smaller than one lot. Tactical T keeps the zero-lot result and
    # is blocked by estimate_t_economics instead.
    if quantity == 0 and shares >= unit and fraction > 0:
        quantity = unit
    actual_fraction = quantity / shares if shares else 0.0
    return quantity, quantity * price, actual_fraction


def build_final_decision(view: dict, advice: dict, discipline: dict, ma: dict,
                         total_assets: float = 0.0, cash: float = 0.0) -> dict:
    """Collapse the 09:20 close-only layers into exactly one public action."""
    pos = view["position"]
    subtype = view.get("asset_subtype") or subtype_for(pos)
    confidence = advice.get("confidence", "低")
    price = _number(getattr(pos, "current_price", None))
    buy, reduce = advice.get("buy_range"), advice.get("reduce_range")
    stop, target = _number(advice.get("stop")), _number(advice.get("target"))
    ma_state = ma.get("ma_state", "")
    ma_hint = ma.get("ma_action_hint", "")
    forbidden = discipline.get("forbidden_action", "") + "；" + ma.get("ma_forbidden_action", "")
    conflict = bool(advice.get("display_conflict") or ma.get("ma_conflict_flag"))
    unavailable = not view.get("available") or not price

    if unavailable:
        return _decision("MANUAL_REVIEW", "行情或关键指标不足，无法形成可靠动作", "低",
                         "补齐上一完整交易日行情与关键指标", "数据仍不完整", "数据不足")
    if subtype == BOND_ETF:
        if advice.get("action") == "HOLD":
            return _decision("HOLD", "债券ETF按防守仓位角色持有，不套用股票减仓或做T逻辑",
                             confidence, "账户防守占比、流动性与利率风险无异常",
                             "防守占比、流动性、折溢价或利率/久期风险显著变化")
        return _decision("MANUAL_REVIEW", "债券ETF专用宏观/久期数据不足，需人工复核防守仓位",
                         "低", "补齐利率、久期、流动性与折溢价信息",
                         "专用风险数据仍不可核验", "资产专用数据不足")
    if subtype in _PARTIAL_SUBTYPES:
        if conflict:
            return _decision("MANUAL_REVIEW", "资产专用趋势与原建议冲突，不能套用A股逻辑解消",
                             "低", "核验资产专用风险后再决定", "专用风险仍不可核验",
                             "资产专用规则冲突")
        action = "HOLD" if advice.get("action") == "HOLD" else "NO_ACTION"
        return _decision(action, advice.get("reasons", ["按资产专用趋势与组合暴露管理"])[0],
                         confidence, "趋势、ATR、组合暴露及资产专用风险共同确认",
                         "时差、汇率、折溢价或宏观驱动无法核验")

    in_buy = bool(buy and buy[0] <= price <= buy[1])
    in_reduce = bool(reduce and price >= reduce[0])
    stop_broken = bool(stop and price <= stop)
    target_reached = bool(target and price >= target)
    trend_broken = "MA20有效跌破" in ma_state
    deep_broken = discipline.get("discipline_state") == "深套/趋势破坏"
    persistent_hot = ma_state == "persistent_overheat"
    single_burst = ma_state == "single_day_momentum_burst"

    if stop_broken or (deep_broken and trend_broken):
        qty, amount, fraction = _sell_size(pos, price, 1.0)
        return _decision("RISK_EXIT", "失效位或多重趋势底线已破坏，风险控制优先",
                         confidence, "价格确认失效位/关键支撑失守", "重新站回失效位并完成结构复核",
                         reduction_type="RISK_CONTROL", quantity=qty, amount=amount, fraction=fraction)
    if in_buy and trend_broken:
        return _decision("NO_ACTION", "价格虽进入买入区，但MA20趋势已破坏，禁止加仓",
                         "低", "重新站回MA20并恢复风险收益结构", "再次跌破失效位",
                         "买入区与趋势破坏冲突")
    if trend_broken:
        qty, amount, fraction = _sell_size(pos, price, FINAL_DECISION_PARAMS["risk_reduce_fraction"])
        return _decision("REDUCE", "MA20有效跌破，先降低风险而非等待做T价差",
                         confidence, "趋势破坏维持且持仓仍可执行", "重新站回MA20并完成结构确认",
                         reduction_type="RISK_CONTROL", quantity=qty, amount=amount,
                         fraction=fraction)
    if (target_reached or in_reduce) and persistent_hot:
        fraction = FINAL_DECISION_PARAMS["profit_taking_fraction"]
        qty, amount, fraction = _sell_size(pos, price, fraction)
        return _decision("REDUCE", "目标/减仓区已触发且持续性过热，分批锁定利润",
                         confidence, "目标或减仓区与持续过热同时成立", "价格退出减仓区或持续过热解除",
                         reduction_type="PROFIT_TAKING", quantity=qty, amount=amount, fraction=fraction)
    if single_burst:
        return _decision("HOLD", "单日强势脉冲不等同持续过热，持有但不追高",
                         confidence, "等待下一完整交易日确认持续性", "出现冲高回落/炸板或趋势失效")
    if conflict:
        return _decision("MANUAL_REVIEW", "建议、纪律或均线层存在无法确定性解消的冲突",
                         "低", "冲突解除且各层规则一致", "冲突仍存在", "多层信号冲突")
    if (target_reached or in_reduce) and advice.get("action") != "REDUCE":
        buyback = _number(buy[1]) if buy and len(buy) == 2 else None
        econ = estimate_t_economics(pos, price, buyback)
        if econ["status"] != "SUFFICIENT":
            return _decision("HOLD", "技术上出现战术减仓窗口，但做T经济性不足或不可评估",
                             confidence, "价差与预计净收益同时达到配置门槛",
                             "价格区间或成本估算失效", "战术减仓被经济性闸门阻断",
                             reduction_type="TACTICAL_T", economics=econ)
        fraction = FINAL_DECISION_PARAMS["tactical_t_fraction"]
        return _decision("REDUCE", "技术触发且做T价差、净收益均达到经济性门槛",
                         confidence, "减仓区触发且经济性门槛满足", "回补价差或净收益低于门槛",
                         reduction_type="TACTICAL_T", quantity=econ["suggested_quantity"],
                         amount=econ["sell_amount"], fraction=econ["suggested_fraction"], economics=econ)
    if advice.get("action") == "ADD_SMALL":
        blocked = any(token in forbidden for token in ("禁止加仓", "禁止追高", "越跌越补", "继续摊平"))
        if not in_buy or blocked or cash <= 0:
            return _decision("NO_ACTION", "加仓条件未同时通过买入区、纪律、现金与仓位闸门",
                             confidence, "进入买入区且趋势、纪律、现金与仓位均确认",
                             "跌破失效位或任一加仓闸门失效", "加仓候选尚未完成确认")
        budget = min(cash, total_assets * FINAL_DECISION_PARAMS["add_fraction_of_assets"])
        unit = trade_unit(pos)
        qty = int(budget / price) // unit * unit
        if qty < unit:
            return _decision("NO_ACTION", "可用现金不足一个交易单位，无法形成可执行加仓数量",
                             confidence, "现金足以购买至少一个交易单位", "趋势或买入区失效")
        amount = qty * price
        return _decision("ADD", "买入区、趋势纪律、现金和仓位闸门均已通过",
                         confidence, "在买入区内企稳并人工确认", "跌破失效位或离开买入区",
                         quantity=qty, amount=amount, fraction=amount / total_assets if total_assets else 0)
    if advice.get("action") == "REDUCE":
        fraction = FINAL_DECISION_PARAMS["profit_taking_fraction"]
        qty, amount, fraction = _sell_size(pos, price, fraction)
        return _decision("REDUCE", "原规则减仓信号成立，按分批锁利处理",
                         confidence, "原减仓条件继续成立", "减仓条件解除或价格结构修复",
                         reduction_type="PROFIT_TAKING", quantity=qty, amount=amount, fraction=fraction)
    if advice.get("action") == "HOLD":
        return _decision("HOLD", "未触发更高优先级风险、减仓或加仓条件",
                         confidence, "维持现有仓位", "触发失效、减仓或加仓条件")
    return _decision("NO_ACTION", "当前仅有观察状态，尚无可执行触发",
                     confidence, "等待明确价格与纪律条件", "失效位或风险条件触发")


def build_intraday_final_decision(position, record: dict | None, quote: dict,
                                  price_status: str, ma: dict) -> dict:
    """Re-evaluate the single action using fresh provisional price only."""
    if record is None:
        return _decision("MANUAL_REVIEW", "无当日09:20建议，仅能提供盘中风险快照", "低",
                         "取得当日晨报审计记录", "仍无可验证的晨报基线", "缺少晨报基线")
    subtype = record.get("asset_subtype") or subtype_for(position)
    morning = dict(record.get("final_decision") or {})
    confidence = record.get("confidence", morning.get("confidence", "低"))
    price = _number(quote.get("price"))
    if subtype == BOND_ETF:
        action = "MANUAL_REVIEW" if "异常" in price_status else "HOLD"
        reason = ("债券ETF出现异常波动，复核流动性、折溢价与利率风险" if action == "MANUAL_REVIEW"
                  else "债券ETF盘中无专用风险触发，维持防守仓位")
        return _decision(action, reason, confidence, "核验债券专用风险", "专用风险数据不可用")
    if subtype in _PARTIAL_SUBTYPES:
        action = morning.get("final_action", "NO_ACTION")
        if action not in ("HOLD", "NO_ACTION", "MANUAL_REVIEW"):
            action = "MANUAL_REVIEW"
        return _decision(action, "沿用资产专用路由；不使用A股量能和股票式做T结论",
                         confidence, "核验资产专用风险", "专用市场状态不可核验")
    if not price:
        return _decision("MANUAL_REVIEW", "盘中价格不可验证", "低", "取得新鲜provisional报价",
                         "报价仍不可验证", "实时数据不足")
    if price_status == "已失效":
        qty, amount, fraction = _sell_size(position, price, 1.0)
        return _decision("RISK_EXIT", "盘中价格已跌破早间失效位，风险控制优先",
                         confidence, "失效位跌破得到新鲜报价确认", "重新站回失效位并人工复核",
                         reduction_type="RISK_CONTROL", quantity=qty, amount=amount, fraction=fraction)
    trend_broken = "MA20有效跌破" in ma.get("ma_state", "")
    if price_status in ("已进入买入区", "接近买入区") and trend_broken:
        return _decision("NO_ACTION", "价格进入买入区但MA20趋势破坏，盘中不加仓",
                         "低", "重新站回MA20并恢复结构", "跌破早间失效位",
                         "买入区与趋势破坏冲突")
    if trend_broken:
        fraction = FINAL_DECISION_PARAMS["risk_reduce_fraction"]
        qty, amount, fraction = _sell_size(position, price, fraction)
        return _decision("REDUCE", "盘中MA20趋势破坏，风险减仓不受做T经济性限制",
                         confidence, "趋势破坏保持", "重新站回MA20并确认结构修复",
                         reduction_type="RISK_CONTROL", quantity=qty, amount=amount, fraction=fraction)
    triggered_reduce = price_status in ("目标已达", "已进入减仓区", "已超过减仓区")
    if triggered_reduce and ma.get("ma_state") == "persistent_overheat":
        fraction = FINAL_DECISION_PARAMS["profit_taking_fraction"]
        qty, amount, fraction = _sell_size(position, price, fraction)
        return _decision("REDUCE", "目标/减仓区触发且持续过热，分批锁利",
                         confidence, "价格触发与持续过热同时成立", "价格退出减仓区或过热解除",
                         reduction_type="PROFIT_TAKING", quantity=qty, amount=amount, fraction=fraction)
    if ma.get("ma_state") == "single_day_momentum_burst" and "减仓复核" not in ma.get("ma_action_hint", ""):
        return _decision("HOLD", "单日强势脉冲尚未形成持续过热，不机械减仓且不追高",
                         confidence, "等待完整交易日确认", "出现可靠回落/背离或风险失效")
    if triggered_reduce:
        buy = record.get("buy_zone")
        buyback = _number(buy[1]) if buy and len(buy) == 2 else None
        econ = estimate_t_economics(position, price, buyback)
        if econ["status"] != "SUFFICIENT":
            return _decision("HOLD", "技术上出现战术减仓窗口，但做T经济性不足或不可评估",
                             confidence, "价差与预计净收益同时达到配置门槛",
                             "价格/成本估算失效", "战术减仓被经济性闸门阻断",
                             reduction_type="TACTICAL_T", economics=econ)
        fraction = FINAL_DECISION_PARAMS["tactical_t_fraction"]
        return _decision("REDUCE", "技术触发且做T价差、净收益均达到经济性门槛",
                         confidence, "减仓区触发且经济性门槛满足", "回补价差或净收益低于门槛",
                         reduction_type="TACTICAL_T", quantity=econ["suggested_quantity"],
                         amount=econ["sell_amount"], fraction=econ["suggested_fraction"], economics=econ)
    action = morning.get("final_action", "NO_ACTION")
    if action not in FINAL_ACTIONS:
        action = "MANUAL_REVIEW"
    if action in ("REDUCE", "RISK_EXIT"):
        # Morning reduction remains valid only through its saved deterministic facts.
        return {**morning, "manual_confirmation_required": True}
    return _decision(action, morning.get("action_reason", "盘中未触发新条件，沿用晨报最终结论"),
                     confidence, morning.get("trigger_condition", "等待明确触发"),
                     morning.get("cancel_condition", "信号失效"), morning.get("conflict_note", "无"))


def economics_text(economics: dict | None) -> str:
    if not economics:
        return ""
    if economics.get("status") == "NOT_EVALUABLE":
        return f"不可评估（{economics.get('reason')}）"
    net = economics.get("estimated_net_profit")
    gap = economics.get("expected_gap_pct")
    quantity = economics.get("suggested_quantity", 0)
    amount = economics.get("sell_amount", 0)
    return (f"{economics['label']}（理论{quantity:,}股≈￥{amount:,.0f}；"
            f"预计价差{gap:.2%}，预计净收益约￥{net:,.0f}）")
