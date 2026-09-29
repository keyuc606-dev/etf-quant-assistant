"""Reproducible monthly review from raw advice, executions and daily bars."""

from __future__ import annotations

import csv
import datetime as dt
import math
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import pandas as pd

from .advice_performance import WINDOWS, correlate_execution, evaluate


ACTIONS = ("ADD", "REDUCE", "RISK_EXIT", "HOLD", "NO_ACTION", "MANUAL_REVIEW")
COMPLETE_FIELDS = (
    "final_action", "final_decision_version", "data_quality", "decision_confidence",
    "asset_subtype", "suggested_quantity", "reference_close", "advice_session",
)


def _month(value: str) -> tuple[dt.date, dt.date]:
    try:
        first = dt.datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError as error:
        raise ValueError("--month 必须为 YYYY-MM") from error
    next_month = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return first, next_month - dt.timedelta(days=1)


def months_for_review(records: list[dict], requested: str | None = None,
                      backfill: bool = False) -> list[str]:
    """Select one validated month or every valid month present in raw history."""
    if not backfill:
        if requested is None:
            raise ValueError("必须指定 --month 或 --backfill")
        _month(requested)
        return [requested]
    months = set()
    for record in records:
        value = str(record.get("as_of", ""))[:7]
        try:
            _month(value)
        except ValueError:
            continue
        months.add(value)
    return sorted(months)


def normalize_action(record: dict) -> str:
    action = record.get("final_action") or (record.get("final_decision") or {}).get("final_action")
    action = action or record.get("action_tendency") or "NO_ACTION"
    aliases = {"ADD_SMALL": "ADD", "WATCH": "MANUAL_REVIEW"}
    action = aliases.get(str(action).upper(), str(action).upper())
    return action if action in ACTIONS else "MANUAL_REVIEW"


def classify_record(record: dict) -> tuple[str, list[str]]:
    missing = [field for field in COMPLETE_FIELDS if record.get(field) is None]
    if record.get("advice_session") == "09:20_LEGACY":
        return "legacy_record", missing
    if not missing:
        return "complete", []
    # A legacy fact can still produce outcomes when its identity, date and price exist.
    calculable = all(record.get(field) is not None for field in
                     ("advice_id", "as_of", "code", "reference_close"))
    return ("partial_backfill" if calculable else "legacy_record"), missing


def _safe_evaluate(record: dict, frame: pd.DataFrame | None, cutoff: dt.date) -> dict:
    normalized = dict(record)
    normalized.setdefault("rule_version", "legacy/unknown")
    normalized.setdefault("advice_id", f"legacy:{record.get('as_of')}:{record.get('code')}")
    try:
        return evaluate(normalized, frame, cutoff)
    except (KeyError, TypeError, ValueError):
        return {
            "advice_id": normalized["advice_id"], "rule_version": normalized["rule_version"],
            "execution": "unknown",
            "windows": {str(n): {"status": "pending", "sessions": 0} for n in WINDOWS},
        }


def _mean(values: Iterable[float | None]) -> float | None:
    clean = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            clean.append(number)
    return sum(clean) / len(clean) if clean else None


