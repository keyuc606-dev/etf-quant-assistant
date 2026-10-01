"""V2 模拟账户：建议日只挂起，下一可用交易日开盘模拟成交。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from ..config import ETF_V2_PARAMS, ETF_V2_UNIVERSE


def new_paper_state(initial_cash: float = 100_000.0) -> dict:
    return {"version": 1, "initial_cash": float(initial_cash), "cash": float(initial_cash),
            "positions": {}, "pending": None, "history": [], "trades": [],
            "benchmark": {"code": ETF_V2_PARAMS["benchmark_code"], "base_price": None}}


def load_local(path: Path) -> dict:
    if not path.exists():
        return new_paper_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"V2模拟账户文件损坏: {path} ({error})") from error
    if not isinstance(state, dict) or state.get("version") != 1:
        raise RuntimeError(f"V2模拟账户版本无效: {path}")
    return state


def save_local(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def paper_positions(state: dict) -> list:
    result = []
    for code, row in state.get("positions", {}).items():
        meta = ETF_V2_UNIVERSE.get(code, {})
        result.append(SimpleNamespace(code=code, shares=int(row.get("shares", 0)),
                                      current_price=float(row.get("last_price", row.get("cost", 0))),
                                      sector=meta.get("sector", ""),
                                      asset_subtype="EQUITY_ETF" if meta.get("equity") else ""))
    return result


def _next_open(frame: pd.DataFrame, after: str) -> tuple[str, float] | None:
    if frame is None or frame.empty or "日期" not in frame:
        return None
    clean = frame.copy()
    clean["日期"] = pd.to_datetime(clean["日期"], errors="coerce")
    price_col = "开盘" if "开盘" in clean else "收盘"
    clean[price_col] = pd.to_numeric(clean[price_col], errors="coerce")
    later = clean[(clean["日期"] > pd.Timestamp(after)) & clean[price_col].notna()].sort_values("日期")
    if later.empty:
        return None
    row = later.iloc[0]
    return row["日期"].date().isoformat(), float(row[price_col])


def settle_pending(state: dict, market_data: dict, params: dict | None = None) -> dict:
    params = {**ETF_V2_PARAMS, **(params or {})}
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return state
    executions = []
    for order in pending.get("orders", []):
        code = order["code"]
        next_quote = _next_open(market_data.get(code), pending["signal_as_of"])
        if next_quote is None:
            continue
        day, raw_price = next_quote
        price = raw_price * (1 + params["slippage_rate"])
        lot = int(params["lot_size"])
        budget = min(float(order["budget"]), float(state["cash"]))
        shares = int(budget / price / lot) * lot
        gross = shares * price
        fee = max(gross * params["commission_rate"], params["min_commission"]) if gross else 0.0
        while shares > 0 and gross + fee > state["cash"]:
            shares -= lot
            gross = shares * price
            fee = max(gross * params["commission_rate"], params["min_commission"]) if gross else 0.0
        if shares <= 0:
            continue
        pos = state["positions"].setdefault(code, {"shares": 0, "cost": 0.0, "last_price": price})
        old_cost = float(pos["cost"]) * int(pos["shares"])
        pos["shares"] = int(pos["shares"]) + shares
        pos["cost"] = (old_cost + gross + fee) / pos["shares"]
        pos["last_price"] = price
        state["cash"] = float(state["cash"]) - gross - fee
        executions.append({"date": day, "code": code, "side": "BUY", "shares": shares,
                           "price": price, "fee": fee, "signal_as_of": pending["signal_as_of"]})
    if executions:
        state.setdefault("trades", []).extend(executions)
        state["pending"] = None
    return state


def mark_to_market(state: dict, market_data: dict, as_of: str) -> dict:
    value = float(state.get("cash", 0.0))
    for code, pos in state.get("positions", {}).items():
        frame = market_data.get(code)
        if frame is not None and not frame.empty and "收盘" in frame:
            price = float(pd.to_numeric(frame["收盘"], errors="coerce").dropna().iloc[-1])
            pos["last_price"] = price
        value += int(pos.get("shares", 0)) * float(pos.get("last_price", 0.0))
    benchmark = state.setdefault("benchmark", {"code": ETF_V2_PARAMS["benchmark_code"], "base_price": None})
    bench_frame = market_data.get(benchmark["code"])
    benchmark_nav = None
    if bench_frame is not None and not bench_frame.empty:
        price = float(pd.to_numeric(bench_frame["收盘"], errors="coerce").dropna().iloc[-1])
        if not benchmark.get("base_price"):
            benchmark["base_price"] = price
        benchmark_nav = price / float(benchmark["base_price"])
    nav = value / float(state["initial_cash"]) if state.get("initial_cash") else 1.0
    row = {"date": as_of, "value": round(value, 2), "nav": nav, "benchmark_nav": benchmark_nav,
           "cash": round(float(state.get("cash", 0.0)), 2)}
    history = state.setdefault("history", [])
    if history and history[-1].get("date") == as_of:
        history[-1] = row
    else:
        history.append(row)
    return row


def queue_allocation(state: dict, allocation: dict, signal_as_of: str) -> None:
    if state.get("pending") or not allocation.get("allocations"):
        return
    orders = [{"code": row["code"], "budget": row["amount"] + row["estimated_fee"]}
              for row in allocation["allocations"]]
    state["pending"] = {"signal_as_of": signal_as_of, "orders": orders}
