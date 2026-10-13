"""Offline account execution evidence. Missing observations remain unknown."""
from __future__ import annotations

import datetime as dt
import math
from statistics import median

import pandas as pd

from ..asset_routing import STOCK, EQUITY_ETF, subtype_for
from ..config import MARKET_FILTER_PARAMS as CFG
from ..trading_calendar import annual_trade_dates

from .fundamental import FIELDS as FUNDAMENTAL_FIELDS
from .morning_confirmation import FIELDS as MORNING_FIELDS, context

VERSION = CFG["version"]
DIAGNOSTIC_FIELDS = (
    "market_filter_version", "market_regime", "market_breadth", "systematic_selloff",
    "benchmark_code", "benchmark_name", "benchmark_degraded", "relative_strength_state",
    "relative_return_day", "relative_return_5d", "relative_return_10d",
    "trend_break_candidate", "ma20_break_only", "support_hold", "support_break",
    "invalidation_break", "mean_reversion_candidate", "reclaim_confirmed",
    "volume_price_confirmation", "sell_confirmations", "filter_applicability",
) + FUNDAMENTAL_FIELDS + MORNING_FIELDS


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def applicable(position):
    return subtype_for(position) in {STOCK, EQUITY_ETF} and getattr(
        getattr(position, "market", None), "name", "") != "HK"


def benchmark_route(position):
    code = position.code
    routes = CFG["benchmark_routes"]
    if code in routes:
        return routes[code], False
    # Default broad benchmark, explicitly disclosed for unmapped ETFs.
    return "510500", subtype_for(position) == EQUITY_ETF


def previous_session(day):
    sessions = (annual_trade_dates(day.year) or set()) | (annual_trade_dates(day.year - 1) or set())
    return max((date for date in sessions if date < day), default=None)


def completed_frame(frame, cutoff):
    if cutoff is None or not isinstance(frame, pd.DataFrame) or frame.empty or "日期" not in frame:
        return None
    rows = frame.copy()
    rows["日期"] = pd.to_datetime(rows["日期"], errors="coerce").dt.date
    rows = rows[rows["日期"] <= cutoff].sort_values("日期").drop_duplicates("日期")
    return rows if not rows.empty else None


def fresh_quote(quote, now):
    if not isinstance(quote, dict) or not quote.get("provisional"):
        return False
    price = number(quote.get("price"))
    try:
        stamp = dt.datetime.fromisoformat(str(quote.get("as_of")))
        return bool(price and price > 0 and stamp.tzinfo and
                    stamp.astimezone(now.tzinfo).date() == now.date() and
                    abs((now - stamp).total_seconds()) <= 900)
    except (TypeError, ValueError):
        return False


def day_return(quote):
    price, previous = number(quote.get("price")), number(quote.get("previous_close"))
    if price and previous and price > 0 and previous > 0:
        return price / previous - 1
    change = number(quote.get("change_pct"))
    return change / 100 if change is not None else None


def market_environment(quotes, now, breadth=None):
    returns = {code: day_return(quote) for code, quote in quotes.items()
               if code in CFG["benchmarks"] and fresh_quote(quote, now)}
    returns = {code: value for code, value in returns.items() if value is not None}
    # Two broad indices plus at least one growth index: holdings are never breadth.
    sufficient = all(code in returns for code in ("510300", "510500")) and len(returns) >= 3
    regime = "UNKNOWN"
    if sufficient:
        values = list(returns.values())
        center = median(values)
        if sum(value <= CFG["selloff_return"] for value in values) >= math.ceil(len(values) * .75):
            regime = "BROAD_SELLOFF"
        elif center >= CFG["strong_up_return"] and all(value > 0 for value in values):
            regime = "STRONG_UP"
        elif center >= CFG["up_return"]:
            regime = "UP"
        else:
            regime = "RANGE"
    valid_breadth = isinstance(breadth, dict) and breadth.get("reliable") is True
    return {"market_regime": regime, "systematic_selloff": regime == "BROAD_SELLOFF",
            "market_breadth": breadth if valid_breadth else {"status": "UNKNOWN"},
            "market_proxy_returns": returns,
            "market_degraded": not sufficient, "market_as_of": now.isoformat()}


