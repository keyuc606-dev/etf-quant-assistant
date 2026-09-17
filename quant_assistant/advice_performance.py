"""Immutable morning advice facts and reproducible, close-only outcome calculations.

All prices in this module are daily market prices. Intraday snapshots never enter it.
"""

import datetime as dt
import hashlib
from collections import defaultdict

import pandas as pd

from .data.fetcher import CN_TZ


WINDOWS = (5, 10, 20)
STAT_VERSION = 1
MIN_SAMPLE = 10
RULE_VERSION = "v3-advice-1"


def make_records(reports: dict, generated_at: dt.datetime, commit: str) -> list[dict]:
    as_of = generated_at.astimezone(CN_TZ).date().isoformat()
    by_code = {a["code"]: a for a in reports["advices"]}
    records = []
    for view in reports["views"]:
        pos = view["position"]
        advice = by_code[pos.code]
        reference = float(pos.current_price) if view["available"] else None
        frame = reports.get("stock_data", {}).get(pos.code)
        volume = None
        if frame is not None and not frame.empty and "成交量" in frame:
            value = pd.to_numeric(frame.iloc[-1]["成交量"], errors="coerce")
            volume = float(value) if pd.notna(value) else None
        identity = f"{as_of}:{pos.code}:{RULE_VERSION}"
        records.append({
            "advice_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
            "as_of": as_of, "code": pos.code, "name": pos.name,
            "asset_type": pos.asset_type or ("ETF" if pos.market.name == "ETF" else "STOCK"),
            "market": pos.market.name, "action_tendency": advice["action"],
            "buy_zone": advice["buy_range"], "reduce_zone": advice["reduce_range"],
            "invalidation_price": advice["stop"], "target_price": advice["target"],
            "confidence": advice["confidence"], "position_weight": view["weight"],
            "cost_basis": float(pos.cost_price), "reference_close": reference,
            "reference_volume": volume,
            "rule_version": RULE_VERSION, "code_commit": commit, "stat_version": STAT_VERSION,
            "data_cutoff": view["data_date"],
            "ai_text_assisted": bool(advice.get("ai_note")),
            "technical_features": view["indicators"],
        })
    return records


def append_immutable(existing: list[dict], incoming: list[dict]) -> list[dict]:
    result = list(existing)
    ids = {row["advice_id"]: row for row in result}
    for row in incoming:
        previous = ids.get(row["advice_id"])
        if previous is not None:
            # A changed retry must not send a report that disagrees with the saved plan.
            if previous != row:
                raise ValueError(f"建议 {row['advice_id']} 已保存且内容变化，拒绝覆盖与推送")
            continue
        result.append(row)
        ids[row["advice_id"]] = row
    return result


def evaluate(record: dict, frame: pd.DataFrame, cutoff: dt.date) -> dict:
    """Count only completed sessions strictly after plan date; same-day overlap is ambiguous."""
    base = record.get("reference_close")
    result = {"advice_id": record["advice_id"], "rule_version": record["rule_version"],
              "stat_version": STAT_VERSION, "execution": "unknown", "windows": {}}
    if not base or base <= 0 or frame is None or frame.empty:
        result["windows"] = {str(n): {"status": "pending", "sessions": 0} for n in WINDOWS}
        return result
    rows = frame.copy()
    rows["日期"] = pd.to_datetime(rows["日期"]).dt.date
    rows = rows[(rows["日期"] > dt.date.fromisoformat(record["as_of"])) &
                (rows["日期"] <= cutoff)].sort_values("日期").drop_duplicates("日期")
    for col in ("最高", "最低", "收盘"):
        rows[col] = pd.to_numeric(rows[col], errors="coerce")
    rows = rows.dropna(subset=["最高", "最低", "收盘"])
    target, invalid = record.get("target_price"), record.get("invalidation_price")
    for horizon in WINDOWS:
        sample = rows.head(horizon)
        if len(sample) < horizon:
            result["windows"][str(horizon)] = {"status": "pending", "sessions": len(sample)}
            continue
        first = "neither"
        for _, day in sample.iterrows():
            hit_target = target is not None and day["最高"] >= target
            hit_invalid = invalid is not None and day["最低"] <= invalid
            if hit_target or hit_invalid:
                first = "same_day_ambiguous" if hit_target and hit_invalid else (
                    "target_first" if hit_target else "invalidation_first")
                break
        result["windows"][str(horizon)] = {
            "status": first, "sessions": horizon,
            "end_return": float(sample.iloc[-1]["收盘"] / base - 1),
            "max_favorable": float(sample["最高"].max() / base - 1),
            "max_adverse": float(sample["最低"].min() / base - 1),
            "end_date": sample.iloc[-1]["日期"].isoformat(),
        }
    return result


