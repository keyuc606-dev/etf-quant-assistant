"""Auditable MA5/MA10/MA20 discipline annotations.

This layer never changes price zones, advice outcomes, or executable trade plans.
It only supplies deterministic review hints which must remain subordinate to the
existing account advice and discipline-v1 controls.
"""

from __future__ import annotations

import math

import pandas as pd

from ..asset_routing import (BOND_ETF, COMMODITY_ETF, EQUITY_ETF, GOLD_ETF,
                             QDII_ETF, STOCK, subtype_for)


VERSION = "ma-discipline-v2"
FULL_SUBTYPES = {STOCK, EQUITY_ETF}
PARTIAL_SUBTYPES = {QDII_ETF, GOLD_ETF, COMMODITY_ETF}
NEAR_ATR = 0.35
OVERHEAT_ATR = 1.25
VOLUME_EXPANSION = 1.20
BURST_ATR = 1.50


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _inside(price, zone):
    return bool(zone and len(zone) == 2 and zone[0] <= price <= zone[1])


def _risk_reward_ok(price, advice):
    reduce = advice.get("reduce_range") or advice.get("reduce_zone")
    stop = _number(advice.get("stop") if "stop" in advice else advice.get("invalidation_price"))
    return bool(reduce and stop and stop < price < reduce[0]
                and reduce[0] - price >= 1.5 * (price - stop))


def _result(subtype, applicability="full"):
    return {
        "version": VERSION,
        "asset_subtype": subtype,
        "applicability": applicability,
        "ma_state": "均线数据不足",
        "ma_action_hint": "等待可靠完整日K，人工复核",
        "ma_forbidden_action": "禁止仅凭单一均线信号操作",
        "ma_confirmation": "未确认",
        "ma_conflict_flag": False,
        "ma_reason": "MA5/MA10/MA20 或 ATR 数据不足",
        "signal_codes": [],
    }


def _recent_stall(frame, atr):
    """Return deterministic price/volume divergence evidence from completed bars."""
    if frame is None or len(frame) < 6 or atr is None or atr <= 0:
        return False
    tail = frame.tail(6)
    close = pd.to_numeric(tail.get("收盘"), errors="coerce")
    volume = pd.to_numeric(tail.get("成交量"), errors="coerce")
    if close.isna().any() or len(close) < 6:
        return False
    advance = close.iloc[-2] - close.iloc[0]
    stalled = close.iloc[-1] <= close.iloc[-2] + NEAR_ATR * atr
    if volume is None or volume.isna().any():
        return bool(advance >= 1.5 * atr and stalled and close.iloc[-1] <= close.iloc[-2])
    price_high = close.iloc[-1] >= close.iloc[:-1].max()
    volume_divergence = price_high and volume.iloc[-1] < .75 * volume.iloc[-3:-1].mean()
    return bool(advance >= 1.5 * atr and (stalled or volume_divergence))


def _momentum_evidence(frame, price, ma5, atr, near_pressure, stall):
    """Separate a one-bar expansion from multi-session, multi-factor heat."""
    tail = frame.tail(6)
    closes = pd.to_numeric(tail.get("收盘"), errors="coerce")
    ma5s = pd.to_numeric(tail.get("MA5"), errors="coerce")
    atrs = pd.to_numeric(tail.get("ATR"), errors="coerce")
    if closes is None or len(closes) < 2 or closes.isna().any():
        return False, False, []
    previous = closes.iloc[-2]
    daily_rise_atr = (price - previous) / atr
    single_day_burst = daily_rise_atr >= BURST_ATR

    recent = closes.tail(5)
    changes = recent.diff().dropna()
    multi_day_rally = bool(
        len(changes) >= 3 and (changes > 0).sum() >= 3
        and (recent.iloc[-1] - recent.iloc[0]) / atr >= 1.5
    )
    previous_bias = None
    if ma5s is not None and atrs is not None and len(ma5s) >= 2 and len(atrs) >= 2:
        prev_ma5, prev_atr = ma5s.iloc[-2], atrs.iloc[-2]
        if pd.notna(prev_ma5) and pd.notna(prev_atr) and prev_atr > 0:
            previous_bias = (previous - prev_ma5) / prev_atr
    current_bias = (price - ma5) / atr
    sustained_bias = bool(previous_bias is not None and previous_bias >= .80
                          and current_bias >= OVERHEAT_ATR
                          and current_bias >= previous_bias - .15)
    evidence = []
    if multi_day_rally:
        evidence.append("MULTI_DAY_RALLY")
    if sustained_bias:
        evidence.append("SUSTAINED_MA5_BIAS")
    if near_pressure:
        evidence.append("NEAR_TARGET_PRESSURE")
    if stall:
        evidence.append("STALL_DIVERGENCE")
    persistent = current_bias >= OVERHEAT_ATR and len(evidence) >= 2
    # A fresh large bar is not persistent merely because it also reaches a target.
    if single_day_burst and not multi_day_rally and not sustained_bias and not stall:
        persistent = False
    return single_day_burst, persistent, evidence


