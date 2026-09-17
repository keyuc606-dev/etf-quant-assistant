"""Cloud orchestration for morning advice and provisional afternoon checks."""

import datetime as dt
import os
from pathlib import Path

from .advice_performance import (RULE_VERSION, append_immutable, correlate_execution,
                                 evaluate, make_records, render_summary, summarize)
from .cloud_state import CloudStateStore
from .config import REPORT_DIR
from .data.fetcher import CN_TZ, DataFetcher
from .models import Market
from .portfolio.holdings import PortfolioManager
from .trading.service import TradingService


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
                        store=None, fetcher=None) -> dict | None:
    if store is None and not cloud_available():
        return None
    now = now or dt.datetime.now(CN_TZ)
    store = store or CloudStateStore()
    state, sha = store.load(os.getenv("ACCOUNT_SNAPSHOT_B64", ""))
    incoming = make_records(reports, now, os.getenv("GITHUB_SHA", "local"))
    existing = state.get("advice_records", [])
    combined = append_immutable(existing, incoming)
    if len(combined) != len(existing):
        state["advice_records"] = combined
        store.save(state, sha, f"Record morning advice {now.astimezone(CN_TZ).date()}")
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
        line = (f"过去20日观察窗口已结算建议：目标先触及 {stats['target_first']}/{summary['settled_count']}，"
                f"10日平均收益 {value:.2%}。")
    else:
        line = "建议效果追踪：尚无已结算20日样本。"
    with Path(reports["daily"]).open("a", encoding="utf-8") as handle:
        handle.write("\n" + line + "\n")
    return summary


def _zone(value, zone) -> bool:
    return bool(zone and zone[0] <= value <= zone[1])


def classify_quote(advice: dict | None, quote: dict, avg_volume: float | None = None) -> dict:
    price = quote["price"]
    flags = []
    if advice is None:
        if abs(quote.get("change_pct") or 0) >= 5:
            flags.append("异常波动")
        if quote.get("open", 0) > 0 and quote.get("previous_close", 0) > 0:
            if abs(quote["open"] / quote["previous_close"] - 1) >= .03:
                flags.append("大幅跳空")
        return {"status": "持仓风险快照", "flags": flags, "rule": "无09:20建议，仅检查盘中风险"}
    stop, target = advice.get("invalidation_price"), advice.get("target_price")
    if stop is not None and price <= stop:
        status, rule = "失效", f"已跌破失效位 {stop:.3f}，停止按早间买入计划执行"
    elif target is not None and price >= target:
        status, rule = "目标已达", f"已达目标位 {target:.3f}，复核减仓纪律"
    elif _zone(price, advice.get("reduce_zone")):
        status, rule = "接近减仓区", "已进入早间减仓区，人工复核"
    elif quote.get("change_pct", 0) >= 5:
        status, rule = "今日不追高", "日内涨幅较大，暂停追价"
    elif _zone(price, advice.get("buy_zone")):
        status, rule = "进入买入区", "已进入早间买入区，仍需人工核对风险"
    else:
        status, rule = "等待", "未触发早间价位"
    if abs(quote.get("change_pct") or 0) >= 5:
        flags.append("异常波动")
    if quote.get("open", 0) > 0 and quote.get("previous_close", 0) > 0:
        if abs(quote["open"] / quote["previous_close"] - 1) >= .03:
            flags.append("大幅跳空")
    if avg_volume and quote.get("volume", 0) > avg_volume * 1.8:
        flags.append("成交量异常")
    return {"status": status, "flags": flags, "rule": rule}


def build_intraday(pm, records: list[dict], fetcher, now: dt.datetime,
                   quality: dict | None = None) -> str:
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
        verdict = classify_quote(advice, quote, advice.get("reference_volume") if advice else None)
        priority = {"失效": 0, "目标已达": 1, "接近减仓区": 2,
                    "进入买入区": 3, "今日不追高": 4, "持仓风险快照": 5, "等待": 6}
        rows.append((priority[verdict["status"]], pos.code, pos, quote, advice, verdict))
    rows.sort(key=lambda item: (item[0], item[1]))
    if quality is not None:
        quality.update({"morning_advice": bool(morning), "fresh_provisional": bool(rows),
                        "degraded": bool(degraded)})
    lines = [f"14:30 盘中风险/执行检查｜{day} 北京时间", "盘中数据均为 provisional，仅供人工复核；不生成正式交易清单。"]
    if not morning:
        lines.append("今日09:20建议不存在，以下仅为持仓盘中风险快照。")
    for _, _, pos, quote, advice, verdict in rows[:5]:
        buy = advice.get("buy_zone") if advice else None
        reduce = advice.get("reduce_zone") if advice else None
        zone = (f"买入区 {buy[0]:.3f}–{buy[1]:.3f}；减仓区 {reduce[0]:.3f}–{reduce[1]:.3f}"
                if buy and reduce else "早间区间不可用")
        lines.append(f"{pos.name} {pos.code}｜{quote['price']:.3f} ({quote['change_pct']:+.2f}%)｜{verdict['status']}")
        lines.append(f"  成交量 {quote['volume']:,.0f} 手；成交额 ￥{quote['amount']:,.0f}；报价 {quote.get('as_of', '时间未标注')}（{quote.get('source', '数据源未知')}）")
        lines.append(f"  {zone}；{verdict['rule']}。" + ("；" + "、".join(verdict["flags"]) if verdict["flags"] else ""))
    if degraded:
        lines.append("实时数据不可用，已降级；未用昨收冒充盘中价：" + "；".join(degraded))
    if not rows:
        lines.append("无可验证的盘中报价；本次不提供价格判断。")
    lines.append("不自动下单。")
    return "\n".join(lines)


def notify_intraday(now: dt.datetime | None = None, store=None, fetcher=None,
                    notifier=None) -> Path:
    from .notifications.telegram import TelegramNotifier, split_telegram_message
    now = now or dt.datetime.now(CN_TZ)
    store = store or CloudStateStore()
    state, _ = store.load(os.getenv("ACCOUNT_SNAPSHOT_B64", ""))
    fetcher = fetcher or DataFetcher()
    quality = {}
    text = build_intraday(PortfolioManager(), state.get("advice_records", []), fetcher,
                          now, quality)
    print("盘中数据校验：当日建议={morning_advice}；新鲜provisional报价={fresh_provisional}；存在降级={degraded}".format(**quality))
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / "my-portfolio-intraday.md"
    path.write_text(text + "\n", encoding="utf-8")
    notifier = notifier or TelegramNotifier(fetcher=fetcher)
    if not notifier.bot_token or not notifier.chat_id:
        raise RuntimeError("缺少 Telegram 凭据")
    for part in split_telegram_message(text):
        fetcher.send_telegram_message(notifier.bot_token, notifier.chat_id, part)
    return path