def correlate_execution(record: dict, executions: list[dict]) -> dict:
    """Only an explicit advice ID establishes attribution."""
    matches = [x for x in executions if x.get("related_plan_id") == record["advice_id"]]
    if not matches:
        return {"status": "unknown", "trade_count": 0, "realized_pnl": None,
                "realized_return": None}
    sells = [x for x in matches if x.get("side") == "SELL" and x.get("realized_pnl") is not None]
    pnl = sum(float(x["realized_pnl"]) for x in sells)
    proceeds = sum(float(x["quantity"]) * float(x["price"]) - float(x.get("fee") or 0)
                   for x in sells)
    invested = proceeds - pnl
    return {"status": "linked", "trade_count": len(matches),
            "realized_pnl": pnl if sells else None,
            "realized_return": pnl / invested if sells and invested > 0 else None}


def summarize(records: list[dict], outcomes: list[dict], version: str | None = None) -> dict:
    selected = [r for r in records if version is None or r["rule_version"] == version]
    by_id = {o["advice_id"]: o for o in outcomes}
    settled = [(r, by_id[r["advice_id"]]) for r in selected
               if r["advice_id"] in by_id and
               by_id[r["advice_id"]]["windows"].get("20", {}).get("status") in
               ("target_first", "invalidation_first", "same_day_ambiguous", "neither")]
    def aggregate(items):
        buckets = [o["windows"]["20"] for _, o in items]
        def mean(values):
            return sum(values) / len(values) if values else None
        return {"samples": len(items),
                "target_first": sum(w["status"] == "target_first" for w in buckets),
                "invalidation_first": sum(w["status"] == "invalidation_first" for w in buckets),
                "same_day_ambiguous": sum(w["status"] == "same_day_ambiguous" for w in buckets),
                "neither": sum(w["status"] == "neither" for w in buckets),
                "mean_return": {str(n): mean([o["windows"][str(n)]["end_return"]
                                              for _, o in items]) for n in WINDOWS},
                "mean_max_favorable": mean([w["max_favorable"] for w in buckets]),
                "mean_max_adverse": mean([w["max_adverse"] for w in buckets])}
    groups = defaultdict(list)
    for row in settled:
        groups[row[0]["action_tendency"]].append(row)
    linked = [by_id[r["advice_id"]]["execution"] for r in selected
              if r["advice_id"] in by_id and
              isinstance(by_id[r["advice_id"]].get("execution"), dict) and
              by_id[r["advice_id"]]["execution"]["status"] == "linked"]
    realized = [x for x in linked if x["realized_return"] is not None]
    return {"stat_version": STAT_VERSION, "rule_version": version,
            "sample_count": len(selected), "settled_count": len(settled),
            "insufficient_sample": len(settled) < MIN_SAMPLE,
            "overall": aggregate(settled),
            "by_action": {key: aggregate(value) for key, value in sorted(groups.items())},
            "execution": {"linked_advice_count": len(linked),
                          "realized_sample_count": len(realized),
                          "total_realized_pnl": sum(x["realized_pnl"] for x in realized),
                          "mean_realized_return": (sum(x["realized_return"] for x in realized) / len(realized)
                                                   if realized else None)}}


def render_summary(summary: dict) -> str:
    overall = summary["overall"]
    lines = ["# 建议效果追踪", "", f"统计版本：{summary['stat_version']}；规则版本：{summary['rule_version'] or '全部（分别核对版本）'}",
             f"样本数：{summary['sample_count']}；已结算20日样本：{summary['settled_count']}"]
    if summary["insufficient_sample"]:
        lines.append("样本不足，仅列事实计数，不据此判断策略有效性。")
    lines.append(f"20日观察窗口（已结算样本）：目标先触及 {overall['target_first']}；失效先触及 {overall['invalidation_first']}；同日均触及（顺序不明）{overall['same_day_ambiguous']}；均未触及 {overall['neither']}")
    for n in WINDOWS:
        value = overall["mean_return"][str(n)]
        lines.append(f"平均{n}日收益：{value:.2%}" if value is not None else f"平均{n}日收益：样本不足")
    for key, label in (("mean_max_favorable", "平均最大有利波动"), ("mean_max_adverse", "平均最大不利波动")):
        value = overall[key]
        lines.append(f"{label}：{value:.2%}" if value is not None else f"{label}：样本不足")
    lines.append("按操作倾向：")
    for action, group in summary["by_action"].items():
        lines.append(f"- {action}：20日已结算 {group['samples']} 条；20日窗口目标先触及 {group['target_first']}；20日窗口失效先触及 {group['invalidation_first']}；10日平均收益 {group['mean_return']['10']:.2%}")
    execution = summary["execution"]
    lines.append(f"实际执行：明确关联 {execution['linked_advice_count']} 条建议；已实现样本 {execution['realized_sample_count']}；已实现盈亏 {execution['total_realized_pnl']:+.2f}。")
    lines.append("未明确关联 advice_id 的成交标记 unknown，不计入实际执行收益。")
    return "\n".join(lines) + "\n"
