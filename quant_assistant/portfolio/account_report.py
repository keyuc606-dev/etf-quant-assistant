"""真实账户日报呈现层：只消费既有持仓、指标和风控结果。"""

import datetime
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd

from ..analysis.indicators import add_all_indicators, get_signals
from ..analysis.account_advice import build_rule_advices
from ..analysis.discipline import build_discipline, cash_defense, short_note
from ..analysis.ma_discipline import build_ma_discipline, compact_ma_note
from ..analysis.final_decision import build_final_decision, economics_text
from ..asset_routing import (BOND_ETF, COMMODITY_ETF, GOLD_ETF, QDII_ETF,
                             role_label, subtype_for, subtype_label)
from ..config import ETF_POOL, REPORT_DIR
from ..data.fetcher import CN_TZ, cached_trade_dates


MAX_FOCUS_POSITIONS = 5


def _completed_day(moment: datetime.datetime) -> datetime.date:
    local = _china_time(moment)
    day = local.date() if local.time() >= datetime.time(16) else local.date() - datetime.timedelta(days=1)
    calendar = cached_trade_dates()
    for _ in range(40):
        is_session = day.isoformat() in calendar if calendar is not None else day.weekday() < 5
        if is_session:
            return day
        day -= datetime.timedelta(days=1)
    return day


def _china_time(moment: datetime.datetime) -> datetime.datetime:
    return moment.replace(tzinfo=CN_TZ) if moment.tzinfo is None else moment.astimezone(CN_TZ)


def _integrity_text(views: List[dict], generated_at: datetime.datetime) -> str:
    cutoff = _completed_day(generated_at).isoformat()
    dates = [item["data_date"] for item in views]
    if dates and all(date == cutoff for date in dates):
        if any(item["indicators"].get("量比") == 0 for item in views):
            return "降级：完整日K存在零成交量信号，需核验数据源"
        return "已验证截至上一完整交易日"
    return "降级：部分标的缺少上一完整交易日日K，相关判断需人工复核"


def _advice_conflict(advice: dict, discipline: dict) -> bool:
    if advice.get("action") != "ADD_SMALL":
        return False
    forbidden = discipline.get("forbidden_action", "")
    return any(word in forbidden for word in ("摊平", "越跌越补", "禁止自动回补", "禁止情绪化追回"))


def _compact_price(value) -> str:
    if value is None:
        return "-"
    value = float(value)
    digits = 2 if value >= 20 else 3 if value >= 1 else 4
    return f"{value:.{digits}f}"


def _compact_range(value) -> str:
    return "-" if value is None else f"{_compact_price(value[0])}–{_compact_price(value[1])}"


def _render_telegram(pm, views: List[dict], news_result: dict,
                     generated_at: datetime.datetime, advice_by_code: dict,
                     disciplines: dict, ma_disciplines: dict,
                     degraded_sources: List[str]) -> str:
    total_cost = sum(item["position"].cost_value for item in views if item["position"].current_price > 0)
    pnl = pm.total_market_value - total_cost
    cash_ratio = pm.cash / pm.total_assets if pm.total_assets else 0
    risk = sorted(views, key=lambda item: (
        -(30 if not item["available"] else 0)
        -(20 if item["pnl_pct"] is not None and item["pnl_pct"] <= -.30 else 10 if item["pnl_pct"] is not None and item["pnl_pct"] <= -.15 else 0)
        -(10 if item["atr_pct"] is not None and item["atr_pct"] >= .03 else 0)
        -(5 if _asset_type(item["position"]) == "STOCK" else 0), item["position"].code))[:3]
    selected = {item["position"].code for item in risk}
    weight = sorted((item for item in views if item["position"].code not in selected),
                    key=lambda item: (-item["weight"], item["position"].code))[:2]
    if len(risk) + len(weight) < 3:
        weight = sorted((item for item in views if item["position"].code not in selected),
                        key=lambda item: (-item["weight"], item["position"].code))[:3-len(risk)]
    max_item = max(views, key=lambda item: item["weight"], default=None)
    high_losses = sum(item["pnl_pct"] is not None and item["pnl_pct"] <= -.15 for item in views)
    overlap = _overlap_groups(views)
    status = "偏低" if cash_ratio < .15 else "中性偏低" if cash_ratio < .25 else "充足"
    lines = ["股票 + ETF 账户晨报", f"报告日期：{_china_time(generated_at).date().isoformat()}",
             f"总资产 ￥{pm.total_assets:,.0f}｜现金 {cash_ratio:.1%}｜浮盈亏 {_money_signed(pnl)}",
             f"持仓 {len(views)} 只｜风险 {_risk_level(views)}",
             f"技术行情截止：{_market_as_of(views)} 收盘｜新闻截止：{_news_as_of(news_result)}",
             f"技术指标数据完整性：{_integrity_text(views, generated_at)}"]
    if degraded_sources:
        lines.append(f"行情源 warning/degraded：备用源接管 {len(degraded_sources)} 只")
    if news_result.get("degraded"):
        lines.append("新闻不完整；综合置信度已降级")

    def add_items(title, items):
        if not items:
            return
        lines.append(title)
        for item in items:
            pos = item["position"]
            advice = advice_by_code[pos.code]
            subtype = item["asset_subtype"]
            lines.append(
                f"{pos.name}({pos.code})｜最终结论：{advice['final_action_label']}｜"
                f"{advice['action_size']}｜置信{advice['confidence']}"
            )
            if subtype == BOND_ETF:
                lines.append("  关键区间：债券ETF不使用股票式买/减/失效位")
            else:
                lines.append(f"  关键区间：买 {_compact_range(advice['buy_range'])}｜减 {_compact_range(advice['reduce_range'])}｜失效 {_compact_price(advice['stop'])}")
            lines.append(f"  原因：{advice['action_reason']}")
            lines.append(f"  触发：{advice['trigger_condition']}")
            lines.append(f"  取消：{advice['cancel_condition']}")
            if advice.get("t_economics"):
                lines.append(f"  做T经济性：{economics_text(advice['t_economics'])}")
            if advice.get("conflict_note") != "无":
                lines.append(f"  冲突，人工复核：{advice['conflict_note']}")
            lines.append(f"  均线纪律：{compact_ma_note(ma_disciplines[pos.code])}（仅状态说明）")

    add_items("风险重点", risk)
    add_items("仓位重点", weight)
    lines.extend(["风险摘要", f"最大单项：{max_item['position'].name} {max_item['weight']:.1%}" if max_item else "最大单项：无",
                  f"主题重叠：{len(overlap)} 组｜高亏损持仓：{high_losses} 只｜现金：{status}",
                  "建议效果追踪：尚无已结算20日样本。",
                  "原量化策略基准：独立保留，不改写真实账户建议。",
                  "详细新闻、信号、纪律和全部持仓见详细报告。"])
    return "\n".join(lines) + "\n"