def build_ma_discipline(view, frame, advice, discipline, cash_note=""):
    """Build the close-only MA discipline used by the 09:20 report."""
    subtype = view.get("asset_subtype") or subtype_for(view["position"])
    if subtype == BOND_ETF:
        result = _result(subtype, "none")
        result.update(
            ma_state="不适用",
            ma_action_hint="仅将价格趋势用于异常监测",
            ma_forbidden_action="禁止套用股票式MA5/MA10/MA20交易纪律",
            ma_confirmation="债券ETF默认关闭",
            ma_reason="防守资产优先复核仓位、流动性、折溢价和利率/久期风险",
        )
        return result
    applicability = "partial" if subtype in PARTIAL_SUBTYPES else "full"
    result = _result(subtype, applicability)
    if not view.get("available") or frame is None or len(frame) < 20:
        return result
    last = frame.iloc[-1]
    previous = frame.iloc[-2] if len(frame) >= 2 else None
    price, ma5, ma10, ma20, atr = (_number(last.get(key)) for key in
                                    ("收盘", "MA5", "MA10", "MA20", "ATR"))
    if not all(value is not None and value > 0 for value in (price, ma5, ma10, ma20, atr)):
        return result
    if applicability == "partial":
        state = "MA20上方趋势偏强" if price >= ma20 else "MA20下方趋势偏弱"
        result.update(
            ma_state=state,
            ma_action_hint="仅结合ATR、组合暴露与资产类别专属风险复核趋势",
            ma_forbidden_action="禁止套用A股量能、涨停/封板或单一均线买卖逻辑",
            ma_confirmation="仅趋势信息；不使用A股成交量确认",
            ma_reason="QDII/黄金/商品仅部分启用MA趋势，需结合时差、汇率、折溢价或宏观驱动",
            signal_codes=["PARTIAL_TREND"],
        )
        return result

    prev_price = _number(previous.get("收盘")) if previous is not None else None
    prev_ma5 = _number(previous.get("MA5")) if previous is not None else None
    prev_ma10 = _number(previous.get("MA10")) if previous is not None else None
    prev_ma20 = _number(previous.get("MA20")) if previous is not None else None
    volume_ratio = _number(last.get("量比"))
    distance_ma5_atr = (price - ma5) / atr
    below_ma20_effective = price < ma20 and bool(
        (prev_price is not None and prev_ma20 is not None and prev_price < prev_ma20)
        or ma20 - price >= NEAR_ATR * atr)
    cross_up = bool(prev_ma5 is not None and prev_ma10 is not None
                    and prev_ma5 <= prev_ma10 and ma5 > ma10)
    volume_up = volume_ratio is not None and volume_ratio >= VOLUME_EXPANSION
    stand_ma10 = bool(price >= ma10 and prev_price is not None and prev_ma10 is not None
                      and prev_price >= prev_ma10 and price > prev_price and ma10 >= prev_ma10)
    stall = _recent_stall(frame, atr)

    buy = advice.get("buy_range") or advice.get("buy_zone")
    reduce = advice.get("reduce_range") or advice.get("reduce_zone")
    stop = _number(advice.get("stop") if "stop" in advice else advice.get("invalidation_price"))
    near_pressure = bool(reduce and price >= reduce[0] - NEAR_ATR * atr)
    single_day_burst, persistent_overheat, heat_evidence = _momentum_evidence(
        frame, price, ma5, atr, near_pressure, stall)
    in_buy = _inside(price, buy)
    forbidden = (discipline or {}).get("forbidden_action", "")
    discipline_blocks_add = any(token in forbidden for token in
                                ("禁止加仓", "禁止追高", "禁止无视压力追高", "越跌越补",
                                 "继续摊平", "禁止自动回补", "禁止情绪化追回"))
    capacity_ok = view.get("weight", 0) < .25 and "现金偏低" not in cash_note
    candidate_ok = in_buy and _risk_reward_ok(price, advice) and not near_pressure \
        and not discipline_blocks_add and capacity_ok

    signals = []
    if below_ma20_effective:
        signals.append("MA20_EFFECTIVE_BREAK")
    if stall:
        signals.append("STALL_DIVERGENCE")
    if distance_ma5_atr >= OVERHEAT_ATR:
        signals.append("MA5_OVERHEAT")
    if single_day_burst:
        signals.append("SINGLE_DAY_MOMENTUM_BURST")
    if persistent_overheat:
        signals.append("PERSISTENT_OVERHEAT")
    signals.extend(code for code in heat_evidence if code not in signals)
    if price < ma10:
        signals.append("MA10_BREAK")
    if price < ma5 and price >= ma10:
        signals.append("MA5_BREAK_MA10_HOLD")
    if cross_up and volume_up:
        signals.append("MA5_CROSS_MA10_VOLUME")
    if stand_ma10 and volume_up:
        signals.append("MA10_HOLD_PRICE_VOLUME")

    if below_ma20_effective:
        multi = bool(stop and price <= stop) or bool(buy and price < buy[0] - NEAR_ATR * atr)
        result.update(
            ma_state="MA20有效跌破/趋势破坏",
            ma_action_hint="清仓复核" if multi else "趋势失效，优先减仓复核",
            ma_forbidden_action="禁止加仓；禁止把单日跌破自动等同清仓",
            ma_confirmation=("多重确认：MA20有效跌破且失效位/关键支撑同步失守" if multi else
                             "连续跌破或偏离达到0.35 ATR；尚缺失效位/关键支撑多重确认"),
            ma_reason="MA20趋势底线失守；清仓结论仍受ATR、关键支撑和既有失效位约束",
        )
    elif persistent_overheat:
        result.update(
            ma_state="persistent_overheat",
            ma_action_hint="分批锁利复核",
            ma_forbidden_action="禁止继续追高；禁止一次性机械清仓",
            ma_confirmation=f"持续性过热获{len(heat_evidence)}项确认：{'、'.join(heat_evidence)}",
            ma_reason="连续涨幅、持续MA5乖离、目标/压力与滞涨背离至少两项共同确认",
        )
    elif single_day_burst:
        result.update(
            ma_state="single_day_momentum_burst",
            ma_action_hint="单日强势脉冲，等待次日确认",
            ma_forbidden_action="禁止追高；禁止因单日MA5乖离机械减仓",
            ma_confirmation=f"单日上涨达到 {(price-prev_price)/atr:.2f} ATR；日线无可靠封板状态",
            ma_reason="此前未形成连续显著拉升，单日扩张与持续性过热分开处理",
        )
    elif stall:
        result.update(
            ma_state="连续拉升后滞涨/量价背离",
            ma_action_hint="与冲高滞涨纪律合并，逐步减仓/锁利复核",
            ma_forbidden_action="禁止高位追涨或重复叠加减仓文案",
            ma_confirmation="此前涨幅达到1.5 ATR，最新价格滞涨或量价背离",
            ma_reason="复用discipline-v1的冲高滞涨出口，不生成第二套独立卖出指令",
        )
    elif distance_ma5_atr >= OVERHEAT_ATR:
        result.update(
            ma_state="MA5显著乖离/持续性待确认",
            ma_action_hint="乖离显著但持续性证据不足，等待确认",
            ma_forbidden_action="禁止追高；禁止仅凭MA5乖离机械减仓",
            ma_confirmation=f"收盘高于MA5 {distance_ma5_atr:.2f} ATR，但持续性证据不足两项",
            ma_reason="ATR乖离仅是候选证据，不再单独触发持续性过热",
        )
    elif price < ma10:
        result.update(
            ma_state="MA10跌破",
            ma_action_hint="风险收缩/减仓观察",
            ma_forbidden_action="禁止机械卖出或弱势补仓",
            ma_confirmation=f"收盘低于MA10 {(ma10-price)/atr:.2f} ATR",
            ma_reason="MA10波段防守失守，但未单独构成清仓条件",
        )
    elif cross_up and volume_up:
        conflict = near_pressure or discipline_blocks_add
        result.update(
            ma_state="MA5上穿MA10且放量",
            ma_action_hint=("信号冲突，人工复核" if conflict else
                            "加仓候选，等待买入区确认" if not candidate_ok else
                            "重新评估/加仓候选"),
            ma_forbidden_action="禁止金叉即加仓；禁止覆盖买入区和风险纪律",
            ma_confirmation=f"MA5向上交叉MA10，完整日量比{volume_ratio:.2f}",
            ma_conflict_flag=conflict,
            ma_reason=("接近压力或discipline-v1禁止追高" if conflict else
                       "转强信号仅建立候选资格，仍需买入区、风险收益与账户余量确认"),
        )
    elif stand_ma10 and volume_up:
        conflict = near_pressure or discipline_blocks_add
        result.update(
            ma_state="站稳MA10且量价齐升",
            ma_action_hint=("信号冲突，人工复核" if conflict else
                            "加仓候选，等待买入区确认" if not candidate_ok else
                            "重新评估/加仓候选"),
            ma_forbidden_action="禁止量价齐升即追涨；禁止覆盖原风险纪律",
            ma_confirmation=f"连续守住MA10且价格上升，完整日量比{volume_ratio:.2f}",
            ma_conflict_flag=conflict,
            ma_reason=("接近压力或discipline-v1禁止追高" if conflict else
                       "量价确认只作为候选条件，原买入区与风险收益继续有效"),
        )
    elif price < ma5 and price >= ma10:
        result.update(
            ma_state="MA5跌破但MA10仍守住",
            ma_action_hint="重新评估/加仓候选" if candidate_ok else "保持观望，等待买入区与风险确认",
            ma_forbidden_action="禁止把守住MA10直接当作加仓指令",
            ma_confirmation=("买入区、风险收益、纪律与账户余量均确认" if candidate_ok else
                             "仅确认MA10仍守住；买入区/风险收益/纪律/账户余量未全部确认"),
            ma_reason="短线转弱但波段结构未破；接回必须经过既有买入区和账户纪律闸门",
        )
    else:
        result.update(
            ma_state="均线结构未触发",
            ma_action_hint="按原建议与discipline-v1观察",
            ma_forbidden_action="禁止仅凭单一均线位置操作",
            ma_confirmation="无MA5/MA10/MA20高优先级触发",
            ma_reason="未达到ATR归一化过热、均线破位或放量转强条件",
        )
    result["signal_codes"] = signals
    return result


