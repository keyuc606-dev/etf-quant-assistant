"""Offline intraday confirmation; a single snapshot never proves persistence."""
import datetime as dt

from ..config import MORNING_CONFIRMATION_PARAMS as CFG

FIELDS = (
    "morning_confirmation_version", "time_of_day_confidence", "rebound_from_low_pct",
    "rebound_protected", "weakness_persistence", "relative_deteriorating",
    "confirmation_state", "morning_reduce_candidate", "conditional_quantity",
    "confirmation_level", "cancel_level", "signal_price", "signal_as_of",
)


def context(record, quote, evidence, now, fresh, price, low, atr, support, ma20):
    local = now.astimezone(dt.timezone(dt.timedelta(hours=8))) if now else None
    clock = local.time() if local else None
    confidence = ("HIGH" if clock and dt.time(13, 30) <= clock <= dt.time(15) else
                  "MEDIUM" if clock and dt.time(10) <= clock < dt.time(11) else "LOW")
    rebound = price / low - 1 if fresh and price and low and 0 < low <= price else None
    threshold = min(CFG["rebound_max"], max(CFG["rebound_min"],
                    CFG["rebound_atr"] * atr / price if atr and price else CFG["rebound_min"]))
    protected = bool(rebound is not None and (rebound >= threshold or evidence.get("reclaim_confirmed")))
    # Only a previous verified observation from the same day can prove elapsed weakness.
    previous = record.get("morning_observation") or {}
    persistence, deteriorating = "UNKNOWN", False
    try:
        stamp = dt.datetime.fromisoformat(previous["signal_as_of"])
        elapsed = (now - stamp).total_seconds() / 60
        valid = bool(fresh and stamp.tzinfo and local and
                     stamp.astimezone(local.tzinfo).date() == local.date() and
                     CFG["weakness_minutes"] <= elapsed <= CFG["max_observation_minutes"])
        old_relative, relative = previous.get("relative_return_day"), evidence.get("relative_return_day")
        deteriorating = bool(valid and old_relative is not None and relative is not None and
                             relative <= old_relative - CFG["relative_deterioration"])
        if valid:
            persistence = "CONFIRMED" if (previous.get("trend_break_candidate") and
                evidence.get("trend_break_candidate") and previous.get("support_break") and
                evidence.get("support_break") and deteriorating and not protected) else "NOT_CONFIRMED"
    except (KeyError, TypeError, ValueError):
        pass
    levels = [value for value in (support, ma20) if value and value > 0]
    return dict(morning_confirmation_version=CFG["version"], time_of_day_confidence=confidence,
                rebound_from_low_pct=rebound, rebound_protected=protected,
                weakness_persistence=persistence, relative_deteriorating=deteriorating,
                confirmation_state="NOT_REQUIRED", morning_reduce_candidate=False,
                conditional_quantity=0, confirmation_level=min(levels) if levels else None,
                cancel_level=max(levels) if levels else None,
                signal_price=price if fresh else None, signal_as_of=quote.get("as_of") if fresh else None)


def apply(decision, evidence, record):
    """Preserve exit and ADD gates; defer ordinary morning reductions."""
    result = {key: evidence.get(key) for key in FIELDS}
    action = decision["final_action"]
    previous = record.get("morning_reduce_candidate") or (record.get("final_decision") or {}).get("morning_reduce_candidate")
    if action == "RISK_EXIT":
        result["confirmation_state"] = "RISK_EXIT_IMMEDIATE"
    elif evidence.get("time_of_day_confidence") in ("LOW", "MEDIUM") and action == "REDUCE":
        result.update(morning_reduce_candidate=True, conditional_quantity=decision["suggested_quantity"])
        confirmed = (evidence.get("weakness_persistence") == "CONFIRMED" and
                     evidence.get("support_break") and evidence.get("relative_deteriorating") and
                     not evidence.get("rebound_protected"))
        if confirmed:
            result["confirmation_state"] = "CONFIRMED"
            decision.update(confidence="中", decision_confidence="MEDIUM")
        else:
            protected = evidence.get("rebound_protected")
            result["confirmation_state"] = "DEFERRED_REBOUND" if protected else "WAIT_CONFIRMATION"
            level, cancel = evidence.get("confirmation_level"), evidence.get("cancel_level")
            unit = "股" if record.get("asset_subtype") == "STOCK" else "份"
            trigger = (f"11:00后仍低于{level:.4g}且继续弱于基准，再复核减{result['conditional_quantity']}{unit}；"
                       f"站回{cancel:.4g}取消；无法核验则等14:10，不追价" if level and cancel else
                       "确认价位或持续弱势数据不足，等14:10复核，不立即减仓")
            decision.update(final_action="HOLD", final_action_label="先不减", confidence="中",
                            decision_confidence="MEDIUM", suggested_quantity=0, suggested_amount=0.0,
                            suggested_fraction=0.0, action_size="0股 / ￥0 / 0%",
                            action_reason=("早盘下跌后已有反抽，先等弱势确认" if protected else
                                           "早盘偏弱，但持续走弱尚未确认"),
                            trigger_condition=trigger, cancel_condition="收复关键位则取消；14:10复核")
    elif evidence.get("time_of_day_confidence") == "HIGH" and previous and action != "ADD":
        result["confirmation_state"] = "CONFIRMED" if action == "REDUCE" else "CANCELLED_BY_REBOUND" if (
            evidence.get("reclaim_confirmed") or not evidence.get("trend_break_candidate")) else "UNCONFIRMED"
        if result["confirmation_state"] == "CANCELLED_BY_REBOUND":
            decision.update(final_action="HOLD", final_action_label="继续持有", suggested_quantity=0,
                            suggested_amount=0.0, suggested_fraction=0.0, action_size="0股 / ￥0 / 0%",
                            action_reason="早盘弱势已修复，取消减仓候选", confidence="中", decision_confidence="MEDIUM")
    return {**decision, **result}
