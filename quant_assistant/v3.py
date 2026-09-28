"""Cloud orchestration for morning advice and provisional afternoon checks."""

import datetime as dt
import math
import os
from pathlib import Path

from .advice_performance import (RULE_VERSION, append_immutable, correlate_execution,
                                 evaluate, make_records, render_summary, summarize)
from .cloud_state import CloudStateStore
from .config import REPORT_DIR
from .analysis.discipline import sale_chase_alert, short_note, t_opportunity
from .analysis.ma_discipline import (compact_ma_note, intraday_ma_discipline,
                                     intraday_priority_adjustment)
from .analysis.final_decision import build_intraday_final_decision, economics_text
from .asset_routing import BOND_ETF, QDII_ETF, subtype_for
from .data.fetcher import CN_TZ, DataFetcher
from .models import Market
from .portfolio.holdings import PortfolioManager
from .trading.service import TradingService


EXECUTION_SESSION = "10:30_EXECUTION"
_LEVEL_ZH = {"HIGH": "高", "MEDIUM": "中", "LOW": "低"}
_ACTION_PRIORITY = {"RISK_EXIT": 0, "REDUCE": 1, "ADD": 2,
                    "MANUAL_REVIEW": 3, "HOLD": 4, "NO_ACTION": 5}
_PUBLIC_ACTIONS = {"ADD", "REDUCE", "RISK_EXIT", "MANUAL_REVIEW"}


def cloud_available() -> bool:
    return bool(os.getenv("ACCOUNT_STATE_TOKEN") and os.getenv("ACCOUNT_STATE_REPO"))


def _completed_cutoff(now: dt.datetime) -> dt.date:
    local = now.astimezone(CN_TZ)
    return local.date() if local.time() >= dt.time(15, 30) else local.date() - dt.timedelta(days=1)


def recalculate(records: list[dict], now: dt.datetime, fetcher=None,
                executions: list[dict] | None = None) -> tuple[list[dict], dict]:
    fetcher = fetcher or DataFetcher()
    cutoff = _completed_cutoff(now)
    frames = {}
    outcomes = []
    earliest = {}
    for row in records:
        key = (row["code"], row["market"])
        date = dt.date.fromisoformat(row["as_of"])
        earliest[key] = min(earliest.get(key, date), date)
    for row in records:
        key = (row["code"], row["market"])
        if key not in frames:
            age = max(90, (cutoff - earliest[key]).days + 40)
            frames[key] = fetcher.fetch_hist(row["code"], Market[row["market"]], days=age)
        outcome = evaluate(row, frames[key], cutoff)
        outcome["execution"] = correlate_execution(row, executions or [])
        outcomes.append(outcome)
    return outcomes, summarize(records, outcomes, RULE_VERSION)


def save_morning_advice(reports: dict, now: dt.datetime | None = None,
                        store=None, fetcher=None,
                        advice_session: str = "09:20_LEGACY") -> dict | None:
    if store is None and not cloud_available():
        return None
    now = now or dt.datetime.now(CN_TZ)
    store = store or CloudStateStore()
    state, sha = store.load(os.getenv("ACCOUNT_SNAPSHOT_B64", ""))
    incoming = make_records(reports, now, os.getenv("GITHUB_SHA", "local"), advice_session)
    existing = state.get("advice_records", [])
    combined = append_immutable(existing, incoming)
    if len(combined) != len(existing):
        state["advice_records"] = combined
        store.save(state, sha, f"Record {advice_session} advice {now.astimezone(CN_TZ).date()}")
    persisted, _ = store.load(os.getenv("ACCOUNT_SNAPSHOT_B64", ""))
    persisted_by_id = {row["advice_id"]: row for row in persisted.get("advice_records", [])}
    if any(persisted_by_id.get(row["advice_id"]) != row for row in incoming):
        raise RuntimeError("建议审计记录写入后回读不一致，Telegram 未发送")
    print("建议审计：私有状态仓库回读验证成功（未输出账户明细）")
    outcomes, summary = recalculate(combined, now, fetcher,
                                    TradingService().repository.list_executions())
    detail = Path(reports["detail"])
    with detail.open("a", encoding="utf-8") as handle:
        handle.write("\n" + render_summary(summary))
    if summary["settled_count"]:
        stats = summary["overall"]
        value = stats["mean_return"]["10"]
        mean = f"{value:.2%}" if value is not None else "样本不足"
        line = (f"建议效果追踪：20日已结算 {summary['settled_count']}；"
                f"目标先触及 {stats['target_first']} / 失效先触及 {stats['invalidation_first']}；"
                f"10日平均收益 {mean}。")
        if summary["insufficient_sample"]:
            line += " 样本不足，不判断效果。"
    else:
        line = "建议效果追踪：尚无已结算20日样本。"
    with Path(reports["daily"]).open("a", encoding="utf-8") as handle:
        handle.write("\n" + line + "\n")
    if reports.get("telegram"):
        path = Path(reports["telegram"])
        compact = path.read_text(encoding="utf-8")
        compact = compact.replace("建议效果追踪：尚无已结算20日样本。", line)
        path.write_text(compact, encoding="utf-8")
    return summary


