"""Offline, auditable valuation and earnings proxies; never AI scores."""
import datetime as dt
import math

from ..asset_routing import STOCK, EQUITY_ETF, subtype_for

VERSION = "fundamental-valuation-v1"
FIELDS = ("fundamental_version", "fundamental_state", "valuation_state",
          "fundamental_score", "valuation_score", "valuation_percentile",
          "fundamental_mapping_degraded", "fundamental_index_code",
          "fundamental_source", "fundamental_as_of", "fundamental_reason",
          "earnings_proxy_change", "low_buy_state", "tech_vs_fundamental_conflict")
# Exact tracked benchmark, not a substitute ETF's price or an inferred sector.
INDEX_ROUTES = {
    "515880": "931160", "512480": "H30184", "159995": "980017",
    "159782": "931643", "159781": "931643", "159755": "980032",
    "159796": "931719", "159851": "930986", "512400": "000819",
    "512890": "H30269", "510300": "000300", "510500": "000905",
    "588000": "000688", "510050": "000016", "515180": "000922",
}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def evaluate(position, observations=None, now=None):
    """Require dated, positive observations; no price-drop-as-value shortcut."""
    now = now or dt.date.today()
    if isinstance(now, dt.datetime):
        now = now.date()
    subtype = subtype_for(position)
    index = INDEX_ROUTES.get(position.code) if subtype == EQUITY_ETF else None
    result = dict.fromkeys(FIELDS)
    result.update(fundamental_version=VERSION, fundamental_state="UNKNOWN",
                  valuation_state="UNKNOWN", fundamental_mapping_degraded=(
                      subtype == EQUITY_ETF and index is None),
                  fundamental_index_code=index, low_buy_state="UNKNOWN",
                  tech_vs_fundamental_conflict=False,
                  fundamental_reason="可靠基本面/估值数据不足")
    if subtype not in (STOCK, EQUITY_ETF):
        result["fundamental_reason"] = "资产专用路由，不适用A股基本面模型"
        return result
    data = observations if isinstance(observations, dict) else {}
    if subtype == EQUITY_ETF and (not index or data.get("index_code") != index):
        result["fundamental_mapping_degraded"] = True
        return result
    try:
        stamp = dt.date.fromisoformat(str(data["as_of"])[:10])
        if not 0 <= (now - stamp).days <= (10 if subtype == EQUITY_ETF else 150):
            return result
    except (KeyError, ValueError, TypeError):
        return result
    result.update(fundamental_source=data.get("source"), fundamental_as_of=stamp.isoformat())
    history = data.get("history") or []
    rows = []
    for row in history:
        try:
            date = dt.date.fromisoformat(str(row["date"])[:10])
            pe, close = number(row.get("pe")), number(row.get("close"))
            if date <= stamp and pe and pe > 0 and close and close > 0:
                rows.append((date, pe, close, row.get("constituents")))
        except (ValueError, KeyError, TypeError):
            continue
    rows = sorted({row[0]: row for row in rows}.values())[-756:]
    if len(rows) >= 252 and rows[-1][0] == stamp and (stamp - rows[0][0]).days >= 330:
        percentile = 100 * (sum(row[1] < rows[-1][1] for row in rows) +
                            .5 * sum(row[1] == rows[-1][1] for row in rows)) / len(rows)
        result.update(valuation_percentile=round(percentile, 2),
                      valuation_score=round(100 - percentile, 2),
                      valuation_state=("CHEAP" if percentile <= 20 else "FAIR" if percentile < 80
                                       else "EXPENSIVE" if percentile < 95 else "EXTREME"))
    if len(rows) >= 84 and rows[-1][0] == stamp and (stamp - rows[-84][0]).days <= 150:
        recent, prior = rows[-21:], rows[-84:-63]
        counts = {row[3] for row in rows[-84:]}
        # The earnings proxy can shift with index reconstitution. Even equal
        # counts cannot establish unchanged constituents; disclose this limit.
        if None not in counts and len(counts) == 1:
            change = (sum(r[2] / r[1] for r in recent) /
                      sum(r[2] / r[1] for r in prior) - 1)
            result.update(earnings_proxy_change=change,
                          fundamental_state=("DETERIORATING" if change < -.05 else
                                             "IMPROVING" if change > .05 else "STABLE"),
                          fundamental_reason="指数隐含盈利代理（价格/滚动PE，21日均值对比63日前）；非盈利预测，成分调整可干扰")
    if subtype == STOCK:
        growth, roe = number(data.get("profit_yoy")), number(data.get("roe"))
        if growth is not None and roe is not None:
            result.update(fundamental_state=("DETERIORATING" if growth < -5 or roe <= 0 else
                                            "IMPROVING" if growth > 5 else "STABLE"),
                          fundamental_reason="已披露财报净利润同比及ROE；估值历史缺失仍UNKNOWN")
    return result


def annotate(decision, facts, evidence):
    weak = bool(evidence.get("trend_break_candidate") or evidence.get("sell_confirmations"))
    favorable = facts["valuation_state"] in ("CHEAP", "FAIR") and facts["fundamental_state"] in ("STABLE", "IMPROVING")
    state = ("BLOCKED" if facts["fundamental_state"] == "DETERIORATING" or
             facts["valuation_state"] in ("EXPENSIVE", "EXTREME") else
             "CONFIRMED" if favorable and decision["final_action"] == "ADD" else
             "LOW_BUY_WATCH" if favorable else "UNKNOWN")
    labels = {"UNKNOWN": "未知", "STABLE": "稳定", "IMPROVING": "改善",
              "DETERIORATING": "恶化", "CHEAP": "偏低", "FAIR": "合理",
              "EXPENSIVE": "偏高", "EXTREME": "极贵"}
    reason = (f"基本面代理{labels[facts['fundamental_state']]}+估值{labels[facts['valuation_state']]}；"
              + decision["action_reason"])
    return {**decision, **facts, "action_reason": reason, "low_buy_state": state,
            "tech_vs_fundamental_conflict": bool(weak and favorable)}