def _profit_loss_ratio(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    wins = [value for value in clean if value > 0]
    losses = [abs(value) for value in clean if value < 0]
    if not wins or not losses:
        return None
    return (sum(wins) / len(wins)) / (sum(losses) / len(losses))


def _directional(action: str, value: float | None) -> float | None:
    if value is None:
        return None
    if action in ("REDUCE", "RISK_EXIT"):
        return -float(value)
    return float(value)


def _execution_details(record: dict, executions: list[dict]) -> dict:
    matches = [row for row in executions if row.get("related_plan_id") == record.get("advice_id")]
    action = normalize_action(record)
    desired_side = "BUY" if action == "ADD" else "SELL" if action in ("REDUCE", "RISK_EXIT") else None
    relevant = [row for row in matches if row.get("side") == desired_side] if desired_side else matches
    quantity = sum(int(row.get("quantity") or 0) for row in relevant)
    notional = sum(float(row.get("quantity") or 0) * float(row.get("price") or 0) for row in relevant)
    actual_price = notional / quantity if quantity else None
    suggested_quantity = int(record.get("suggested_quantity") or 0)
    zone = record.get("buy_zone") if desired_side == "BUY" else record.get("reduce_zone")
    benchmark = None
    if zone and len(zone) == 2:
        benchmark = (float(zone[0]) + float(zone[1])) / 2
    elif record.get("reference_close"):
        benchmark = float(record["reference_close"])
    price_deviation = actual_price / benchmark - 1 if actual_price and benchmark else None
    slippage = None
    if price_deviation is not None and desired_side:
        slippage = price_deviation if desired_side == "BUY" else -price_deviation
    slippage_amount = (slippage * benchmark * quantity
                       if slippage is not None and benchmark is not None else None)
    fees = sum(float(row.get("fee") or 0) for row in matches)
    realized = [float(row["realized_pnl"]) for row in matches if row.get("realized_pnl") is not None]
    correlated = correlate_execution(record, executions) if record.get("advice_id") else {
        "status": "unknown", "realized_pnl": None, "realized_return": None,
    }
    buys = [row for row in matches if row.get("side") == "BUY"]
    sells = [row for row in matches if row.get("side") == "SELL"]
    buy_qty = sum(int(row.get("quantity") or 0) for row in buys)
    sell_qty = sum(int(row.get("quantity") or 0) for row in sells)
    paired = min(buy_qty, sell_qty)
    avg_buy = (sum(int(row["quantity"]) * float(row["price"]) for row in buys) / buy_qty
               if buy_qty else None)
    avg_sell = (sum(int(row["quantity"]) * float(row["price"]) for row in sells) / sell_qty
                if sell_qty else None)
    t_gross = (avg_sell - avg_buy) * paired if paired and avg_buy is not None and avg_sell is not None else None
    is_t = bool(record.get("t_economics") or record.get("reduction_reason_type") == "TACTICAL_T")
    return {
        "trade_count": len(matches), "actual_quantity": quantity,
        "quantity_deviation": quantity - suggested_quantity if desired_side else None,
        "actual_price": actual_price, "price_deviation": price_deviation,
        "slippage_rate": slippage, "slippage_amount": slippage_amount, "fees": fees,
        "realized_pnl": sum(realized) if realized else correlated.get("realized_pnl"),
        "realized_return": correlated.get("realized_return"),
        "is_t": is_t, "t_paired_quantity": paired if is_t else 0,
        "t_gross_pnl": t_gross if is_t else None,
        "t_net_pnl": (t_gross - fees) if is_t and t_gross is not None else None,
    }


def _aggregate(rows: list[dict]) -> dict:
    directional_20 = [row["directional_20d_return"] for row in rows]
    settled_20 = [row for row in rows if row["window_20_status"] != "pending"]
    return {
        "samples": len(rows),
        "settled_20": len(settled_20),
        "mean_return": {str(n): _mean(row[f"return_{n}d"] for row in rows) for n in WINDOWS},
        "mean_action_return": {
            str(n): _mean(row[f"directional_{n}d_return"] for row in rows) for n in WINDOWS
        },
        "mean_directional_return_20d": _mean(directional_20),
        "target_first": sum(row["window_20_status"] == "target_first" for row in rows),
        "invalidation_first": sum(row["window_20_status"] == "invalidation_first" for row in rows),
        "target_first_rate": (sum(row["window_20_status"] == "target_first" for row in rows) /
                              len(settled_20) if settled_20 else None),
        "invalidation_first_rate": (sum(row["window_20_status"] == "invalidation_first" for row in rows) /
                                    len(settled_20) if settled_20 else None),
        "mean_mfe": _mean(row["mfe_20d"] for row in rows),
        "mean_mae": _mean(row["mae_20d"] for row in rows),
        "profit_loss_ratio": _profit_loss_ratio(directional_20),
        "max_drawdown": min((row["directional_mae_20d"] for row in rows
                             if row["directional_mae_20d"] is not None), default=None),
    }


def _group(rows: list[dict], key: str) -> dict:
    groups = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "UNKNOWN")].append(row)
    return {name: _aggregate(items) for name, items in sorted(groups.items())}