def _zone(value, zone) -> bool:
    return bool(zone and zone[0] <= value <= zone[1])


def _positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _market_progress(stamp):
    """A-share continuous auction minutes elapsed, excluding lunch."""
    try:
        parsed = dt.datetime.fromisoformat(str(stamp))
        if parsed.tzinfo is None:
            return None
        time = parsed.astimezone(CN_TZ).time()
    except (TypeError, ValueError):
        return None
    minutes = time.hour * 60 + time.minute
    if minutes < 570 or minutes > 900:
        return None
    elapsed = min(max(minutes - 570, 0), 120) + min(max(minutes - 780, 0), 120)
    return elapsed / 240 if elapsed > 0 else None


def _volume_note(quote, avg_volume):
    volume = _positive(quote.get("volume"))
    baseline = _positive(avg_volume)
    progress = _market_progress(quote.get("as_of"))
    if not volume or not baseline or progress is None or progress < .15:
        return "成交量判断不可用"
    # At 14:45 the full-day comparison avoids amplifying the closing auction.
    ratio = volume / baseline if progress >= .9375 else volume / (baseline * progress)
    return "成交量异常" if ratio >= 1.8 or ratio <= .35 else "成交量正常"


def _distance(price, level):
    return abs(price - level) / level * 100


def _price_text(value):
    return f"{value:.3f}" if value < 10 else f"{value:.2f}"


def _execution_data_quality(base: str, quote: dict | None,
                            now: dt.datetime | None = None) -> str:
    """Combine close-only inputs with a verified provisional quote."""
    if base == "LOW" or not quote or not quote.get("provisional") or not _positive(quote.get("price")):
        return "LOW"
    if now is not None:
        try:
            stamp = dt.datetime.fromisoformat(str(quote.get("as_of")))
            if stamp.tzinfo is None:
                return "LOW"
            local_now = now.astimezone(CN_TZ)
            if (stamp.astimezone(CN_TZ).date() != local_now.date() or
                    abs((local_now - stamp.astimezone(CN_TZ)).total_seconds()) > 900):
                return "LOW"
        except (TypeError, ValueError):
            return "LOW"
    required = ("open", "high", "low", "volume")
    if base == "MEDIUM" or any(quote.get(key) is None for key in required):
        return "MEDIUM"
    return "HIGH"


def _decision_confidence(decision: dict, status: str, ma: dict) -> str:
    action = decision.get("final_action")
    if action == "RISK_EXIT" and status == "已失效":
        return "HIGH" if "MA20有效跌破" in ma.get("ma_state", "") else "MEDIUM"
    if action == "REDUCE" and ("MA20有效跌破" in ma.get("ma_state", "") or
                               ma.get("ma_state") == "persistent_overheat"):
        return "HIGH"
    return decision.get("decision_confidence") or {
        "高": "HIGH", "中": "MEDIUM", "低": "LOW"
    }.get(decision.get("confidence"), "LOW")


def _limit_range(record: dict, quote: dict, decision: dict) -> list[float] | None:
    """Return deterministic limit references; this never creates an order."""
    action = decision.get("final_action")
    if action == "ADD":
        zone = record.get("buy_zone")
        return list(zone) if zone and len(zone) == 2 else None
    if action == "REDUCE" and decision.get("reduction_reason_type") != "RISK_CONTROL":
        zone = record.get("reduce_zone")
        return list(zone) if zone and len(zone) == 2 else None
    if action in ("REDUCE", "RISK_EXIT"):
        price = _positive(quote.get("price"))
        atr = _positive((record.get("technical_features") or {}).get("ATR"))
        if price:
            floor = max(.01, price - .25 * atr) if atr else price
            return [floor, price]
    return None


def _render_order_range(value) -> str:
    if not value:
        return "不设价格单"
    return f"{_price_text(value[0])}–{_price_text(value[1])}"