def intraday_ma_discipline(record, quote, price_status=""):
    """Re-evaluate risk against morning MAs without mutating the morning zones."""
    saved = dict(record.get("ma_discipline") or {})
    had_saved_discipline = bool(saved)
    subtype = record.get("asset_subtype") or saved.get("asset_subtype")
    if subtype == BOND_ETF:
        return saved or _result(subtype, "none")
    if subtype in PARTIAL_SUBTYPES:
        return saved or _result(subtype, "partial")
    features = record.get("technical_features") or {}
    price = _number(quote.get("price"))
    ma5, ma10, ma20, atr = (_number(features.get(key)) for key in ("MA5", "MA10", "MA20", "ATR"))
    if not all(value is not None and value > 0 for value in (price, ma5, ma10, ma20, atr)):
        return saved or _result(subtype)
    result = saved or _result(subtype)
    previous_close = _number(quote.get("previous_close"))
    rise_atr = ((price - previous_close) / atr if previous_close else None)
    burst = rise_atr is not None and rise_atr >= BURST_ATR
    limit_status = quote.get("limit_status")
    high = _number(quote.get("high"))
    reversed_from_high = bool(high and high - price >= .50 * atr)
    reversal = bool(quote.get("intraday_reversal") or quote.get("volume_divergence")
                    or limit_status == "broken" or reversed_from_high)
    if price < ma20 and ma20 - price >= NEAR_ATR * atr:
        result.update(ma_state="MA20有效跌破/趋势破坏", ma_action_hint="趋势失效，优先减仓复核",
                      ma_forbidden_action="禁止加仓；清仓需失效位与关键支撑多重确认",
                      ma_confirmation="盘中价低于早间MA20至少0.35 ATR",
                      ma_reason="provisional价格确认趋势风险；不改写早间区间")
    elif price < ma10:
        result.update(ma_state="MA10跌破", ma_action_hint="风险收缩/减仓观察",
                      ma_forbidden_action="禁止机械卖出或弱势补仓",
                      ma_confirmation="盘中价失守早间MA10",
                      ma_reason="provisional价格确认波段防守转弱；不改写早间区间")
    elif result.get("ma_state") == "persistent_overheat" and price - ma5 >= OVERHEAT_ATR * atr:
        result.update(ma_action_hint="分批锁利复核",
                      ma_confirmation="早间多因素持续性过热仍成立，盘中乖离未解除")
    elif burst or price - ma5 >= OVERHEAT_ATR * atr:
        if limit_status == "sealed":
            action = "强势脉冲/观察，不追高，不因单日乖离机械减仓"
            confirmation = "可靠封板状态已核验"
        elif limit_status in ("not_sealed", "broken") and reversal:
            action = "冲高回落/炸板，减仓复核"
            confirmation = "未封板且出现冲高回落、炸板或量价背离"
        else:
            action = "单日强势脉冲，等待次日确认"
            confirmation = "封板数据缺失或尚无可靠冲高回落证据"
        result.update(ma_state="single_day_momentum_burst", ma_action_hint=action,
                      ma_forbidden_action="禁止追高；禁止因单日MA5乖离机械减仓",
                      ma_confirmation=confirmation,
                      ma_reason="盘中单日扩张不等同于多日持续性过热")
    elif price < ma5 and price >= ma10:
        result.update(ma_state="MA5跌破但MA10仍守住",
                      ma_action_hint="保持观望，等待买入区与风险确认",
                      ma_forbidden_action="禁止把守住MA10直接当作加仓指令",
                      ma_confirmation="盘中价低于早间MA5但仍守住早间MA10",
                      ma_reason="provisional价格仅恢复短线/波段结构；不改写早间区间")
    elif not had_saved_discipline:
        result.update(ma_state="均线结构未触发",
                      ma_action_hint="按早间区间与discipline-v1观察",
                      ma_forbidden_action="禁止仅凭单一均线位置操作",
                      ma_confirmation="旧版晨报记录已用MA5/MA10/MA20与ATR安全降级复核",
                      ma_reason="不补猜历史交叉或量价信号；下一次09:20将保存完整MA状态")

    # Only evaluate the special no-volume rally when both volume freshness and
    # an explicit exchange limit/seal state are present. Current providers do
    # not guarantee the latter, so normal production quotes safely omit it.
    progress = _number(quote.get("session_progress"))
    volume = _number(quote.get("volume"))
    baseline = _number(record.get("reference_volume"))
    if (limit_status in ("sealed", "not_sealed") and progress and progress >= .15
            and volume and baseline and previous_close and price > previous_close):
        ratio = volume / (baseline if progress >= .9375 else baseline * progress)
        rise_atr = (price - previous_close) / atr
        if ratio <= .60 and rise_atr >= .75:
            if limit_status == "sealed":
                result.update(ma_state="single_day_momentum_burst",
                              ma_action_hint="强势脉冲/观察，不追高，不因单日乖离机械减仓",
                              ma_forbidden_action="禁止追涨；禁止从封板状态推导自动持有结论",
                              ma_confirmation=f"封板可靠；量能为进度基准{ratio:.2f}倍",
                              ma_reason="封板脉冲优先观察，不由单日乖离触发卖出")
            elif reversal:
                result.update(ma_state="single_day_momentum_burst",
                              ma_action_hint="冲高回落/炸板，减仓复核",
                              ma_forbidden_action="禁止追涨",
                              ma_confirmation=f"未封板且回落；量能为进度基准{ratio:.2f}倍",
                              ma_reason="单日脉冲只有在未封板并出现回落/背离时进入减仓复核")
    return result


