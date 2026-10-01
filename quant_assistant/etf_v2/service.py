"""V2 应用编排：复用 DataFetcher、PortfolioManager 和现有 Telegram 适配器。"""

from __future__ import annotations

import datetime
import os
from pathlib import Path

from ..config import DATA_DIR, ETF_V2_PARAMS, ETF_V2_UNIVERSE, REPORT_DIR
from ..data.fetcher import DataFetcher
from ..models import Market
from ..notifications.telegram import TelegramNotifier, split_telegram_message
from ..portfolio.holdings import PortfolioManager
from .engine import allocate_new_cash, scan_candidates
from .paper import (load_local, mark_to_market, new_paper_state, paper_positions,
                    queue_allocation, save_local, settle_pending)
from .report import render_compact, render_detail, write_reports


PAPER_PATH = DATA_DIR / "etf_v2_paper.json"


def _holding_weights(pm: PortfolioManager) -> dict:
    total = pm.total_market_value
    return {p.code: p.market_value / total for p in pm.positions} if total > 0 else {}


def _latest_as_of(market_data: dict) -> str:
    dates = []
    for df in market_data.values():
        if df is not None and not df.empty and "日期" in df:
            dates.append(df["日期"].max().date())
    return max(dates).isoformat() if dates else datetime.date.today().isoformat()


def load_market_data(pm: PortfolioManager, fetcher: DataFetcher | None = None,
                     params: dict | None = None) -> tuple[dict, dict, list[str]]:
    params = {**ETF_V2_PARAMS, **(params or {})}
    fetcher = fetcher or DataFetcher()
    market_data, holdings_data, warnings = {}, {}, []
    for code in ETF_V2_UNIVERSE:
        df = fetcher.fetch_hist(code, Market.ETF, days=params["lookback_days"])
        if df is None or df.empty:
            warnings.append(f"{code} 行情不可用")
        else:
            market_data[code] = df
    for pos in pm.positions:
        if pos.code in market_data:
            holdings_data[pos.code] = market_data[pos.code]
            continue
        df = fetcher.fetch_hist(pos.code, pos.market, days=params["lookback_days"])
        if df is not None and not df.empty:
            holdings_data[pos.code] = df
    return market_data, holdings_data, warnings


def _load_paper() -> tuple[dict, object | None, str | None]:
    if os.getenv("ACCOUNT_STATE_TOKEN") and os.getenv("ACCOUNT_STATE_REPO"):
        from ..cloud_state import CloudStateStore
        store = CloudStateStore()
        cloud, sha = store.load(os.getenv("ACCOUNT_SNAPSHOT_B64", ""))
        return cloud.get("etf_v2_paper") or new_paper_state(), (store, cloud), sha
    return load_local(PAPER_PATH), None, None


def _save_paper(state: dict, cloud_context, sha) -> None:
    if cloud_context is None:
        save_local(PAPER_PATH, state)
        return
    store, cloud = cloud_context
    cloud["etf_v2_paper"] = state
    store.save(cloud, sha, "Update ETF V2 paper account")


def build_daily(pm: PortfolioManager | None = None, investable_cash: float | None = None,
                fetcher: DataFetcher | None = None, persist_paper: bool = True) -> dict:
    pm = pm or PortfolioManager()
    market_data, holdings_data, warnings = load_market_data(pm, fetcher)
    scan = scan_candidates(market_data, holdings_data, _holding_weights(pm),
                           as_of_date=datetime.date.today())
    cash = pm.cash if investable_cash is None else max(0.0, float(investable_cash))
    allocation = allocate_new_cash(scan, pm.positions, cash, portfolio_cash=max(pm.cash, cash))
    paper, cloud_context, sha = _load_paper()
    settle_pending(paper, market_data)
    paper_scan = scan_candidates(market_data, as_of_date=datetime.date.today())
    paper_allocation = allocate_new_cash(paper_scan, paper_positions(paper), paper.get("cash", 0.0))
    as_of = _latest_as_of(market_data)
    snapshot = mark_to_market(paper, market_data, as_of)
    queue_allocation(paper, paper_allocation, as_of)
    if persist_paper:
        _save_paper(paper, cloud_context, sha)
    compact = render_compact(scan, allocation, len(pm.positions), snapshot)
    detail = render_detail(scan, allocation, paper)
    compact_path, detail_path = write_reports(REPORT_DIR, compact, detail)
    return {"scan": scan, "allocation": allocation, "paper": paper, "paper_snapshot": snapshot,
            "compact": compact_path, "detail": detail_path, "warnings": warnings}


def notify_daily(result: dict, notifier: TelegramNotifier | None = None,
                 fetcher: DataFetcher | None = None) -> None:
    fetcher = fetcher or DataFetcher()
    notifier = notifier or TelegramNotifier(fetcher=fetcher)
    if not notifier.bot_token or not notifier.chat_id:
        raise RuntimeError("缺少 Telegram 凭据")
    text = Path(result["compact"]).read_text(encoding="utf-8")
    for part in split_telegram_message(text):
        fetcher.send_telegram_message(notifier.bot_token, notifier.chat_id, part)
    fetcher.send_telegram_document(notifier.bot_token, notifier.chat_id, result["detail"])