def build_morning_execution(reports: dict, fetcher, now: dt.datetime,
                            quality: dict | None = None,
                            detail: list[str] | None = None) -> str:
    """Build the 10:30 executable shortlist from fresh provisional quotes."""
    temporary = make_records(reports, now, os.getenv("GITHUB_SHA", "local"), EXECUTION_SESSION)
    records = {row["code"]: row for row in temporary}
    advices = {row["code"]: row for row in reports["advices"]}
    candidates = []
    degraded = []
    for view in reports["views"]:
        pos = view["position"]
        advice = advices[pos.code]
        record = records[pos.code]
        quote = fetcher.fetch_intraday_quote(pos.code, pos.market, now)
        data_quality = _execution_data_quality(advice.get("data_quality", "LOW"), quote, now)
        if data_quality == "LOW":
            degraded.append(pos.code)
            decision = {
                **(record.get("final_decision") or {}),
                "final_action": "MANUAL_REVIEW", "final_action_label": "人工复核",
                "action_reason": "当日新鲜盘中行情或上一完整交易日数据不足，不给强执行动作",
                "suggested_quantity": 0, "suggested_amount": 0.0,
                "action_size": "0股 / ￥0 / 0%", "decision_confidence": "LOW",
                "data_quality": "LOW", "confidence": "低",
                "limit_range": None,
            }
            advice.update(decision)
            continue
        verdict = classify_quote(record, quote, record.get("reference_volume"),
                                 record.get("asset_subtype"))
        ma_quote = {**quote, "session_progress": _market_progress(quote.get("as_of"))}
        ma = intraday_ma_discipline(record, ma_quote, verdict["status"])
        decision = build_intraday_final_decision(pos, record, quote, verdict["status"], ma)
        decision["data_quality"] = data_quality
        decision["decision_confidence"] = _decision_confidence(decision, verdict["status"], ma)
        decision["confidence"] = _LEVEL_ZH[decision["decision_confidence"]]
        decision["limit_range"] = _limit_range(record, quote, decision)
        advice.update(decision)
        # The immutable 10:30 reference is the verified provisional price.  It
        # is stored only in advice history and never written to the daily cache.
        pos.current_price = float(quote["price"])
        if (decision["final_action"] in _PUBLIC_ACTIONS and
                decision["decision_confidence"] in ("HIGH", "MEDIUM")):
            candidates.append((
                _ACTION_PRIORITY[decision["final_action"]], pos.code, pos, advice,
                verdict, ma, quote,
            ))
        if detail is not None:
            detail.append(
                f"### {pos.name} {pos.code}\n{quote.get('as_of', '时间未标注')} "
                f"{quote.get('source', '未知源')} provisional；状态 {verdict['status']}；"
                f"数据 {data_quality}；决策 {decision['decision_confidence']}；"
                f"均线 {compact_ma_note(ma)}。"
            )
    candidates.sort(key=lambda row: (row[0], row[1]))
    selected = candidates[:5]
    if quality is not None:
        quality.update({"fresh_provisional": bool(reports["views"]) and
                         len(degraded) < len(reports["views"]),
                        "degraded": bool(degraded), "action_count": len(selected)})
    day = now.astimezone(CN_TZ).date().isoformat()
    lines = [f"【10:30 早盘执行策略】｜{day}",
             "仅列确定性规则给出的可执行动作；盘中数据为 provisional，不自动下单。"]
    for index, (_, _, pos, advice, _verdict, _ma, _quote) in enumerate(selected, 1):
        decision = advice
        lines.append(f"{index}. {pos.name} {pos.code}")
        lines.append(
            f"结论：{decision['final_action_label']}｜{decision['suggested_quantity']:,}股"
            f"≈￥{decision['suggested_amount']:,.0f}｜决策{_LEVEL_ZH[decision['decision_confidence']]}"
            f"｜数据{_LEVEL_ZH[decision['data_quality']]}"
        )
        stop = advice.get("stop")
        lines.append(f"挂单{_render_order_range(decision.get('limit_range'))}｜"
                     f"失效/取消{_price_text(stop) if stop else decision['cancel_condition']}")
        lines.append(f"原因：{decision['action_reason']}")
        if decision.get("t_economics"):
            lines.append(f"做T经济性：{economics_text(decision['t_economics'])}")
        if decision["final_action"] in ("ADD", "REDUCE"):
            lines.append("未成交：不追价，14:35复核。")
    hidden = len(reports["views"]) - len(selected)
    lines.append(f"其余{hidden}只：无明确早盘动作。")
    if degraded:
        lines.append(f"其中{len(degraded)}只因数据质量低已折叠，不给强执行动作。")
    lines.append("所有建议均需人工确认；系统不会连接券商或代为下单。")
    return "\n".join(lines)