def compact_ma_note(result):
    state = result.get("ma_state", "均线数据不足")
    action = result.get("ma_action_hint", "人工复核")
    if state == "不适用":
        return "不适用｜债券ETF仅做异常趋势监测"
    if result.get("applicability") == "partial":
        return f"{state}｜部分启用，不使用A股量能逻辑"
    if state == "single_day_momentum_burst":
        return f"单日强势脉冲｜{action}"
    if state == "persistent_overheat":
        return f"持续性过热｜{action}"
    return f"{state}｜{action}"


def intraday_priority_adjustment(verdict, ma_result):
    """Deterministically promote combinations requested by the live checklist."""
    status, state = verdict.get("status", ""), ma_result.get("ma_state", "")
    if status in ("已失效", "接近失效位") and "MA20" in state:
        return 0
    if status == "跌破买入区但未失效" and state in ("MA10跌破", "MA20有效跌破/趋势破坏"):
        return 1
    if status in ("目标已达", "已进入减仓区", "已超过减仓区") and state == "persistent_overheat":
        return 2
    if (status in ("目标已达", "已进入减仓区", "已超过减仓区")
            and state == "single_day_momentum_burst"
            and "减仓复核" in ma_result.get("ma_action_hint", "")):
        return 2
    if status in ("已进入买入区", "接近买入区") and ma_result.get("ma_conflict_flag"):
        return 3
    return verdict.get("priority", 90)