def _plain_signal(signal: str) -> str:
    replacements = {
        "MA5上穿MA20，金叉买入信号": "MA5上穿MA20，短期趋势改善",
        "MA5下穿MA20，死叉卖出信号": "MA5下穿MA20，短期趋势偏弱",
        "MACD金叉，买入信号": "MACD向上交叉，动能改善",
        "MACD死叉，卖出信号": "MACD向下交叉，动能偏弱",
        "KDJ金叉，买入信号": "KDJ向上交叉，短线动能改善",
        "KDJ死叉，卖出信号": "KDJ向下交叉，短线动能转弱",
    }
    return replacements.get(signal, signal.replace("买入信号", "偏强信号").replace("卖出信号", "偏弱信号"))


def _position_views(pm, stock_data: Dict[str, object],
                    theme_observations: Optional[Dict[str, dict]] = None) -> List[dict]:
    theme_observations = theme_observations or {}
    total_assets = pm.total_assets
    views = []
    for pos in pm.positions:
        df = stock_data.get(pos.code)
        available = df is not None and not df.empty and pos.current_price > 0
        raw_signals = get_signals(df) if available else []
        signals = [_plain_signal(item) for item in raw_signals]
        subtype = subtype_for(pos)
        if subtype == BOND_ETF:
            signals = []
        elif subtype in (GOLD_ETF, COMMODITY_ETF, QDII_ETF):
            signals = [signal for signal in signals
                       if not any(token in signal for token in ("RSI", "布林带", "KDJ", "量比"))]
        atr_pct = None
        data_date = None
        indicator_values = {}
        if available:
            last = df.iloc[-1]
            data_date = _date_text(last.get("日期"))
            for key in ("MA5", "MA10", "MA20", "MA60", "DIF", "DEA", "MACD",
                        "RSI14", "BOLL_UP", "BOLL_MID", "BOLL_DN", "K", "D", "J",
                        "ATR", "ATR_PCT", "量比", "距涨停", "距跌停"):
                value = last.get(key)
                indicator_values[key] = None if value is None or pd.isna(value) else float(value)
            atr_pct = indicator_values.get("ATR_PCT")
        weight = pos.market_value / total_assets if total_assets > 0 and pos.current_price > 0 else 0.0
        pnl_pct = pos.profit_loss_pct if pos.current_price > 0 else None
        loss_score = 18 if pnl_pct is not None and pnl_pct <= -0.30 else (
            10 if pnl_pct is not None and pnl_pct <= -0.15 else 0
        )
        volatility_score = 8 if atr_pct is not None and atr_pct >= 0.03 else 0
        multi_score = 6 if len(signals) >= 2 else 0
        unavailable_score = 40 if not available else 0
        observation = theme_observations.get(pos.code, {})
        # 新闻只提供有限的关注度加分，封顶 8 分，不能凭数量压倒账户风险与技术因素。
        news_cap = 8.0 if subtype == "STOCK" else 4.0 if subtype == "EQUITY_ETF" else 0.0
        news_score = min(float(observation.get("event_strength", 0) or 0), news_cap)
        score = (weight * 100 + loss_score + len(signals) * 5 + volatility_score
                 + multi_score + unavailable_score + news_score)
        obvious = (
            not available or weight >= 0.10 or loss_score > 0 or volatility_score > 0
            or len(signals) >= 2 or news_score > 0
            or any(token in signal for signal in signals for token in ("MA5", "MACD", "布林带", "RSI="))
        )
        views.append({
            "position": pos, "available": available, "weight": weight,
            "pnl_pct": pnl_pct, "signals": signals, "raw_signals": raw_signals,
            "atr_pct": atr_pct, "data_date": data_date,
            "indicators": indicator_values, "score": score, "obvious": obvious,
            "theme_observation": observation, "news_score": news_score,
            "asset_subtype": subtype,
        })
    return views