def classify_quote(advice: dict | None, quote: dict, avg_volume: float | None = None,
                   asset_subtype: str | None = None) -> dict:
    price = quote["price"]
    flags = []
    subtype = asset_subtype or (advice or {}).get("asset_subtype")
    opened, previous = _positive(quote.get("open")), _positive(quote.get("previous_close"))
    if opened and previous:
        gap = (opened / previous - 1) * 100
        if abs(gap) >= 3:
            flags.append(f"向{'上' if gap > 0 else '下'}跳空{abs(gap):.1f}%")
    volume_note = ("A股盘中成交量逻辑不适用于QDII，需结合境外市场时段复核"
                   if subtype == QDII_ETF else _volume_note(quote, avg_volume))
    if volume_note == "成交量异常":
        flags.append(volume_note)
    if subtype == BOND_ETF:
        abnormal = abs(quote.get("change_pct") or 0) >= .8 or volume_note == "成交量异常"
        if abs(quote.get("change_pct") or 0) >= .8:
            flags.append("债券ETF异常波动")
        return {
            "status": "异常波动/流动性需复核" if abnormal else "防守资产盘中正常",
            "flags": flags,
            "rule": "仅复核异常波动、流动性、折溢价与防守仓位；不使用股票买卖区",
            "priority": 2 if abnormal else 95,
            "distance": "债券ETF不使用股票压力位/买卖区",
            "distance_atr": math.inf,
            "group": "防守资产异常复核" if abnormal else "防守资产观察",
            "volume_note": volume_note,
        }
    if subtype == QDII_ETF:
        flags.append("境外市场开闭市、汇率与折溢价状态未核验")
    if advice is None:
        if abs(quote.get("change_pct") or 0) >= 5:
            flags.append("异常波动")
        return {"status": "持仓风险快照", "flags": flags, "rule": "无09:20建议",
                "priority": 90, "distance": "关键区间不可用", "distance_atr": math.inf,
                "group": "暂不动作", "volume_note": volume_note}
    stop, target = _positive(advice.get("invalidation_price")), _positive(advice.get("target_price"))
    buy, reduce = advice.get("buy_zone"), advice.get("reduce_zone")
    atr = _positive((advice.get("technical_features") or {}).get("ATR"))
    levels = []
    for label, zone in (("买入区", buy), ("减仓区", reduce)):
        if zone and len(zone) == 2 and _positive(zone[0]) and _positive(zone[1]):
            boundary = zone[0] if price < zone[0] else zone[1] if price > zone[1] else price
            levels.append((abs(price - boundary), label, boundary))
    if stop:
        levels.append((abs(price - stop), "失效位", stop))
    if target:
        levels.append((abs(price - target), "目标位", target))
    nearest = min(levels, default=None)
    distance = (f"距{nearest[1]}{_distance(price, nearest[2]):.2f}%"
                if nearest else "关键区间不可用")
    normalized = nearest[0] / atr if nearest and atr else math.inf
    near = lambda level: bool(atr and abs(price - level) <= .35 * atr)
    if stop and price <= stop:
        status, rule, priority, group = "已失效", "停止按早间买入计划执行", 0, "接近失效/高风险"
    elif stop and price > stop and near(stop):
        status, rule, priority, group = "接近失效位", "人工复核风险", 1, "接近失效/高风险"
    elif target and price >= target:
        status, rule, priority, group = "目标已达", "人工复核减仓纪律", 2, "最接近触发"
    elif _zone(price, reduce):
        status, rule, priority, group = "已进入减仓区", "人工复核分批处理", 3, "最接近触发"
    elif _zone(price, buy):
        status, rule, priority, group = "已进入买入区", "仅人工核对风险", 4, "最接近触发"
    elif buy and price < buy[0]:
        status, rule, priority, group = "跌破买入区但未失效", "勿将跌破视为买入触发", 7, "接近失效/高风险"
    elif reduce and price < reduce[0] and near(reduce[0]):
        status, rule, priority, group = "接近减仓区", "进入减仓区后人工分批处理", 5, "最接近触发"
    elif buy and price > buy[1] and near(buy[1]):
        status, rule, priority, group = "接近买入区", "仅人工观察", 6, "最接近触发"
    elif reduce and price > reduce[1]:
        status, rule, priority, group = "已超过减仓区", "人工复核减仓纪律", 8, "最接近触发"
    else:
        status, rule, priority, group = "暂不动作", "未触发早间价位", 90, "暂不动作"
    if abs(quote.get("change_pct") or 0) >= 5:
        flags.append("异常波动")
    return {"status": status, "flags": flags, "rule": rule, "priority": priority,
            "distance": distance, "distance_atr": normalized, "group": group,
            "volume_note": volume_note}