def build_monthly_review(records: list[dict], executions: list[dict],
                         market_data: dict[str, pd.DataFrame], month: str,
                         cutoff: dt.date | None = None) -> dict:
    first, last = _month(month)
    cutoff = cutoff or dt.date.today()
    selected = []
    for record in records:
        try:
            advice_day = dt.date.fromisoformat(str(record.get("as_of", "")))
        except ValueError:
            continue
        if first <= advice_day <= last:
            selected.append(record)
    rows = []
    for record in selected:
        quality, missing = classify_record(record)
        outcome = _safe_evaluate(record, market_data.get(str(record.get("code"))), cutoff)
        action = normalize_action(record)
        execution = _execution_details(record, executions)
        row = {
            "advice_id": record.get("advice_id"), "as_of": record.get("as_of"),
            "code": record.get("code"), "name": record.get("name"), "action": action,
            "record_quality": quality, "missing_fields": ";".join(missing),
            "missing_intraday_context": not bool(
                record.get("generated_at") and record.get("advice_session") == "10:30_EXECUTION"
            ),
            "rule_version": record.get("rule_version") or "legacy/unknown",
            "decision_confidence": record.get("decision_confidence") or "UNKNOWN",
            "data_quality": record.get("data_quality") or "UNKNOWN",
            "asset_subtype": record.get("asset_subtype") or "UNKNOWN",
            "suggested_quantity": int(record.get("suggested_quantity") or 0),
            "suggested_amount": float(record.get("suggested_amount") or 0),
        }
        for horizon in WINDOWS:
            window = outcome["windows"].get(str(horizon), {"status": "pending", "sessions": 0})
            row[f"window_{horizon}_status"] = window.get("status", "pending")
            row[f"sessions_{horizon}"] = window.get("sessions", 0)
            row[f"return_{horizon}d"] = window.get("end_return")
            row[f"directional_{horizon}d_return"] = _directional(
                action, row[f"return_{horizon}d"]
            )
        twenty = outcome["windows"].get("20", {})
        row["mfe_20d"] = twenty.get("max_favorable")
        row["mae_20d"] = twenty.get("max_adverse")
        row["directional_mae_20d"] = (
            -row["mfe_20d"] if action in ("REDUCE", "RISK_EXIT") and row["mfe_20d"] is not None
            else row["mae_20d"]
        )
        row.update(execution)
        row["unexecuted_actionable"] = action in ("ADD", "REDUCE", "RISK_EXIT") and not execution["trade_count"]
        rows.append(row)

    actual = [row for row in rows if row["trade_count"]]
    t_rows = [row for row in rows if row["is_t"] and row["t_gross_pnl"] is not None]
    no_action_returns = [row["return_20d"] for row in rows]
    report = {
        "month": month, "data_start": min((row["as_of"] for row in rows), default=None),
        "advice_sample_count": len(rows),
        "complete_sample_count": sum(row["record_quality"] == "complete" for row in rows),
        "legacy_sample_count": sum(row["record_quality"] != "complete" for row in rows),
        "partial_backfill_count": sum(row["record_quality"] == "partial_backfill" for row in rows),
        "real_execution_sample_count": len(actual),
        "missing_context_count": sum(row["missing_intraday_context"] for row in rows),
        "overall": _aggregate(rows), "by_action": _group(rows, "action"),
        "by_decision_confidence": _group(rows, "decision_confidence"),
        "by_data_quality": _group(rows, "data_quality"),
        "by_asset_subtype": _group(rows, "asset_subtype"),
        "by_rule_version": _group(rows, "rule_version"),
        "comparison": {
            "system_advice_20d": _mean(row["directional_20d_return"] for row in rows),
            "actual_execution_return": _mean(row["realized_return"] for row in actual),
            "no_action_hold_20d": _mean(no_action_returns),
        },
        "execution": {
            "trade_count": sum(row["trade_count"] for row in rows),
            "fees": sum(row["fees"] for row in rows),
            "realized_pnl": sum(row["realized_pnl"] for row in actual if row["realized_pnl"] is not None),
            "mean_slippage_rate": _mean(row["slippage_rate"] for row in actual),
            "slippage_amount": sum(row["slippage_amount"] for row in actual
                                   if row["slippage_amount"] is not None),
            "mean_quantity_deviation": _mean(row["quantity_deviation"] for row in actual),
        },
        "t_trading": {
            "samples": len(t_rows), "gross_pnl": sum(row["t_gross_pnl"] for row in t_rows),
            "fees": sum(row["fees"] for row in t_rows),
            "net_pnl": sum(row["t_net_pnl"] for row in t_rows),
            "slippage_rate": _mean(row["slippage_rate"] for row in t_rows),
            "slippage_amount": sum(row["slippage_amount"] for row in t_rows
                                   if row["slippage_amount"] is not None),
        },
        "unexecuted": {
            "samples": sum(row["unexecuted_actionable"] for row in rows),
            "theoretical_20d": _mean(row["directional_20d_return"] for row in rows
                                     if row["unexecuted_actionable"]),
        },
        "rows": rows,
    }
    return report


def _pct(value) -> str:
    return "N/A" if value is None else f"{value:+.2%}"