def select_focus_positions(views: Iterable[dict], limit: int = MAX_FOCUS_POSITIONS) -> List[dict]:
    candidates = [item for item in views if item["obvious"]]
    return sorted(candidates, key=lambda item: (-item["score"], item["position"].code))[:limit]


def generate_account_reports(pm, stock_data: Optional[Dict[str, object]] = None,
                             alerts: Optional[list] = None,
                             fetch_errors: Optional[List[str]] = None,
                             news_result: Optional[dict] = None,
                             theme_observations: Optional[Dict[str, dict]] = None,
                             report_dir: Optional[Path] = None,
                             generated_at: Optional[datetime.datetime] = None,
                             advice_provider=None, executions=None,
                             degraded_sources: Optional[List[str]] = None) -> dict:
    report_dir = Path(report_dir) if report_dir is not None else REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    stock_data = stock_data or {}
    generated_at = generated_at or datetime.datetime.now(CN_TZ)
    cutoff = _completed_day(generated_at)
    complete_data = {}
    for code, frame in stock_data.items():
        if frame is None or frame.empty:
            continue
        dates = pd.to_datetime(frame["日期"], errors="coerce")
        complete = frame.loc[dates.dt.date <= cutoff].copy()
        if not complete.empty:
            if len(complete) != len(frame):
                if {"收盘", "最高", "最低", "成交量"}.issubset(complete.columns):
                    complete = add_all_indicators(complete)
                pm.update_price(code, float(complete.iloc[-1]["收盘"]))
            complete_data[code] = complete
    stock_data = complete_data
    news_result = news_result or _empty_news_result()
    theme_observations = theme_observations or {}
    views = _position_views(pm, stock_data, theme_observations)
    advices = build_rule_advices(views, stock_data)
    advice_mode = "纯规则"
    if advice_provider is not None:
        advices, advice_mode = advice_provider.enhance(advices)
    degraded_sources = degraded_sources or []
    for advice in advices:
        view = next(item for item in views if item["position"].code == advice["code"])
        subtype = view["asset_subtype"]
        issues = []
        statuses = news_result.get("source_status") or {}
        if (news_result.get("degraded") or news_result.get("is_stale")
                or any(status not in ("ok", "success") for status in statuses.values())):
            issues.append("新闻不完整")
        if advice["code"] in degraded_sources:
            issues.append("主行情源降级")
        if view["data_date"] != cutoff.isoformat():
            issues.append("行情未覆盖上一完整交易日")
        if any(view["indicators"].get(key) is None for key in ("MA20", "ATR", "量比")):
            issues.append("关键指标缺失")
        frame = stock_data.get(advice["code"])
        if frame is not None and "成交量" in frame and pd.to_numeric(frame.iloc[-1]["成交量"], errors="coerce") <= 0:
            issues.append("成交量异常")
        if subtype == BOND_ETF:
            issues.append("利率/久期/折溢价未接入")
        elif subtype == QDII_ETF:
            issues.append("境外开闭市/汇率/折溢价未接入")
        elif subtype in (GOLD_ETF, COMMODITY_ETF):
            issues.append("宏观/商品驱动数据未完整接入")
        if issues:
            advice["confidence"] = "低" if (len(issues) > 1 or not view["available"]
                or view["data_date"] != cutoff.isoformat() or subtype == QDII_ETF) else "中"
        advice["confidence_note"] = "、".join(issues) if issues else "完整日K与关键指标可用"
    advice_by_code = {item["code"]: item for item in advices}
    cash_note = cash_defense(pm, views)
    disciplines = {view["position"].code: build_discipline(
        view, stock_data.get(view["position"].code),
        advice_by_code[view["position"].code], cash_note, executions,
        generated_at) for view in views}
    ma_disciplines = {view["position"].code: build_ma_discipline(
        view, stock_data.get(view["position"].code),
        advice_by_code[view["position"].code], disciplines[view["position"].code],
        cash_note) for view in views}
    for advice in advices:
        ma = ma_disciplines[advice["code"]]
        advice["display_conflict"] = (_advice_conflict(advice, disciplines[advice["code"]])
                                      or ma.get("ma_conflict_flag", False)
                                      or (advice.get("action") == "ADD_SMALL"
                                          and "禁止加仓" in ma.get("ma_forbidden_action", "")))
        if advice["display_conflict"]:
            advice["confidence"] = "低"
            advice["confidence_note"] += "、建议与纪律冲突"
        view = next(item for item in views if item["position"].code == advice["code"])
        decision = build_final_decision(
            view, advice, disciplines[advice["code"]], ma,
            total_assets=pm.total_assets, cash=pm.cash,
        )
        advice.update(decision)
    focus = select_focus_positions(views)
    advice_order = {item["code"]: index for index, item in enumerate(advices)}
    focus.sort(key=lambda item: advice_order.get(item["position"].code, len(advices)))
    daily = _render_daily(pm, views, focus, news_result, generated_at,
                          advice_by_code, advice_mode, disciplines, ma_disciplines)
    detail = _render_detail(
        pm, views, alerts or [], fetch_errors or [], news_result, generated_at,
        advice_by_code, advice_mode, disciplines, ma_disciplines, degraded_sources,
    )
    telegram = _render_telegram(pm, views, news_result, generated_at,
                                advice_by_code, disciplines, ma_disciplines, degraded_sources)
    daily_path = report_dir / "my-portfolio-daily.md"
    detail_path = report_dir / "my-portfolio-detail.md"
    telegram_path = report_dir / "my-portfolio-telegram.txt"
    daily_path.write_text(daily, encoding="utf-8")
    detail_path.write_text(detail, encoding="utf-8")
    telegram_path.write_text(telegram, encoding="utf-8")
    return {"daily": daily_path, "detail": detail_path, "focus_count": len(focus),
            "telegram": telegram_path,
            "advices": advices, "views": views, "stock_data": stock_data,
            "advice_mode": advice_mode, "disciplines": disciplines,
            "ma_disciplines": ma_disciplines,
            "final_decisions": {item["code"]: {
                key: item.get(key) for key in (
                    "final_decision_version", "final_action", "final_action_label",
                    "action_reason", "action_size", "suggested_quantity",
                    "suggested_amount", "suggested_fraction", "trigger_condition",
                    "cancel_condition", "confidence", "conflict_note",
                    "reduction_reason_type", "t_economics",
                    "manual_confirmation_required",
                )
            } for item in advices}}