def build_intraday(pm, records: list[dict], fetcher, now: dt.datetime,
                   quality: dict | None = None,
                   executions: list[dict] | None = None,
                   detail: list[str] | None = None) -> str:
    day = now.astimezone(CN_TZ).date().isoformat()
    morning = {r["code"]: r for r in records if r["as_of"] == day}
    rows = []
    degraded = []
    for pos in pm.positions:
        quote = fetcher.fetch_intraday_quote(pos.code, pos.market, now)
        if quote is None or not quote.get("provisional"):
            degraded.append(f"{pos.name}（{pos.code}）：实时行情不可用")
            continue
        advice = morning.get(pos.code)
        subtype = (advice or {}).get("asset_subtype") or subtype_for(pos)
        verdict = classify_quote(advice, quote,
                                 advice.get("reference_volume") if advice else None,
                                 subtype)
        if advice:
            ma_quote = {**quote, "session_progress": _market_progress(quote.get("as_of"))}
            ma_result = intraday_ma_discipline(advice, ma_quote, verdict["status"])
            verdict["ma_discipline"] = ma_result
            verdict["priority"] = intraday_priority_adjustment(verdict, ma_result)
        else:
            ma_result = {}
        verdict["final_decision"] = build_intraday_final_decision(
            pos, advice, quote, verdict["status"], ma_result,
        )
        rows.append((verdict["priority"], verdict["distance_atr"], pos.code,
                     pos, quote, advice, verdict, subtype))
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    if quality is not None:
        quality.update({"morning_advice": bool(morning), "fresh_provisional": bool(rows),
                        "degraded": bool(degraded)})
    lines = [f"14:30 盘中风险/执行检查｜{day} 北京时间", "盘中数据均为 provisional，仅供人工复核；不生成正式交易清单。"]
    if not morning:
        lines.append("今日09:20建议不存在，以下仅为持仓盘中风险快照。")
    focus_count = min(5, max(3, sum(row[0] < 90 for row in rows)))
    groups = sorted({row[-2]["group"] for row in rows[:focus_count]},
                    key=lambda group: min(row[0] for row in rows[:focus_count]
                                          if row[-2]["group"] == group))
    for group in groups:
        selected = [row for row in rows[:focus_count] if row[-2]["group"] == group]
        if not selected:
            continue
        lines.append(f"【{group}】")
        for _, _, _, pos, quote, advice, verdict, subtype in selected:
            buy = advice.get("buy_zone") if advice else None
            reduce = advice.get("reduce_zone") if advice else None
            stop = _positive(advice.get("invalidation_price")) if advice else None
            zone = ("债券ETF不展示股票式买入区/减仓区/压力位" if subtype == BOND_ETF else
                    f"买{_price_text(buy[0])}–{_price_text(buy[1])}｜"
                    f"减{_price_text(reduce[0])}–{_price_text(reduce[1])}｜失效{_price_text(stop)}"
                    if buy and reduce and stop else "早间关键区间不完整")
            discipline = advice.get("discipline") if advice else None
            ma_result = verdict.get("ma_discipline") or {}
            final = verdict["final_decision"]
            forbidden = (discipline or {}).get("forbidden_action", "")
            conflict = verdict["status"] in ("已进入买入区", "接近买入区") and any(
                word in forbidden for word in ("禁止越跌越补", "禁止补仓", "禁止加仓", "禁止情绪化追回"))
            conflict = conflict or (verdict["status"] in ("已进入买入区", "接近买入区")
                                    and ma_result.get("ma_conflict_flag", False))
            if subtype == BOND_ETF:
                reminder = "债券专用纪律：不按股票超买/压力位机械减仓，不做T"
            elif conflict:
                reminder = f"信号冲突，人工复核（{ma_result.get('ma_state', '均线纪律')}）"
            elif (verdict["status"] in ("已失效", "接近失效位")
                  and "MA20" in ma_result.get("ma_state", "")):
                reminder = "接近失效 + MA20趋势破坏：禁止加仓，优先减仓复核"
            elif verdict["status"] == "已失效":
                reminder = "停止早间买入计划，人工复核风险"
            elif (verdict["status"] in ("目标已达", "已进入减仓区", "已超过减仓区")
                  and ma_result.get("ma_state") == "persistent_overheat"):
                reminder = "目标已达 + 持续性过热：分批锁利复核"
            elif (verdict["status"] in ("目标已达", "已进入减仓区", "已超过减仓区")
                  and ma_result.get("ma_state") == "single_day_momentum_burst"):
                reminder = ("目标已达 + 单日强势脉冲：" +
                            ("减仓复核" if "减仓复核" in ma_result.get("ma_action_hint", "")
                             else "观察，不因单日乖离机械减仓"))
            elif (verdict["status"] == "跌破买入区但未失效"
                  and ma_result.get("ma_state") == "MA10跌破"):
                reminder = "跌破买入区 + MA10失守：风险收缩/减仓观察"
            elif discipline and sale_chase_alert(pos.code, executions, now, quote["price"],
                    (advice.get("technical_features") or {}).get("ATR")):
                reminder = "卖出后禁止情绪化追回"
            elif executions is None and (discipline or {}).get("discipline_state") == "卖飞/减仓后续涨":
                reminder = short_note(discipline)
            else:
                reminder = verdict["rule"]
            change = quote.get("change_pct") or 0
            lines.append(
                f"{pos.name} {pos.code} {_price_text(quote['price'])}｜"
                f"最终结论：{final['final_action_label']}｜{final['action_size']}｜置信{final['confidence']}"
            )
            lines.append(f"原因：{final['action_reason']}")
            lines.append(zone)
            gaps = [flag for flag in verdict["flags"] if "跳空" in flag]
            lines.append(f"原始状态：{verdict['status']}｜{verdict['distance']}｜纪律：{reminder}｜"
                         f"均线：{compact_ma_note(ma_result) if ma_result else '不可用'}" +
                         ("｜" + "、".join(gaps) if gaps else ""))
            if final.get("t_economics"):
                lines.append(f"做T经济性：{economics_text(final['t_economics'])}")
            lines.append(f"取消条件：{final['cancel_condition']}")
            if detail is not None:
                atr = (advice.get("technical_features") or {}).get("ATR") if advice else None
                t_note = (t_opportunity({"buy_range": buy, "reduce_range": reduce,
                                         "asset_type": advice.get("asset_type"),
                                         "asset_subtype": subtype}, atr, quote)
                          if advice else "不建议做T：无早间建议")
                detail.append(f"### {pos.name} {pos.code}\n报价 {quote.get('as_of', '时间未标注')}（{quote.get('source', '未知源')}）；"
                              f"成交量 {quote.get('volume', 0):,.0f} 手；成交额 ￥{quote.get('amount', 0):,.0f}\n"
                              f"{zone}；{verdict['distance']}；{verdict['volume_note']}；{t_note}；"
                              f"均线纪律：{compact_ma_note(ma_result) if ma_result else '不可用'}；"
                              f"{', '.join(verdict['flags']) or '无额外风险标记'}。")
    if len(rows) > focus_count:
        others = rows[focus_count:]
        lines.append("其余：" + "；".join(f"{pos.name}{pos.code} {verdict['status']}" for _, _, _, pos, _, _, verdict, _ in others))
        if detail is not None:
            for _, _, _, pos, quote, advice, verdict, subtype in others:
                ma_result = verdict.get("ma_discipline") or {}
                atr = (advice.get("technical_features") or {}).get("ATR") if advice else None
                t_note = (t_opportunity({"buy_range": advice.get("buy_zone"),
                                         "reduce_range": advice.get("reduce_zone"),
                                         "asset_type": advice.get("asset_type"),
                                         "asset_subtype": subtype}, atr, quote)
                          if advice else "不建议做T：无早间建议")
                detail.append(f"### {pos.name} {pos.code}\n报价 {quote.get('as_of', '时间未标注')}"
                              f"（{quote.get('source', '未知源')}）；成交量 {quote.get('volume', 0):,.0f} 手；"
                              f"成交额 ￥{quote.get('amount', 0):,.0f}\n{verdict['status']}；"
                              f"{verdict['distance']}；{verdict['volume_note']}；{t_note}；"
                              f"均线纪律：{compact_ma_note(ma_result) if ma_result else '不可用'}。")
    if degraded:
        lines.append("实时数据不可用，已降级；未用昨收冒充盘中价：" + "；".join(degraded))
    if not rows:
        lines.append("无可验证的盘中报价；本次不提供价格判断。")
    lines.append("不自动下单。")
    return "\n".join(lines)