def history_facts(frame):
    """Frozen completed bars: support and return endpoints, never intraday lows."""
    result = {"support_level": None, "history_closes": [], "history_dates": [],
              "history_as_of": None}
    if not isinstance(frame, pd.DataFrame) or frame.empty or "收盘" not in frame:
        return result
    rows = frame.sort_values("日期").drop_duplicates("日期").tail(21)
    closes = pd.to_numeric(rows["收盘"], errors="coerce")
    if closes.isna().any() or (closes <= 0).any():
        return result
    result.update(history_closes=[float(value) for value in closes],
                  history_dates=[str(pd.Timestamp(value).date()) for value in rows["日期"]],
                  history_as_of=str(pd.Timestamp(rows.iloc[-1]["日期"]).date()))
    if "最低" in rows and len(rows) >= 5:
        lows = pd.to_numeric(rows["最低"].tail(10), errors="coerce")
        if not lows.isna().any() and (lows > 0).all():
            result["support_level"] = float(lows.min())
    return result


def relative_strength(record, benchmark_frame, quote, benchmark_quote, now):
    day = None
    if fresh_quote(quote, now) and fresh_quote(benchmark_quote, now):
        asset, benchmark = day_return(quote), day_return(benchmark_quote)
        if asset is not None and benchmark is not None:
            day = asset - benchmark
    relative = {"relative_return_day": day, "relative_return_5d": None,
                "relative_return_10d": None}
    expected = previous_session(now.date())
    facts = history_facts(completed_frame(benchmark_frame, expected)) if expected else {}
    dates, closes = record.get("history_dates", []), record.get("history_closes", [])
    bench_dates, bench_closes = facts.get("history_dates", []), facts.get("history_closes", [])
    # Align endpoints by actual sessions and require no missing sessions.
    if expected and dates and dates[-1] == str(expected) and bench_dates and bench_dates[-1] == str(expected):
        asset_map = dict(zip(dates, closes))
        bench_map = dict(zip(bench_dates, bench_closes))
        sessions = sorted(date for date in (annual_trade_dates(expected.year) or set()) if date <= expected)
        for n in (5, 10):
            window = [str(date) for date in sessions[-n-1:]]
            if len(window) == n + 1 and all(date in asset_map and date in bench_map for date in window):
                start, end = window[0], window[-1]
                relative[f"relative_return_{n}d"] = asset_map[end] / asset_map[start] - bench_map[end] / bench_map[start]
    five, ten = relative["relative_return_5d"], relative["relative_return_10d"]
    persistent = five is not None and ten is not None and five <= CFG["relative_5d_weak"] and ten <= CFG["relative_10d_weak"]
    weak = (day is not None and day <= CFG["relative_day_weak"]) or persistent
    state = "REL_WEAK" if weak else ("UNKNOWN" if day is None else
             "REL_STRONG" if day >= CFG["relative_day_strong"] else "REL_NEUTRAL")
    return {**relative, "relative_strength_state": state, "relative_persistent_weak": persistent}


