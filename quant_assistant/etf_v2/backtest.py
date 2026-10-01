"""V2 walk-forward 回测与样本外验证。

规则参数固定，不做参数搜索。每周末只使用当时可见历史生成目标，下一交易日开盘成交。
"""

from __future__ import annotations

import datetime
import math
from pathlib import Path

import pandas as pd

from ..config import ETF_V2_PARAMS, ETF_V2_UNIVERSE
from .engine import allocate_new_cash, scan_candidates


def _tables(market_data: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    closes, opens = [], []
    for code, frame in market_data.items():
        if frame is None or frame.empty or "日期" not in frame or "收盘" not in frame:
            continue
        clean = frame.copy()
        clean["日期"] = pd.to_datetime(clean["日期"], errors="coerce")
        clean["收盘"] = pd.to_numeric(clean["收盘"], errors="coerce")
        if "开盘" not in clean:
            clean["开盘"] = clean["收盘"]
        clean["开盘"] = pd.to_numeric(clean["开盘"], errors="coerce")
        clean = clean.dropna(subset=["日期", "收盘"]).sort_values("日期").drop_duplicates("日期")
        closes.append(clean.set_index("日期")["收盘"].rename(code))
        opens.append(clean.set_index("日期")["开盘"].rename(code))
    if not closes:
        return pd.DataFrame(), pd.DataFrame()
    return pd.concat(closes, axis=1).sort_index().ffill(), pd.concat(opens, axis=1).sort_index()


def _slice_history(market_data: dict, date: pd.Timestamp) -> dict:
    return {code: frame[pd.to_datetime(frame["日期"]) <= date].copy()
            for code, frame in market_data.items() if frame is not None and not frame.empty}


def _metrics(history: list[dict], start_index: int = 0) -> dict:
    rows = history[start_index:]
    if len(rows) < 2:
        return {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0,
                "benchmark_return": 0.0, "excess_return": 0.0, "days": len(rows)}
    base, final = rows[0]["equity"], rows[-1]["equity"]
    bench_base, bench_final = rows[0]["benchmark"], rows[-1]["benchmark"]
    days = max((rows[-1]["date"] - rows[0]["date"]).days, 1)
    total_return = final / base - 1 if base else 0.0
    annual = (final / base) ** (365 / days) - 1 if base > 0 and final > 0 else -1.0
    peak, max_dd = base, 0.0
    for row in rows:
        peak = max(peak, row["equity"])
        max_dd = max(max_dd, 1 - row["equity"] / peak if peak else 0.0)
    benchmark_return = bench_final / bench_base - 1 if bench_base else 0.0
    return {"total_return": total_return, "annual_return": annual, "max_drawdown": max_dd,
            "benchmark_return": benchmark_return, "excess_return": total_return - benchmark_return,
            "days": len(rows)}


def run_walk_forward(market_data: dict, initial_capital: float = 100_000.0,
                     params: dict | None = None, split_ratio: float = .70) -> dict:
    params = {**ETF_V2_PARAMS, **(params or {})}
    closes, opens = _tables(market_data)
    benchmark_code = params["benchmark_code"]
    if closes.empty or benchmark_code not in closes:
        raise ValueError("V2回测缺少沪深300基准行情")
    valid = closes[benchmark_code].dropna()
    if len(valid) < params["min_history_days"] + 20:
        raise ValueError("V2回测历史长度不足")
    dates = valid.index
    start_at = params["min_history_days"] - 1
    weekly_last = pd.Series(dates[start_at:], index=dates[start_at:]).resample("W-FRI").last().dropna()
    signal_dates = set(pd.to_datetime(weekly_last.values))
    cash, shares, pending = float(initial_capital), {}, None
    trades, history = [], []
    benchmark_base = float(valid.iloc[start_at])
    total_cost = 0.0

    for date in dates[start_at:]:
        open_row = opens.loc[date] if date in opens.index else pd.Series(dtype=float)
        close_row = closes.loc[date]
        if pending is not None and date > pending["date"]:
            target_values = pending["targets"]
            # 先卖后买；信号日不成交，使用下一交易日开盘并计滑点/佣金。
            for code, qty in list(shares.items()):
                raw = open_row.get(code)
                if pd.isna(raw) or raw is None:
                    continue
                price = float(raw) * (1 - params["slippage_rate"])
                desired = int(target_values.get(code, 0.0) / max(float(raw), .0001) /
                              params["lot_size"]) * params["lot_size"]
                sell = max(0, qty - desired)
                if sell:
                    gross = sell * price
                    fee = max(gross * params["commission_rate"], params["min_commission"])
                    cash += gross - fee
                    shares[code] -= sell
                    total_cost += fee + sell * (float(raw) - price)
                    trades.append({"date": date.date(), "code": code, "side": "SELL",
                                   "shares": sell, "price": price, "fee": fee})
            for code, target in sorted(target_values.items(), key=lambda item: -item[1]):
                raw = open_row.get(code)
                if pd.isna(raw) or raw is None:
                    continue
                price = float(raw) * (1 + params["slippage_rate"])
                desired = int(target / price / params["lot_size"]) * params["lot_size"]
                buy = max(0, desired - shares.get(code, 0))
                while buy > 0:
                    gross = buy * price
                    fee = max(gross * params["commission_rate"], params["min_commission"])
                    if gross + fee <= cash:
                        break
                    buy -= params["lot_size"]
                if buy:
                    gross = buy * price
                    fee = max(gross * params["commission_rate"], params["min_commission"])
                    cash -= gross + fee
                    shares[code] = shares.get(code, 0) + buy
                    total_cost += fee + buy * (price - float(raw))
                    trades.append({"date": date.date(), "code": code, "side": "BUY",
                                   "shares": buy, "price": price, "fee": fee})
            pending = None

        equity = cash + sum(qty * float(close_row.get(code, 0) or 0)
                            for code, qty in shares.items() if pd.notna(close_row.get(code)))
        benchmark = initial_capital * float(close_row[benchmark_code]) / benchmark_base
        history.append({"date": date.date(), "equity": equity, "cash": cash, "benchmark": benchmark})

        if date in signal_dates and date != dates[-1]:
            sliced = _slice_history(market_data, date)
            scan = scan_candidates(sliced, params=params, as_of_date=date.date())
            # 用总资产作为研究账户资金生成受约束目标；不读取未来数据，也不以盈亏改规则。
            allocation = allocate_new_cash(scan, [], equity, params=params)
            pending = {"date": date, "targets": {row["code"]: row["amount"]
                                                  for row in allocation["allocations"]}}

    split_index = max(1, min(len(history) - 2, int(len(history) * split_ratio)))
    result = {"history": history, "trades": trades, "transaction_cost": total_cost,
              "full": _metrics(history), "in_sample": _metrics(history[:split_index + 1]),
              "out_of_sample": _metrics(history, split_index),
              "split_date": history[split_index]["date"],
              "notes": ["参数固定，未针对结果搜索参数。",
                        "每周收盘生成信号，下一交易日开盘成交，含0.1%滑点与佣金。",
                        "样本外区间按时间顺序后30%划分；结果不构成未来收益保证。"]}
    return result


def render_backtest(result: dict) -> str:
    lines = ["# ETF V2 walk-forward 回测与样本外验证", "",
             f"- 样本内/样本外分界：{result['split_date']}",
             f"- 模拟交易：{len(result['trades'])}笔",
             f"- 估算交易成本（佣金+滑点）：￥{result['transaction_cost']:,.2f}", "",
             "|区间|总收益|年化|最大回撤|宽基基准|超额收益|",
             "|---|---:|---:|---:|---:|---:|"]
    for key, label in (("in_sample", "样本内"), ("out_of_sample", "样本外"), ("full", "全样本")):
        row = result[key]
        lines.append(f"|{label}|{row['total_return']:.2%}|{row['annual_return']:.2%}|"
                     f"{row['max_drawdown']:.2%}|{row['benchmark_return']:.2%}|{row['excess_return']:+.2%}|")
    lines.extend(["", *[f"- {note}" for note in result["notes"]], ""])
    return "\n".join(lines)


def write_backtest(result: dict, report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"etf-v2-backtest-{datetime.date.today().isoformat()}.md"
    path.write_text(render_backtest(result), encoding="utf-8")
    return path