def build_closing_execution(pm, records: list[dict], fetcher, now: dt.datetime,
                            quality: dict | None = None,
                            executions: list[dict] | None = None,
                            detail: list[str] | None = None) -> str:
    """Re-check the immutable 10:30 plan without assuming order execution."""
    day = now.astimezone(CN_TZ).date().isoformat()
    morning = {row["code"]: row for row in records
               if row.get("as_of") == day and row.get("advice_session") == EXECUTION_SESSION}
    candidates = []
    degraded = []
    fresh_count = 0
    for pos in pm.positions:
        quote = fetcher.fetch_intraday_quote(pos.code, pos.market, now)
        record = morning.get(pos.code)
        if not quote or not quote.get("provisional"):
            degraded.append(pos.code)
            continue
        fresh_count += 1
        if record is None:
            if detail is not None:
                detail.append(f"### {pos.name} {pos.code}\n无10:30执行建议基准；仅保留风险快照。")
            continue
        data_quality = _execution_data_quality(record.get("data_quality", "LOW"), quote, now)
        verdict = classify_quote(record, quote, record.get("reference_volume"),
                                 record.get("asset_subtype"))
        ma_quote = {**quote, "session_progress": _market_progress(quote.get("as_of"))}
        ma = intraday_ma_discipline(record, ma_quote, verdict["status"])
        decision = build_intraday_final_decision(pos, record, quote, verdict["status"], ma)
        decision["data_quality"] = data_quality
        decision["decision_confidence"] = _decision_confidence(decision, verdict["status"], ma)
        if data_quality == "LOW":
            decision["decision_confidence"] = "LOW"
        decision["confidence"] = _LEVEL_ZH[decision["decision_confidence"]]
        decision["limit_range"] = _limit_range(record, quote, decision)
        original = (record.get("final_decision") or {}).get("final_action", "NO_ACTION")
        current = decision["final_action"]
        if original in _PUBLIC_ACTIONS and current not in _PUBLIC_ACTIONS:
            change = "CANCEL"
        elif original in _PUBLIC_ACTIONS and current == original:
            change = "CONTINUE"
        elif original in _PUBLIC_ACTIONS:
            change = "REVISE"
        elif current in _PUBLIC_ACTIONS:
            change = "NEW"
        else:
            change = "UNCHANGED"
        eligible = (data_quality != "LOW" and
                    (decision["decision_confidence"] in ("HIGH", "MEDIUM") or change == "CANCEL") and
                    (current in _PUBLIC_ACTIONS or change == "CANCEL"))
        if eligible:
            priority = _ACTION_PRIORITY.get(current, 5)
            candidates.append((priority, pos.code, pos, record, decision, change, verdict, ma))
        if detail is not None:
            detail.append(
                f"### {pos.name} {pos.code}\n状态 {verdict['status']}；早盘 {original}；"
                f"尾盘 {current}；变更 {change}；数据 {data_quality}；"
                f"决策 {decision['decision_confidence']}；均线 {compact_ma_note(ma)}。"
            )
    candidates.sort(key=lambda row: (row[0], row[1]))
    selected = candidates[:5]
    if quality is not None:
        quality.update({"morning_advice": bool(morning), "fresh_provisional": fresh_count > 0,
                        "degraded": bool(degraded), "action_count": len(selected)})
    lines = [f"【14:35 尾盘执行策略】｜{day}",
             "仅复核10:30计划的继续、取消或修正；盘中数据为 provisional，不自动下单。"]
    if not morning:
        lines.append("今日无10:30执行建议基准：本次仅为风险快照，不伪造早盘计划。")
    for index, (_, _, pos, record, decision, change, _verdict, _ma) in enumerate(selected, 1):
        original = (record.get("final_decision") or {}).get("final_action", "NO_ACTION")
        label = "取消原挂单/不再追价" if change == "CANCEL" else decision["final_action_label"]
        lines.append(f"{index}. {pos.name} {pos.code}")
        lines.append(
            f"结论：{label}｜{decision['suggested_quantity']:,}股≈￥{decision['suggested_amount']:,.0f}"
            f"｜决策{_LEVEL_ZH[decision['decision_confidence']]}｜数据{_LEVEL_ZH[decision['data_quality']]}"
        )
        if change != "CANCEL":
            lines.append(f"挂单{_render_order_range(decision.get('limit_range'))}｜"
                         f"取消条件：{decision['cancel_condition']}")
        lines.append(f"原因：{decision['action_reason']}")
        if decision.get("t_economics"):
            lines.append(f"做T经济性：{economics_text(decision['t_economics'])}")
        if original in _PUBLIC_ACTIONS:
            pending = ("取消原挂单，不再追价。" if change == "CANCEL" else
                       "仅按本次更新后的区间人工复核，区间外不追价。")
            lines.append(f"若早盘订单未成交：{pending}")
            lines.append("若已成交：请以已录入成交为准；系统不假设订单已执行。")
    hidden = len(pm.positions) - len(selected)
    lines.append(f"其余{hidden}只：无明确尾盘动作，维持原计划/不操作。")
    if degraded:
        lines.append(f"其中{len(degraded)}只实时数据不可用，已折叠且未用昨收冒充。")
    lines.append("所有建议均需人工确认；系统不会连接券商或代为下单。")
    return "\n".join(lines)


