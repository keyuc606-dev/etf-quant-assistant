"""Deterministic account discipline annotations; never changes a trade plan."""

import datetime as dt
import math


VERSION = "discipline-v1"


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _recent_sale(code, executions, as_of):
    """A sale is evidence only when its timestamp and subsequent trades are known."""
    relevant = []
    for row in executions or []:
        if row.get("code") != code or row.get("side") not in ("BUY", "SELL"):
            continue
        try:
            when = dt.datetime.fromisoformat(row["executed_at"].replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            when = when.astimezone(dt.timezone.utc)
            age = (as_of.date() - when.astimezone(
                as_of.tzinfo or dt.timezone(dt.timedelta(hours=8))).date()).days
            if 0 <= age <= 15:
                relevant.append((when, row))
        except (KeyError, TypeError, ValueError):
            continue
    relevant.sort(key=lambda pair: pair[0])
    if not relevant or relevant[-1][1]["side"] != "SELL":
        return None
    return _number(relevant[-1][1].get("price"))


def sale_chase_alert(code, executions, as_of, price, atr):
    sale = _recent_sale(code, executions, as_of)
    price, atr = _number(price), _number(atr)
    return bool(sale and price and atr and atr > 0
                and price >= sale + max(0.75 * atr, 0.0075 * sale))


def cash_defense(pm, views):
    """Account-level warning, not an allocation or return optimization target."""
    total = _number(pm.total_assets)
    if not total or total <= 0:
        return "账户现金状态未知，无法评估防守余量"
    cash = max(0.0, _number(pm.cash) or 0.0) / total
    concentration = max((v["weight"] for v in views), default=0.0)
    exposed = 1 - cash
    if cash < 0.10 or (cash < 0.20 and (concentration > 0.25 or exposed > 0.85)):
        state = "现金偏低"
    elif cash >= 0.35 and concentration <= 0.25:
        state = "现金充足"
    else:
        state = "现金正常"
    return f"账户纪律：{state}（现金 {cash:.1%}，最大单项 {concentration:.1%}，总持仓 {exposed:.1%}）；人工复核流动性"


def t_opportunity(advice, atr, quote=None):
    """Only fresh intraday high/low can establish a provisional T opportunity."""
    if not quote or not quote.get("provisional"):
        return "不建议做T：缺少可信日内高低点，无法判断空间"
    price, high, low, opened, previous = (_number(quote.get(k)) for k in
                                          ("price", "high", "low", "open", "previous_close"))
    buy, reduce = advice.get("buy_range"), advice.get("reduce_range")
    if (not all((price, high, low, opened, previous, atr, buy, reduce))
            or low <= 0 or high < max(price, opened) or low > min(price, opened)
            or buy[1] >= reduce[0]):
        return "不建议做T：日内区间或支撑压力不完整"
    # A round trip estimate includes commissions, sale tax where applicable and slip.
    round_trip = 0.0035 if advice.get("asset_type") == "STOCK" else 0.0025
    available = min(high - low, reduce[0] - buy[1])
    if available <= max(atr, 3 * round_trip * price):
        return "无T空间，今日不做T：可用价差未明显覆盖ATR与估算交易成本"
    return "具备T机会（仅人工参考）：日内波幅与支撑压力间距均覆盖ATR和估算成本；核对实时深度与费用"


def build_discipline(view, frame, advice, cash_note, executions=None,
                     as_of=None, quote=None):
    pos = view["position"]
    as_of = as_of or dt.datetime.now(dt.timezone.utc)
    base = {"version": VERSION, "discipline_state": "数据不足",
            "discipline_action": "等待可靠数据，人工复核", "forbidden_action": "禁止仅凭成本价加仓",
            "reentry_condition": "待趋势、支撑、量能和风险收益重新确认",
            "t_opportunity": "不建议做T：缺少可信日内高低点，无法判断空间",
            "cash_defense_note": cash_note, "reason": "行情或指标不足", "sale_status": "unknown"}
    if not view.get("available") or frame is None or len(frame) < 20:
        return base
    last = frame.iloc[-1]
    price = _number(quote.get("price")) if quote else _number(last.get("收盘"))
    atr = _number(last.get("ATR"))
    ma5, ma10, ma20, ma60 = (_number(last.get(k)) for k in ("MA5", "MA10", "MA20", "MA60"))
    rsi, macd, k, d = (_number(last.get(key)) for key in ("RSI14", "MACD", "K", "D"))
    boll_up, volume_ratio = (_number(last.get(key)) for key in ("BOLL_UP", "量比"))
    if not price or price <= 0 or not atr or atr <= 0 or not ma20:
        return base
    weak = price < ma20 and (ma5 is None or ma5 < ma20) and (macd is None or macd <= 0)
    broken = advice.get("stop") is not None and price < advice["stop"]
    strong = (price >= ma20 and ma5 is not None and ma5 >= ma20
              and (macd is None or macd >= 0) and (k is None or d is None or k >= d)
              and (volume_ratio is None or volume_ratio >= 0.8))
    buy, reduce = advice.get("buy_range"), advice.get("reduce_range")
    near_support = bool(buy and buy[0] <= price <= buy[1])
    risk_reward = bool(reduce and advice.get("stop") is not None
                       and reduce[0] > price and price > advice["stop"]
                       and (reduce[0] - price) >= 1.5 * (price - advice["stop"]))
    reentry_ok = strong and near_support and risk_reward and not broken
    base["reentry_condition"] = ("已进入规则买入区且趋势、量能和风险收益确认；仅重新人工评估"
                                 if reentry_ok else "仅回踩规则买入区、重新企稳且量能与风险收益确认后评估")
    base["t_opportunity"] = t_opportunity({**advice, "asset_type": pos.asset_type}, atr, quote)
    pnl = view.get("pnl_pct")
    sale = _recent_sale(pos.code, executions, as_of)
    if sale:
        base["sale_status"] = "recent_sell"
    if sale_chase_alert(pos.code, executions, as_of, price, atr) and not reentry_ok:
        state, action, forbidden, reason = ("卖飞/减仓后续涨", "保留已卖资金；剩余仓位按原计划管理",
            "禁止情绪化追回已卖仓位", "近期账本有卖出且现价显著高于成交价，尚无有效回踩确认")
    elif broken and weak:
        state, action, forbidden, reason = ("深套/趋势破坏" if pnl is not None and pnl < 0 else "趋势破坏",
            "优先人工复核风险收缩", "禁止越跌越补、继续摊平", "失效位跌破且MA20与动能偏弱")
    elif (quote and quote.get("provisional") and _number(quote.get("high"))
          and price < _number(quote.get("high")) - atr and price < ma20):
        state, action, forbidden, reason = ("冲高回落", "人工检查原有仓位和支撑压力", "无空间不做T",
                                             "日内高点回落超过ATR并跌回短线支撑下方")
    elif (pnl is not None and pnl > 0 and reduce and price >= reduce[0] - 0.3 * atr
          and (rsi is not None and rsi >= 65 or volume_ratio is not None and volume_ratio >= 1.5
               and ma5 is not None and price <= ma5)):
        state, action, forbidden, reason = ("冲高滞涨", "在原规则减仓区人工复核分批锁利", "禁止无视压力追高",
                                             "已有浮盈，接近规则压力区且出现过热或放量滞涨")
    elif pnl is not None and pnl < 0 and (abs(pnl) <= max(0.12, 3 * atr / price)):
        state = "浅套"
        action = ("支撑企稳后可小额回补，仅人工评估" if reentry_ok else
                  "趋势弱时偏反弹减仓；等待结构确认" if weak else "持有观察，等待结构确认")
        forbidden, reason = "禁止仅因接近成本线补仓", "成本亏损仅作状态标签；补仓需要趋势、支撑、量能和风险收益同时确认"
    elif pnl is not None and pnl < 0 and weak:
        state, action, forbidden, reason = ("深套/下行趋势", "优先人工复核风险收缩", "禁止越跌越补、继续摊平",
                                             "亏损伴随MA20及动能走弱")
    else:
        state, action, forbidden, reason = ("正常观察", "按原规则建议人工复核", "禁止仅凭成本价加仓",
                                             "未触发更高优先级纪律状态")
    if sale and reentry_ok:
        state, action, forbidden, reason = ("回踩后重新评估", "可重新人工评估原规则买入区", "禁止自动回补",
                                             "近期卖出后，规则支撑、趋势、量能和风险收益再次确认")
    base.update(discipline_state=state, discipline_action=action,
                forbidden_action=forbidden, reason=reason)
    return base


def short_note(discipline):
    state = discipline["discipline_state"]
    if state == "卖飞/减仓后续涨":
        return f"卖飞后禁止追回；{discipline['reentry_condition']}。"
    if state.startswith("深套") or state == "趋势破坏":
        return "深套或趋势破坏，禁止继续摊平。"
    if state == "冲高滞涨":
        return "接近压力，人工复核分批锁利。"
    if state == "回踩后重新评估":
        return "回踩企稳后可重新人工评估，不自动回补。"
    if discipline["t_opportunity"].startswith("具备"):
        return "日内具备T空间，仅供人工复核。"
    return "无可信T空间，今日不做T；" + discipline["forbidden_action"] + "。"