def execution_evidence(position, record, quote, ma, environment=None,
                       benchmark_frames=None, benchmark_quotes=None, now=None):
    environment = environment or {"market_regime": "UNKNOWN", "market_breadth": {"status": "UNKNOWN"}}
    result = {key: environment.get(key) for key in ("market_regime", "market_breadth", "systematic_selloff")}
    result.update(market_filter_version=VERSION, filter_applicability="full" if applicable(position) else "not_applicable",
                  benchmark_code=None, benchmark_name=None, benchmark_degraded=True,
                  relative_strength_state="UNKNOWN", relative_return_day=None,
                  relative_return_5d=None, relative_return_10d=None)
    if not applicable(position):
        return result
    code, degraded = benchmark_route(position)
    quotes = benchmark_quotes or {}
    if now and not fresh_quote(quotes.get(code), now) and code != "510300" and fresh_quote(quotes.get("510300"), now):
        code, degraded = "510300", True
    result.update(benchmark_code=code, benchmark_name=CFG["benchmarks"][code],
                  benchmark_degraded=degraded or not (now and fresh_quote(quotes.get(code), now)))
    if now:
        result.update(relative_strength(record, (benchmark_frames or {}).get(code), quote,
                                        quotes.get(code) or {}, now))
    features = record.get("technical_features") or {}
    price, atr, ma20 = (number(value) for value in (quote.get("price"), features.get("ATR"), features.get("MA20")))
    stop = number(record.get("invalidation_price"))
    support = number(record.get("support_level"))
    trend = bool(price and ((ma20 and price < ma20) or
                 (not ma20 and "MA20有效跌破" in ma.get("ma_state", ""))))
    invalid = bool(price and stop and price <= stop)
    history_fresh = bool(now and record.get("history_as_of") == str(previous_session(now.date())))
    if now and not history_fresh:
        support = None
    support_break = bool(price and atr and support and price < support - CFG["support_break_atr"] * atr)
    support_hold = bool(price and support and price >= support)
    previous = number(quote.get("previous_close"))
    falling = bool(price and previous and price < previous)
    ratio = None
    volume, base, progress = (number(value) for value in (quote.get("volume"), record.get("reference_volume"), quote.get("session_progress")))
    if volume and base and progress and .15 <= progress <= 1:
        ratio = volume / (base if progress >= .9375 else base * progress)
    volume_sell = bool(falling and ratio and ratio >= CFG["sell_volume_ratio"])
    momentum = bool(falling and atr and previous - price >= CFG["sell_momentum_atr"] * atr)
    weak = result["relative_strength_state"] == "REL_WEAK"
    independent = bool(result["market_regime"] in ("RANGE", "UP", "STRONG_UP") and weak and falling)
    confirmations = [name for name, yes in (("SUPPORT_BREAK", support_break), ("RELATIVE_WEAK", weak),
                    ("VOLUME_SELL", volume_sell), ("DOWN_MOMENTUM", momentum), ("INDEPENDENT_WEAK", independent)) if yes]
    # On broad selloff days raw downside momentum alone is systematic, not independent evidence.
    if result["market_regime"] == "BROAD_SELLOFF" and not weak and not support_break:
        confirmations = []
    low = number(quote.get("low"))
    ma10 = number(features.get("MA10"))
    reclaim_levels = [value for value in (support, ma10, ma20) if value and stop and value > stop]
    reclaim = bool(price and low and atr and any(low < level <= price for level in reclaim_levels)
                   and price - low >= CFG["reclaim_atr"] * atr)
    opening, high = number(quote.get("open")), number(quote.get("high"))
    stabilizing = bool(price and opening and high and atr and price >= opening and high - price <= .5 * atr)
    rsi = number(features.get("RSI14"))
    oversold = bool((rsi is not None and rsi <= CFG["oversold_rsi"]) or
                    (low and ma20 and atr and ma20 - low >= CFG["oversold_atr"] * atr))
    zone = record.get("buy_zone")
    in_zone = bool(price and zone and len(zone) == 2 and zone[0] <= price <= zone[1])
    near = bool(price and atr and support and abs(price - support) <= CFG["support_atr"] * atr)
    candidate = bool(result["market_regime"] in ("RANGE", "BROAD_SELLOFF") and oversold and
                     (near or in_zone) and reclaim and stabilizing and result["relative_strength_state"] in ("REL_NEUTRAL", "REL_STRONG") and
                     not invalid and not support_break and not volume_sell and not momentum and not weak and
                     not result.get("relative_persistent_weak") and history_fresh)
    result.update(trend_break_candidate=trend, ma20_break_only=trend and not invalid and not confirmations,
                  support_hold=support_hold, support_break=support_break, invalidation_break=invalid,
                  mean_reversion_candidate=candidate, reclaim_confirmed=reclaim,
                  volume_price_confirmation=volume_sell or momentum, sell_confirmations=confirmations,
                  volume_ratio=ratio, mean_reversion_state="RECLAIM_CONFIRMED" if candidate else
                  "SUPPORT_HOLD" if support_hold else "UNCONFIRMED")
    result.update(context(record, quote, result, now, bool(now and fresh_quote(quote, now)),
                          price, low, atr, support, ma20))
    return result