def notify_morning_execution(reports: dict, now: dt.datetime | None = None,
                             store=None, fetcher=None, notifier=None) -> Path:
    """Persist the 10:30 baseline, verify it, then send the compact execution card."""
    from .notifications.telegram import TelegramNotifier, split_telegram_message
    now = now or dt.datetime.now(CN_TZ)
    fetcher = fetcher or DataFetcher()
    quality = {}
    detail = []
    text = build_morning_execution(reports, fetcher, now, quality, detail)
    if not quality.get("fresh_provisional"):
        raise RuntimeError("没有可验证的当日新鲜 provisional 行情，10:30执行策略不推送")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "my-portfolio-morning-execution.md"
    path.write_text(text + "\n", encoding="utf-8")
    detail_path = REPORT_DIR / "my-portfolio-morning-execution-detail.md"
    detail_path.write_text("# 10:30早盘详细数据（provisional，仅人工参考）\n\n" +
                           "\n\n".join(detail) + "\n", encoding="utf-8")
    reports["telegram"] = path
    save_morning_advice(reports, now, store=store, fetcher=fetcher,
                        advice_session=EXECUTION_SESSION)
    notifier = notifier or TelegramNotifier(fetcher=fetcher)
    if not notifier.bot_token or not notifier.chat_id:
        raise RuntimeError("缺少 Telegram 凭据")
    for part in split_telegram_message(path.read_text(encoding="utf-8")):
        fetcher.send_telegram_message(notifier.bot_token, notifier.chat_id, part)
    fetcher.send_telegram_document(notifier.bot_token, notifier.chat_id, detail_path)
    print("10:30数据校验：新鲜provisional报价=True；"
          f"存在降级={quality['degraded']}；主屏动作={quality['action_count']}")
    return path