def _risk_level(views: List[dict]) -> str:
    unavailable = any(not item["available"] for item in views)
    high_losses = sum(1 for item in views if item["pnl_pct"] is not None and item["pnl_pct"] <= -0.15)
    max_weight = max((item["weight"] for item in views), default=0.0)
    if max_weight > 0.30:
        return "高"
    if unavailable or high_losses >= 2 or max_weight > 0.20:
        return "较高（需重点复核）"
    if high_losses or any(item["obvious"] for item in views):
        return "关注"
    return "正常"


def _render_daily(pm, views: List[dict], focus: List[dict], news_result: dict,
                  generated_at: datetime.datetime, advice_by_code: dict,
                  advice_mode: str, disciplines: dict, ma_disciplines: dict) -> str:
    total_cost = sum(item["position"].cost_value for item in views if item["position"].current_price > 0)
    total_pnl = pm.total_market_value - total_cost
    total_pnl_pct = total_pnl / total_cost if total_cost > 0 else 0.0
    cash_ratio = pm.cash / pm.total_assets if pm.total_assets > 0 else 0.0
    focus_codes = {item["position"].code for item in focus}
    other = [item for item in views if item["position"].code not in focus_codes]
    max_weight = max((item["weight"] for item in views), default=0.0)
    unavailable = [item for item in views if not item["available"]]
    high_losses = [item for item in views if item["pnl_pct"] is not None and item["pnl_pct"] <= -0.15]
    extreme = [item for item in views if item["atr_pct"] is not None and item["atr_pct"] >= 0.03]
    overlap = _overlap_groups(views)
    stock_value = sum(item["position"].market_value for item in views
                      if _asset_type(item["position"]) == "STOCK")
    etf_value = sum(item["position"].market_value for item in views
                    if _asset_type(item["position"]) == "ETF")
    stock_count = sum(_asset_type(item["position"]) == "STOCK" for item in views)
    etf_count = len(views) - stock_count

    lines = [
        "# 股票 + ETF 账户日报",
        "",
        f"报告日期：{_china_time(generated_at).date().isoformat()}",
        f"技术行情截止：{_market_as_of(views)} 收盘",
        f"技术指标数据完整性：{_integrity_text(views, generated_at)}",
        f"新闻截止：{_news_as_of(news_result)}",
        "计划口径：前一交易日收盘 + 隔夜新闻的当天作战计划（非实时盘中建议）",
        f"分析模式：{advice_mode}",
        *([f"新闻状态：{_source_status_text(news_result)}"] if news_result.get("degraded") else []),
        "",
        "## 一、账户概览",
        "",
        f"- 总资产：￥{pm.total_assets:,.2f}",
        f"- 股票市值：￥{stock_value:,.2f}",
        f"- ETF 市值：￥{etf_value:,.2f}",
        f"- 现金：￥{pm.cash:,.2f}",
        f"- 现金比例：{cash_ratio:.1%}",
        f"- 整体浮动盈亏：{_money_signed(total_pnl)}（{total_pnl_pct:+.2%}）",
        f"- 持仓数量：{len(pm.positions)} 只（股票 {stock_count}、ETF {etf_count}）",
        f"- 整体风险等级：{_risk_level(views)}",
        "",
        "## 二、今日重点关注",
        "",
    ]
    if not focus:
        lines.extend(["今日没有触发明显异常。", ""])
    for item in focus:
        pos = item["position"]
        advice = advice_by_code[pos.code]
        subtype = item["asset_subtype"]
        if subtype == BOND_ETF:
            lines.extend([
                f"### {pos.name}（{pos.code}）｜防守资产｜仓位 {item['weight']:.1%}",
                "",
                f"- 类型：{subtype_label(subtype)}",
                f"- 状态：{advice.get('asset_status', advice['action_label'])}",
                "- 纪律：不按股票超买信号机械减仓；不使用浅套/深套、卖飞或做T作为主纪律",
                "- 重点：仓位集中度、账户防守资产占比、流动性、折溢价及利率/久期风险",
                "- 趋势用途：价格趋势与ATR仅用于异常监测，不以压力位触发减仓",
                f"- 均线纪律：{compact_ma_note(ma_disciplines[pos.code])}",
                f"- 最终结论：{advice['final_action_label']}；动作大小：{advice['action_size']}；综合置信度：{advice['confidence']}（{advice['confidence_note']}）",
                f"- 触发：{advice['trigger_condition']}；取消：{advice['cancel_condition']}",
                "- 数据降级：利率环境、久期和折溢价未纳入，本条仅做账户防守仓位复核",
                f"- 核心理由：{'；'.join(advice['reasons'][:2])}",
                "",
            ])
            continue
        lines.extend([
            f"### {pos.name}（{pos.code}）",
            "",
            f"- 类型：{subtype_label(subtype)}｜角色：{role_label(subtype)}",
            f"- 仓位：{item['weight']:.2%}" if item["available"] else "- 仓位：行情不可用，暂无法准确计算",
            f"- 浮动盈亏：{item['pnl_pct']:+.1%}" if item["pnl_pct"] is not None else "- 浮动盈亏：不可用",
            f"- 主要信号：{'；'.join(item['signals'][:3]) if item['signals'] else '暂未触发明显技术信号'}",
            f"- 状态：{_state_summary(item)}",
            f"- 近期事件：{_recent_event_text(item['theme_observation'])}" if subtype == "STOCK" else
            f"- 资产提示：{advice.get('routing_note', '按资产类别规则复核')}",
            f"- 主题状态：{item['theme_observation'].get('status', '近期公开信息不足，暂不形成行业判断。')}" if subtype in ("STOCK", "EQUITY_ETF") else
            f"- 资产类别风险：{advice.get('routing_note', '按资产类别规则复核')}",
            f"- 最终结论：{advice['final_action_label']}；动作大小：{advice['action_size']}；综合置信度：{advice['confidence']}（{advice['confidence_note']}）",
            f"- 一句话原因：{advice['action_reason']}",
            f"- 触发：{advice['trigger_condition']}；取消：{advice['cancel_condition']}",
            f"- 冲突说明：{advice['conflict_note']}",
            f"- 参考买入区间：{_price_range(advice['buy_range'])}；减仓区间：{_price_range(advice['reduce_range'])}",
            f"- 止损/失效位：{_price(advice['stop'])}；目标位：{_price(advice['target'])}",
            f"- 交易纪律：{short_note(disciplines[pos.code])}",
            f"- 均线纪律：{compact_ma_note(ma_disciplines[pos.code])}",
            f"- 核心理由：{advice['ai_note'] or '；'.join(advice['reasons'][:2])}",
        ])
        if item["pnl_pct"] is not None and item["pnl_pct"] <= -0.15:
            lines.append("- 提示：历史亏损较大，但不能仅因亏损决定卖出或补仓。")
        lines.append("")

    pressure = "没有明显的单一持仓压力" if max_weight <= 0.20 else "存在单一持仓占比较高的压力"
    lines.extend([
        "## 三、现金状态",
        "",
        f"- 当前现金：￥{pm.cash:,.2f}",
        f"- 现金比例：{cash_ratio:.1%}",
        f"- 仓位压力：{pressure}",
        f"- {cash_defense(pm, views)}",
        "- 当前系统尚没有足够信息判断是否应该使用现金。",
        "",
        "## 四、仓位风险",
        "",
        f"- 单一标的仓位：最大为 {max_weight:.1%}；{'未超过20%预警线' if max_weight <= 0.20 else '存在超过20%的持仓'}。",
        f"- 主题重叠：{_overlap_text(overlap)}",
        f"- 高亏损仓位 / 重点复核：{_names_text(high_losses)}",
        f"- 数据不可用：{_names_text(unavailable, none_text='无')}。",
        f"- 极端技术/波动信号：{_names_text(extreme, none_text='无')}。",
        "",
        "## 五、其他持仓",
        "",
        f"其余 {len(other)} 只持仓今日没有进入前五重点（股票与ETF均继续完成行情、指标和风险检查）。",
        "",
        "| 代码 | 名称 | 类型 | 仓位 | 浮动盈亏 |",
        "|---|---|---|---:|---:|",
    ])
    for item in other:
        pos = item["position"]
        weight = f"{item['weight']:.2%}" if item["available"] else "不可用"
        pnl = f"{item['pnl_pct']:+.1%}" if item["pnl_pct"] is not None else "不可用"
        lines.append(f"| {pos.code} | {pos.name} | {subtype_label(item['asset_subtype'])} | {weight} | {pnl} |")

    weak_names = [item["position"].name for item in focus
                  if any(word in signal for signal in item["signals"] for word in ("偏弱", "下轨", "超卖", "波动"))]
    improving = [item["position"].name for item in focus
                 if any(word in signal for signal in item["signals"] for word in ("改善", "向上"))]
    lines.extend([
        "",
        "## 六、今日摘要",
        "",
        f"- {cash_defense(pm, views)}。",
        f"- 当前{'无' if max_weight <= 0.20 else '有'}单一股票或 ETF 仓位超过 20%。",
        f"- 短期偏弱、超卖或波动较大的重点项：{'、'.join(weak_names) if weak_names else '无'}。",
        f"- 出现短期技术改善信号的重点项：{'、'.join(improving) if improving else '无'}；不能单凭技术交叉判断反转。",
        "- 所有价格区间、失效位、目标位和仓位变化均由确定性规则计算；AI（如启用）只压缩文字与排序。",
        *(["- 当前尚未接入 AI 综合判断，以上为纯规则建议。"]
          if advice_mode == "纯规则" else []),
        "- 若实际成交未录入，系统只能按最后一次确认的已记录持仓分析；不会假定成交已经发生。",
        "",
        "## 原量化策略基准",
        "",
        f"- 原策略池 {len(ETF_POOL)} 只 ETF 独立保留；股票和池外 ETF 不会加入策略。",
        "- 本账户日报不把原策略结果改写成整个真实账户的交易建议。",
        "",
        "## 数据时间",
        "",
        f"- 技术行情截止：{_market_as_of(views)} 收盘",
        f"- 新闻截止：{_news_as_of(news_result)}",
        "",
        "详细数据见 `my-portfolio-detail.md`。",
        "",
    ])
    return "\n".join(lines)


