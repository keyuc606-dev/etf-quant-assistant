"""V2 ETF 扫描与新增资金配置。

本模块是纯计算层，不联网、不读写文件。所有分数和资金数字均由确定性规则生成；
历史成本与浮亏不参与候选评分，避免“为了回本”强行交易。
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd

from ..asset_routing import BOND_ETF, EQUITY_ETF, GOLD_ETF, QDII_ETF, STOCK
from ..config import ETF_V2_PARAMS, ETF_V2_UNIVERSE


def _frame(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None or df.empty or "日期" not in df or "收盘" not in df:
        return None
    clean = df.copy()
    clean["日期"] = pd.to_datetime(clean["日期"], errors="coerce")
    for col in ("开盘", "收盘", "最高", "最低", "成交量", "成交额"):
        if col in clean:
            clean[col] = pd.to_numeric(clean[col], errors="coerce")
    clean = clean.dropna(subset=["日期", "收盘"]).sort_values("日期")
    return clean.drop_duplicates("日期", keep="last").reset_index(drop=True)


def market_regime(market_data: Dict[str, pd.DataFrame], benchmark_code: str = "510300") -> dict:
    benchmark = _frame(market_data.get(benchmark_code))
    if benchmark is None or len(benchmark) < 120:
        return {"state": "UNKNOWN", "score": 40.0, "breadth": None,
                "reason": "宽基历史不足，市场环境降级为未知"}
    close = benchmark["收盘"]
    last = float(close.iloc[-1])
    ma60 = float(close.tail(60).mean())
    ma120 = float(close.tail(120).mean())
    breadth_flags = []
    for df in market_data.values():
        clean = _frame(df)
        if clean is not None and len(clean) >= 60:
            breadth_flags.append(float(clean["收盘"].iloc[-1]) > float(clean["收盘"].tail(60).mean()))
    breadth = sum(breadth_flags) / len(breadth_flags) if breadth_flags else None
    if last > ma60 > ma120 and (breadth is None or breadth >= 0.55):
        return {"state": "RISK_ON", "score": 80.0, "breadth": breadth,
                "reason": "沪深300位于60/120日均线上方，市场宽度偏强"}
    if last < ma60 and last < ma120 and (breadth is None or breadth < 0.45):
        return {"state": "RISK_OFF", "score": 25.0, "breadth": breadth,
                "reason": "沪深300位于60/120日均线下方，市场宽度偏弱"}
    return {"state": "NEUTRAL", "score": 55.0, "breadth": breadth,
            "reason": "宽基趋势与市场宽度未形成同向确认"}


def _return(close: pd.Series, periods: int) -> Optional[float]:
    if len(close) <= periods or float(close.iloc[-periods - 1]) <= 0:
        return None
    return float(close.iloc[-1] / close.iloc[-periods - 1] - 1)


def _max_drawdown(close: pd.Series) -> float:
    values = close.astype(float)
    peaks = values.cummax()
    return float(((values / peaks) - 1).min()) if len(values) else 0.0


def _avg_amount(df: pd.DataFrame, window: int) -> Optional[float]:
    if "成交额" in df and df["成交额"].notna().any():
        return float(df["成交额"].tail(window).mean())
    if "成交量" in df and df["成交量"].notna().any():
        return float((df["成交量"] * df["收盘"]).tail(window).mean())
    return None


def _portfolio_returns(holdings_data: Dict[str, pd.DataFrame], weights: Optional[dict] = None) -> Optional[pd.Series]:
    series = []
    for code, df in holdings_data.items():
        clean = _frame(df)
        if clean is None or len(clean) < 20:
            continue
        ret = clean.set_index("日期")["收盘"].pct_change().rename(code)
        series.append(ret)
    if not series:
        return None
    table = pd.concat(series, axis=1).dropna(how="all")
    if table.empty:
        return None
    raw = pd.Series({col: max(0.0, float((weights or {}).get(col, 1.0))) for col in table})
    raw = raw / raw.sum() if raw.sum() > 0 else pd.Series(1 / len(table.columns), index=table.columns)
    return table.mul(raw, axis=1).sum(axis=1, min_count=1)


def _correlation(df: pd.DataFrame, portfolio_returns: Optional[pd.Series], window: int) -> Optional[float]:
    if portfolio_returns is None:
        return None
    candidate = df.set_index("日期")["收盘"].pct_change().rename("candidate")
    joined = pd.concat([candidate, portfolio_returns.rename("portfolio")], axis=1).dropna().tail(window)
    if len(joined) < 20:
        return None
    value = joined["candidate"].corr(joined["portfolio"])
    return None if pd.isna(value) else float(value)


def score_candidate(code: str, df: Optional[pd.DataFrame], regime: dict,
                    portfolio_returns: Optional[pd.Series] = None,
                    params: Optional[dict] = None, meta: Optional[dict] = None,
                    as_of_date=None) -> dict:
    params = {**ETF_V2_PARAMS, **(params or {})}
    meta = meta or ETF_V2_UNIVERSE.get(code, {})
    clean = _frame(df)
    base = {"code": code, "name": meta.get("name", code), "asset_class": meta.get("asset_class", ""),
            "sector": meta.get("sector", "未分类"), "equity": bool(meta.get("equity", True)),
            "eligible": False, "reasons": [], "score": 0.0}
    if clean is None or len(clean) < params["min_history_days"]:
        base["reasons"].append("历史行情不足")
        return base
    close = clean["收盘"]
    price = float(close.iloc[-1])
    ma20, ma60, ma120 = (float(close.tail(n).mean()) for n in (20, 60, 120))
    ret20, ret60, ret120 = (_return(close, n) for n in (20, 60, 120))
    daily = close.pct_change().dropna().tail(60)
    volatility = float(daily.std(ddof=1) * math.sqrt(252)) if len(daily) > 1 else None
    drawdown = _max_drawdown(close.tail(120))
    avg_amount = _avg_amount(clean, params["liquidity_window"])
    corr = _correlation(clean, portfolio_returns, params["correlation_window"])

    trend = 20.0 + 25.0 * (price > ma20) + 30.0 * (price > ma60) + 25.0 * (price > ma120)
    weighted_momentum = sum(v * w for v, w in ((ret20, .25), (ret60, .35), (ret120, .40)) if v is not None)
    momentum = float(np.clip(50 + weighted_momentum * 180, 0, 100))
    risk_volatility = volatility if volatility is not None else .50
    risk = float(np.clip(100 - risk_volatility * 100 - abs(drawdown) * 80, 0, 100))
    if avg_amount is None or avg_amount <= 0:
        liquidity = 0.0
    else:
        liquidity = float(np.clip(35 + 25 * math.log10(max(avg_amount, 1) / params["min_avg_amount"]), 0, 100))
    diversification = 60.0 if corr is None else float(np.clip(100 * (1 - max(-.2, corr)) / 1.2, 0, 100))
    environment = regime["score"] if base["equity"] else max(60.0, 100.0 - regime["score"] * .45)
    score = (.25 * trend + .25 * momentum + .15 * risk + .15 * liquidity +
             .10 * environment + .10 * diversification)
    liquid = avg_amount is not None and avg_amount >= params["min_avg_amount"]
    trend_ok = price > ma60 or not base["equity"]
    last_date = clean["日期"].iloc[-1].date()
    stale = bool(as_of_date and (as_of_date - last_date).days > params["max_data_age_days"])
    eligible = liquid and trend_ok and not stale and score >= params["min_candidate_score"]
    reasons = [f"趋势{trend:.0f}", f"动量{momentum:.0f}", f"风险{risk:.0f}",
               f"流动性{liquidity:.0f}", f"相关性{'未知' if corr is None else f'{corr:.2f}'}"]
    if not liquid:
        reasons.append("未通过成交额门槛")
    if not trend_ok:
        reasons.append("未站上60日均线")
    if score < params["min_candidate_score"]:
        reasons.append("综合分未达买入门槛")
    if stale:
        reasons.append(f"行情陈旧（截止{last_date.isoformat()}）")
    base.update({"eligible": eligible, "reasons": reasons, "score": round(score, 2),
                 "price": price, "as_of": clean["日期"].iloc[-1].date().isoformat(),
                 "trend_score": trend, "momentum_score": round(momentum, 2),
                 "risk_score": round(risk, 2), "liquidity_score": round(liquidity, 2),
                 "environment_score": round(environment, 2),
                 "diversification_score": round(diversification, 2),
                 "correlation": corr, "volatility": volatility, "max_drawdown_120d": drawdown,
                 "avg_amount_20d": avg_amount, "returns": {"20d": ret20, "60d": ret60, "120d": ret120}})
    return base


def scan_candidates(market_data: Dict[str, pd.DataFrame], holdings_data: Optional[Dict[str, pd.DataFrame]] = None,
                    holding_weights: Optional[dict] = None, params: Optional[dict] = None,
                    universe: Optional[dict] = None, as_of_date=None) -> dict:
    params = {**ETF_V2_PARAMS, **(params or {})}
    universe = universe or ETF_V2_UNIVERSE
    regime = market_regime(market_data, params["benchmark_code"])
    portfolio_returns = _portfolio_returns(holdings_data or {}, holding_weights)
    candidates = [score_candidate(code, market_data.get(code), regime, portfolio_returns, params, meta,
                                  as_of_date=as_of_date)
                  for code, meta in universe.items()]
    candidates.sort(key=lambda row: (-row["score"], row["code"]))
    return {"regime": regime, "candidates": candidates,
            "eligible": [row for row in candidates if row["eligible"]]}


def _position_value(position) -> float:
    return max(0.0, float(getattr(position, "shares", 0) or 0) * float(getattr(position, "current_price", 0) or 0))


def _existing_exposure(positions: Iterable, universe: dict) -> tuple[dict, dict, float, float]:
    by_code, by_sector, equity, invested = {}, {}, 0.0, 0.0
    for pos in positions:
        value = _position_value(pos)
        if value <= 0:
            continue
        invested += value
        code = str(getattr(pos, "code", ""))
        meta = universe.get(code, {})
        sector = meta.get("sector") or str(getattr(pos, "sector", "未分类") or "未分类")
        by_code[code] = by_code.get(code, 0.0) + value
        by_sector[sector] = by_sector.get(sector, 0.0) + value
        subtype = str(getattr(pos, "asset_subtype", "") or "")
        is_equity = meta.get("equity") if meta else subtype in {STOCK, EQUITY_ETF, QDII_ETF, ""}
        if is_equity and subtype not in {BOND_ETF, GOLD_ETF}:
            equity += value
    return by_code, by_sector, equity, invested


def allocate_new_cash(scan: dict, positions: Iterable, investable_cash: float,
                      params: Optional[dict] = None, universe: Optional[dict] = None,
                      portfolio_cash: Optional[float] = None) -> dict:
    """在风险上限内给新增资金生成候选配置；只买不卖，现金永远是合法结果。"""
    params = {**ETF_V2_PARAMS, **(params or {})}
    universe = universe or ETF_V2_UNIVERSE
    cash = max(0.0, float(investable_cash or 0.0))
    by_code, by_sector, equity_value, invested = _existing_exposure(positions, universe)
    # --funds 可以只是账户现金中的一部分；风险权重分母仍应包含全部账户现金。
    cash_in_account = cash if portfolio_cash is None else max(cash, float(portfolio_cash or 0.0))
    total = invested + cash_in_account
    result = {"investable_cash": cash, "capital_base": total, "allocations": [],
              "cash_amount": cash, "cash_reason": "", "constraints": {
                  "single_etf_max": params["single_etf_max"],
                  "single_sector_max": params["single_sector_max"],
                  "total_equity_max": params["total_equity_max"]},
              "regime": scan["regime"]}
    if cash <= 0 or total <= 0:
        result["cash_reason"] = "没有可投资闲置资金"
        return result
    remaining = cash
    for row in scan["eligible"]:
        if len(result["allocations"]) >= params["max_candidates"] or remaining <= 0:
            break
        code, sector = row["code"], row["sector"]
        code_room = max(0.0, total * params["single_etf_max"] - by_code.get(code, 0.0))
        sector_room = max(0.0, total * params["single_sector_max"] - by_sector.get(sector, 0.0))
        equity_room = (max(0.0, total * params["total_equity_max"] - equity_value)
                       if row["equity"] else remaining)
        budget = min(remaining, code_room, sector_room, equity_room)
        price = float(row.get("price") or 0.0)
        lot = int(params["lot_size"])
        shares = int(budget / price / lot) * lot if price > 0 else 0
        gross = shares * price
        fee = max(gross * params["commission_rate"], params["min_commission"]) if gross else 0.0
        while shares > 0 and gross + fee > remaining:
            shares -= lot
            gross = shares * price
            fee = max(gross * params["commission_rate"], params["min_commission"]) if gross else 0.0
        if shares <= 0:
            continue
        amount = gross + fee
        result["allocations"].append({**row, "shares": shares, "amount": round(gross, 2),
                                      "estimated_fee": round(fee, 2),
                                      "basis": "当前风险收益与组合约束共同通过；与历史亏损无关"})
        remaining -= amount
        by_code[code] = by_code.get(code, 0.0) + gross
        by_sector[sector] = by_sector.get(sector, 0.0) + gross
        if row["equity"]:
            equity_value += gross
    result["cash_amount"] = round(max(0.0, remaining), 2)
    if not result["allocations"]:
        result["cash_reason"] = ("没有候选同时通过评分、流动性、趋势及组合风险约束；当前不买"
                                 if scan["eligible"] else "没有 ETF 达到买入标准；当前不买")
    elif remaining > max(100.0, cash * .01):
        result["cash_reason"] = "剩余资金受单ETF、单行业或总权益上限约束，保留现金"
    else:
        result["cash_reason"] = "仅保留取整和预计费用后的现金余额"
    return result