def notify_intraday(now: dt.datetime | None = None, store=None, fetcher=None,
                    notifier=None) -> Path:
    from .notifications.telegram import TelegramNotifier, split_telegram_message
    now = now or dt.datetime.now(CN_TZ)
    store = store or CloudStateStore()
    state, _ = store.load(os.getenv("ACCOUNT_SNAPSHOT_B64", ""))
    fetcher = fetcher or DataFetcher()
    quality = {}
    detail = []
    text = build_closing_execution(PortfolioManager(), state.get("advice_records", []), fetcher,
                                   now, quality,
                                   TradingService().repository.list_executions(), detail)
    print("尾盘数据校验：10:30建议={morning_advice}；新鲜provisional报价={fresh_provisional}；存在降级={degraded}".format(**quality))
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "my-portfolio-intraday.md"
    path.write_text(text + "\n", encoding="utf-8")
    detail_path = REPORT_DIR / "my-portfolio-intraday-detail.md"
    detail_path.write_text("# 14:35尾盘详细数据（provisional，仅人工参考）\n\n" + "\n\n".join(detail) + "\n", encoding="utf-8")
    notifier = notifier or TelegramNotifier(fetcher=fetcher)
    if not notifier.bot_token or not notifier.chat_id:
        raise RuntimeError("缺少 Telegram 凭据")
    for part in split_telegram_message(text):
        fetcher.send_telegram_message(notifier.bot_token, notifier.chat_id, part)
    fetcher.send_telegram_document(notifier.bot_token, notifier.chat_id, detail_path)
    return path