def _render_detail(pm, views: List[dict], alerts: list, fetch_errors: List[str],
                   news_result: dict, generated_at: datetime.datetime,
                   advice_by_code: dict, advice_mode: str, disciplines: dict,
                   ma_disciplines: dict,
                   degraded_sources: List[str]) -> str:
    total_cost = sum(item["position"].cost_value for item in views if item["position"].current_price > 0)
    total_pnl = pm.total_market_value - total_cost
    cash_ratio = pm.cash / pm.total_assets if pm.total_assets > 0 else 0.0
    lines = [
        "# 股票 + ETF 账户详细报告",
        "",
        f"生成日期：{generated_at.isoformat(timespec='minutes')}",
        f"技术行情截止：{_market_as_of(views)} 收盘",
        f"技术指标数据完整性：{_integrity_text(views, generated_at)}",
        f"新闻截止：{_news_as_of(news_result)}",
        "计划口径：前一交易日收盘 + 隔夜新闻的当天作战计划（非实时盘中建议）",
        f"分析模式：{advice_mode}",
        "",
        "## 账户完整数据",
        "",
        f"- 总资产：￥{pm.total_assets:,.2f}",
        f"- 股票市值：￥{sum(item['position'].market_value for item in views if _asset_type(item['position']) == 'STOCK'):,.2f}",
        f"- ETF 市值：￥{sum(item['position'].market_value for item in views if _asset_type(item['position']) == 'ETF'):,.2f}",
        f"- 现金：￥{pm.cash:,.2f}（{cash_ratio:.2%}）",
        f"- 持仓成本：￥{total_cost:,.2f}",
        f"- 整体浮动盈亏：{_money_signed(total_pnl)}",
        "",
        "## 完整持仓",
        "",
        "| 代码 | 名称 | 类型 | 数量 | 成本价 | 当前价 | 市值 | 盈亏 | 仓位 | 数据日期 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in views:
        pos = item["position"]
        current = f"{pos.current_price:.4f}" if item["available"] else "不可用"
        pnl = f"{item['pnl_pct']:+.2%}" if item["pnl_pct"] is not None else "不可用"
        lines.append(
            f"| {pos.code} | {pos.name} | {subtype_label(item['asset_subtype'])} | {int(pos.shares):,} | {pos.cost_price:.4f} | "
            f"{current} | ￥{pos.market_value:,.2f} | {pnl} | {item['weight']:.2%} | "
            f"{item['data_date'] or '-'} |"
        )
    lines.extend([
        "", "## 全部持仓规则建议", "",
        "| 代码 | 最终结论 | 动作大小 | 买入区间 | 减仓区间 | 止损/失效位 | 目标位 | 减仓目的 | 置信度 | 核心理由 |",
        "|---|---|---|---:|---:|---:|---:|---|---|---|",
    ])
    for item in views:
        advice = advice_by_code[item["position"].code]
        lines.append(
            f"| {advice['code']} | {advice['final_action_label']} | {advice['action_size']} | {_price_range(advice['buy_range'])} | "
            f"{_price_range(advice['reduce_range'])} | {_price(advice['stop'])} | {_price(advice['target'])} | "
            f"{advice.get('reduction_reason_type') or '-'} | {advice['confidence']}（{advice['confidence_note']}） | "
            f"{advice['action_reason']} |"
        )
    lines.extend(["", "## 交易纪律实验标签（discipline-v1）", "",
                  "仅供人工复核，不改变原建议、ETF交易清单或效果结算口径。", ""])
    for item in views:
        pos = item["position"]
        d = disciplines[pos.code]
        lines.extend([
            f"### {pos.name}（{pos.code}）", "",
            f"- 状态：{d['discipline_state']}；动作：{d['discipline_action']}",
            f"- 禁止：{d['forbidden_action']}",
            f"- 再评估条件：{d['reentry_condition']}",
            f"- T机会：{d['t_opportunity']}",
            f"- 现金防守：{d['cash_defense_note']}",
            f"- 理由：{d['reason']}；近期卖出证据：{d['sale_status']}", "",
        ])
    lines.extend(["", "## 均线趋势纪律（ma-discipline-v2）", "",
                  "只提供确定性复核提示，不改写早间区间、原建议或交易清单。", ""])
    for item in views:
        pos = item["position"]
        ma = ma_disciplines[pos.code]
        lines.extend([
            f"### {pos.name}（{pos.code}）", "",
            f"- 状态：{ma['ma_state']}；动作提示：{ma['ma_action_hint']}",
            f"- 禁止：{ma['ma_forbidden_action']}",
            f"- 确认：{ma['ma_confirmation']}",
            f"- 冲突：{'是' if ma['ma_conflict_flag'] else '否'}",
            f"- 理由：{ma['ma_reason']}", "",
        ])
    lines.extend([
        "",
        "## 全部技术指标：趋势与动量",
        "",
        "| 代码 | MA5 | MA10 | MA20 | MA60 | DIF | DEA | MACD | RSI14 | K | D | J |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for item in views:
        values = item["indicators"]
        lines.append(
            f"| {item['position'].code} | {_number(values.get('MA5'))} | {_number(values.get('MA10'))} | "
            f"{_number(values.get('MA20'))} | {_number(values.get('MA60'))} | "
            f"{_number(values.get('DIF'), 4)} | {_number(values.get('DEA'), 4)} | "
            f"{_number(values.get('MACD'), 4)} | {_number(values.get('RSI14'), 1)} | "
            f"{_number(values.get('K'), 1)} | {_number(values.get('D'), 1)} | {_number(values.get('J'), 1)} |"
        )
    lines.extend([
        "",
        "## 全部技术指标：波动与状态",
        "",
        "| 代码 | BOLL上轨 | BOLL中轨 | BOLL下轨 | ATR | ATR% | 量比 | 距涨停 | 距跌停 | 状态信号 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for item in views:
        values = item["indicators"]
        lines.append(
            f"| {item['position'].code} | {_number(values.get('BOLL_UP'))} | "
            f"{_number(values.get('BOLL_MID'))} | {_number(values.get('BOLL_DN'))} | "
            f"{_number(values.get('ATR'))} | {_percent(values.get('ATR_PCT'))} | "
            f"{_number(values.get('量比'), 2)} | {_number(values.get('距涨停'), 2)} | "
            f"{_number(values.get('距跌停'), 2)} | "
            f"{'；'.join(item['signals']) if item['signals'] else '无明显离散信号'} |"
        )
    lines.extend([
        "",
        "## 新闻与行业背景",
        "",
        f"- 新闻数据更新时间：{news_result.get('fetched_at') or '无'}",
        f"- 新闻缓存状态：{'陈旧' if news_result.get('is_stale') else '正常'}",
        f"- 获取状态：{_source_status_text(news_result)}",
        "",
        "| 代码 | 主题 | 近期新闻数 | 主要来源 | 最近事件 | 主题观察 |",
        "|---|---|---:|---|---|---|",
    ])
    for item in views:
        observation = item["theme_observation"]
        evidence = observation.get("evidence", [])
        sources = "、".join(sorted({event.get("source", "未知") for event in evidence})) or "无"
        lines.append(
            f"| {item['position'].code} | {observation.get('theme', '未分类')} | "
            f"{observation.get('recent_news_count', 0)} | {sources} | "
            f"{_recent_event_text(observation)} | "
            f"{observation.get('status', '近期公开信息不足，暂不形成行业判断。')} |"
        )
    lines.extend([
        "",
        "### 新闻证据",
        "",
    ])
    if not news_result.get("items"):
        lines.append("- 无可用新闻；报告已按无新闻模式正常降级。")
    else:
        for event in news_result["items"]:
            lines.append(
                f"- [{event['title']}]({event['url']})｜{event['source']}｜"
                f"{_display_timestamp(event['published_at'])}｜匹配标的："
                f"{', '.join(event.get('matched_instruments', event.get('matched_etfs', [])))}｜关键词："
                f"{', '.join(event.get('matched_keywords', []))}"
            )
    lines.extend([
        "",
        "## 风控明细",
        "",
    ])
    if not alerts:
        lines.append("- 当前没有规则告警。")
    for alert in alerts:
        if alert.rule_name == "个股止损线":
            label = "高亏损仓位 / 重点复核"
            message = (
                f"{alert.stock_code} 当前成本盈亏 {alert.current_value:.1%}，"
                f"超过 {abs(alert.threshold):.0%} 重点复核阈值；这不是自动卖出信号"
            )
        else:
            label = alert.rule_name
            message = alert.message.replace("建议立即清仓", "需要人工重点复核")
        lines.append(f"- {label}：{message}")
    lines.extend([
        "",
        "## 行情数据来源与完整性",
        "",
        "- 行情由项目现有 AkShare 数据层获取：A股和ETF均以东方财富为主源，ETF另有新浪免费备用源。",
        f"- 本次不可用项目：{', '.join(fetch_errors) if fetch_errors else '无'}。",
        f"- 主源失败、备用源接管（warning/degraded）：{', '.join(degraded_sources) if degraded_sources else '无'}。",
        "- 不可用行情不会用成本价或虚构价格补齐，也不会参与价格类盈亏判断。",
        "- 新闻按自然时间记录，可能晚于行情截止并包含周末；不会回填成交易日信号，也不会改变规则交易清单。",
        "",
        "## 原策略结果与真实账户边界",
        "",
        f"- 原策略池共有 {len(ETF_POOL)} 只 ETF，只用于 allocation、rebalance 和 backtest。",
        "- 真实账户持仓不会因进入成交台账而自动加入策略池。",
        "- 原策略可能把池外持仓列为“存量迁移”；这是规则模型的对照结果，不是针对账户的自动交易指令。",
        "- 具体原策略周度结果保存在同日 `weekly-YYYY-MM-DD.md` 中。",
        "",
        "## 能力边界",
        "",
        "- 当前新闻与主题观察只基于公开标题元数据和关键词规则，不读取或保存新闻正文。",
        "- 当前没有可靠宏观利率、久期、实时折溢价、汇率、境外市场开闭市状态、基金规模和跟踪误差数据；对应资产明确降级，不作推断。",
        "- 当前没有 OpenAI 或多 Agent 综合判断。",
        "- 当前不会自动下单，也不会根据历史亏损自动给出买卖方向。",
        "- 技术指标只描述历史价格状态，不能证明趋势已经反转或预测未来收益。",
        "",
    ])
    return "\n".join(lines)


def _state_summary(item: dict) -> str:
    if not item["available"]:
        return "公开行情暂不可用，今天无法形成可靠判断。"
    if item.get("asset_subtype") == BOND_ETF:
        return "防守资产仅按仓位、波动与可得流动性数据复核；不使用股票式超买/压力位结论。"
    signals = item["signals"]
    if len(signals) >= 2:
        return "多个技术条件同时触发，值得优先复核，但信号不等于交易结论。"
    if signals:
        return f"{signals[0]}；单一指标不足以决定操作。"
    if item["pnl_pct"] is not None and item["pnl_pct"] <= -0.15:
        return "历史亏损较大，今天没有新的强技术信号，需要结合产品和行业信息复核。"
    return "当前没有突出技术异常，继续观察即可。"


def _overlap_groups(views: List[dict]) -> List[List[str]]:
    groups = defaultdict(list)
    for item in views:
        pos = item["position"]
        if pos.sector:
            groups[pos.sector].append(pos.name)
    return [names for names in groups.values() if len(names) > 1]


def _overlap_text(groups: List[List[str]]) -> str:
    if not groups:
        return "按当前账户分类未发现明确重复主题。"
    return "；".join("、".join(names) for names in groups) + " 存在主题重叠，需留意合并暴露。"


def _names_text(items: List[dict], none_text: str = "无") -> str:
    if not items:
        return none_text
    return "、".join(f"{item['position'].name}({item['position'].code})" for item in items)


def _date_text(value) -> Optional[str]:
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "date"):
        return value.date().isoformat()
    return str(value)


def _number(value, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _price(value) -> str:
    return "不可用" if value is None else f"￥{float(value):.4f}"


def _price_range(value) -> str:
    return "不可用" if value is None else f"￥{value[0]:.4f}–{value[1]:.4f}"


def _percent(value) -> str:
    return "-" if value is None else f"{value:.2%}"


def _money_signed(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}￥{abs(value):,.2f}"


def _recent_event_text(observation: dict) -> str:
    events = observation.get("major_events") or observation.get("evidence") or []
    if not events:
        return "近期没有匹配到可核验的公开事件。"
    event = events[0]
    return (f"[{event['title']}]({event['url']})"
            f"（{event['source']}，{_display_timestamp(event['published_at'])}）")


def _display_timestamp(value: Optional[str]) -> str:
    if not value:
        return "时间未知"
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M 北京时间")
    except ValueError:
        return value


def _market_as_of(views: List[dict]) -> str:
    dates = [item["data_date"] for item in views if item.get("data_date")]
    return max(dates) if dates else "无可用行情"


def _news_as_of(news_result: dict) -> str:
    value = news_result.get("news_as_of")
    suffix = "（陈旧缓存）" if news_result.get("is_stale") else ""
    return f"{_display_timestamp(value)}{suffix}" if value else f"无可用新闻{suffix}"


def _source_status_text(news_result: dict) -> str:
    statuses = news_result.get("source_status", {})
    text = "、".join(f"{source}={status}" for source, status in statuses.items()) or "无"
    if news_result.get("degraded"):
        text += "（已降级）"
    return text


def _empty_news_result() -> dict:
    return {
        "items": [], "fetched_at": None, "news_as_of": None,
        "is_stale": True, "degraded": True,
        "source_status": {"news": "not_loaded"},
    }


def _asset_type(position) -> str:
    return position.asset_type or ("ETF" if position.market.value == "ETF" else "STOCK")