def render_markdown(report: dict) -> str:
    overall = report["overall"]
    ratio = ("N/A" if overall["profit_loss_ratio"] is None
             else f"{overall['profit_loss_ratio']:.2f}")
    lines = [f"# {report['month']} 月度策略复盘", "",
             "> 由原始 advice records、显式关联 executions 与日线重算；未保存的盘中事实不补造。", "",
             "## 数据覆盖", "",
             f"- 数据起点：{report['data_start'] or '无样本'}",
             f"- 建议样本：{report['advice_sample_count']}；完整字段：{report['complete_sample_count']}；"
             f"legacy 兼容：{report['legacy_sample_count']}（partial backfill {report['partial_backfill_count']}）",
             f"- 真实执行样本：{report['real_execution_sample_count']}；缺失盘中上下文：{report['missing_context_count']}", "",
             "## 建议层", "",
             f"5/10/20 个真实交易日平均收益：{_pct(overall['mean_return']['5'])} / "
             f"{_pct(overall['mean_return']['10'])} / {_pct(overall['mean_return']['20'])}",
             f"目标先触及率：{_pct(overall['target_first_rate'])}；失效先触及率："
             f"{_pct(overall['invalidation_first_rate'])}",
             f"平均 MFE / MAE：{_pct(overall['mean_mfe'])} / {_pct(overall['mean_mae'])}；"
             f"盈亏比：{ratio}；"
             f"样本内最大回撤：{_pct(overall['max_drawdown'])}", "",
             "| 动作 | 样本 | 已结算20日 | 5日 | 10日 | 20日 |", "|---|---:|---:|---:|---:|---:|"]
    for action in ACTIONS:
        item = report["by_action"].get(action, _aggregate([]))
        lines.append(f"| {action} | {item['samples']} | {item['settled_20']} | "
                     f"{_pct(item['mean_action_return']['5'])} | "
                     f"{_pct(item['mean_action_return']['10'])} | "
                     f"{_pct(item['mean_action_return']['20'])} |")
    comparison = report["comparison"]
    lines += ["", "## 建议 vs 实际执行 vs 不操作", "",
              f"- 系统动作方向调整后20日表现：{_pct(comparison['system_advice_20d'])}",
              f"- 用户实际执行已实现收益率：{_pct(comparison['actual_execution_return'])}",
              f"- 若保持原持仓不操作的20日价格表现：{_pct(comparison['no_action_hold_20d'])}",
              f"- 未执行明确建议：{report['unexecuted']['samples']} 条；理论20日表现："
              f"{_pct(report['unexecuted']['theoretical_20d'])}", "",
              "实际执行只统计显式 related_plan_id 关联；未关联成交不猜测归因。", "",
              "## 执行与做T", "",
              f"- 成交 {report['execution']['trade_count']} 笔；已实现盈亏 "
              f"￥{report['execution']['realized_pnl']:+,.2f}；费用 ￥{report['execution']['fees']:,.2f}；"
              f"平均滑点 {_pct(report['execution']['mean_slippage_rate'])}（估算金额 "
              f"￥{report['execution']['slippage_amount']:+,.2f}）",
              f"- 做T：{report['t_trading']['samples']} 组；毛收益 ￥{report['t_trading']['gross_pnl']:+,.2f}；"
              f"费用 ￥{report['t_trading']['fees']:,.2f}；实际成交价内含滑点估算 "
              f"￥{report['t_trading']['slippage_amount']:+,.2f}；净贡献 "
              f"￥{report['t_trading']['net_pnl']:+,.2f}", "",
              "## 分组（规则版本严格分列）", ""]
    for title, key in (("decision_confidence", "by_decision_confidence"),
                       ("data_quality", "by_data_quality"),
                       ("asset_subtype", "by_asset_subtype"),
                       ("rule_version", "by_rule_version")):
        lines.append(f"### {title}")
        lines.append("")
        for name, item in report[key].items():
            lines.append(f"- {name}：{item['samples']} 条；10日平均 {_pct(item['mean_return']['10'])}；"
                         f"20日动作表现 {_pct(item['mean_directional_return_20d'])}")
        lines.append("")
    lines += ["## 口径", "",
              "- 5/10/20 窗口只按行情中实际存在的交易日推进，不按自然日。",
              "- REDUCE/RISK_EXIT 的“动作表现”按相对继续持有的规避效果反向计；其余动作展示后续价格表现。",
              "- 最大回撤为本月建议样本在20交易日窗口内的最差方向化 MAE，不是账户净值回撤。",
              "- 缺失行情、盘中上下文或未录入成交保持空值/unknown，不进行事后推断。", ""]
    return "\n".join(lines)


def write_monthly_review(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"strategy_review_{report['month']}"
    csv_path, md_path = output_dir / f"{stem}.csv", output_dir / f"{stem}.md"
    fields = list(report["rows"][0]) if report["rows"] else [
        "advice_id", "as_of", "code", "action", "record_quality"
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report["rows"])
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return csv_path, md_path
