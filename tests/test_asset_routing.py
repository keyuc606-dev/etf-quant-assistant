import datetime as dt
import json
from types import SimpleNamespace

import pandas as pd

from quant_assistant.advice_performance import make_records
from quant_assistant.analysis.account_advice import build_rule_advice
from quant_assistant.analysis.discipline import build_discipline
from quant_assistant.analysis.indicators import add_all_indicators
from quant_assistant.asset_routing import (BOND_ETF, EQUITY_ETF, GOLD_ETF, QDII_ETF,
                                           classify_asset_subtype)
from quant_assistant.data.fetcher import CN_TZ
from quant_assistant.models import Market, StockPosition
from quant_assistant.portfolio.account_report import generate_account_reports
from quant_assistant.portfolio.holdings import PortfolioManager
from quant_assistant.trading.service import TradingService
from quant_assistant.v3 import build_intraday, classify_quote


MORNING = dt.datetime(2026, 9, 22, 9, 20, tzinfo=CN_TZ)


def market_frame(price=100.0, hot=False):
    dates = pd.bdate_range(end="2026-09-21", periods=80)
    closes = [price + index * .02 for index in range(80)]
    if hot:
        closes[-8:] = [price + 2 + index * 1.2 for index in range(8)]
    raw = pd.DataFrame({
        "日期": dates,
        "开盘": closes,
        "收盘": closes,
        "最高": [value + .2 for value in closes],
        "最低": [value - .2 for value in closes],
        "成交量": [100000] * len(closes),
        "成交额": [10000000] * len(closes),
    })
    return add_all_indicators(raw)


def position(code, name, subtype="", cost=100, price=101):
    return StockPosition(code=code, name=name, market=Market.ETF, shares=1000,
                         cost_price=cost, current_price=price, asset_type="ETF",
                         asset_subtype=subtype)


def test_automatic_subtype_classification_regression_set():
    assert classify_asset_subtype("159649", "国开债ETF", Market.ETF, "ETF") == BOND_ETF
    assert classify_asset_subtype("159934", "黄金ETF易方达", Market.ETF, "ETF") == GOLD_ETF
    assert classify_asset_subtype("518600", "上海金ETF", Market.ETF, "ETF") == GOLD_ETF
    assert classify_asset_subtype("159866", "日经ETF工银", Market.ETF, "ETF") == QDII_ETF
    assert classify_asset_subtype("513010", "恒生科技ETF", Market.ETF, "ETF") == QDII_ETF
    assert classify_asset_subtype("561550", "中证500增强ETF", Market.ETF, "ETF") == EQUITY_ETF


def test_first_buy_persists_asset_subtype(tmp_path):
    portfolio = {"cash": 10000, "cash_flows": [], "positions": []}
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps(portfolio), encoding="utf-8")
    service = TradingService(tmp_path / "ledger.sqlite3", path)
    service.initialize_from_portfolio()
    result = service.record_trade(
        "BUY", "159934", 100, 5.0,
        instrument={"name": "黄金ETF易方达", "market": "ETF", "asset_type": "ETF"},
    )
    saved = json.loads(path.read_text(encoding="utf-8"))["positions"][0]
    assert result.execution["asset_subtype"] == GOLD_ETF
    assert saved["asset_subtype"] == GOLD_ETF
    assert PortfolioManager(path).positions[0].asset_subtype == GOLD_ETF


def test_159649_morning_routes_away_from_stock_pressure_and_rsi(tmp_path):
    pm = PortfolioManager(tmp_path / "portfolio.json")
    pm.cash = 350000
    bond = position("159649", "国开债ETF", cost=95, price=110)
    pm.positions = [bond]
    reports = generate_account_reports(
        pm, {bond.code: market_frame(100, hot=True)}, report_dir=tmp_path,
        generated_at=MORNING,
    )
    advice = reports["advices"][0]
    discipline = reports["disciplines"][bond.code]
    daily = reports["daily"].read_text(encoding="utf-8")
    assert advice["asset_subtype"] == BOND_ETF
    assert advice["reduce_range"] is None and advice["target"] is None
    assert advice["action"] != "REDUCE"
    assert discipline["discipline_state"] == "资产类别专用纪律"
    assert "分批锁利" not in daily and "接近压力" not in daily
    assert "不按股票超买信号机械减仓" in daily
    assert "利率环境、久期和折溢价未纳入" in daily


def test_gold_qdii_and_equity_use_distinct_morning_routes(tmp_path):
    pm = PortfolioManager(tmp_path / "portfolio.json")
    pm.cash = 500000
    positions = [
        position("159934", "黄金ETF易方达"),
        position("159866", "日经ETF工银"),
        position("561550", "中证500增强ETF"),
    ]
    pm.positions = positions
    reports = generate_account_reports(
        pm, {item.code: market_frame(100) for item in positions},
        report_dir=tmp_path, generated_at=MORNING,
    )
    by_code = {item["code"]: item for item in reports["advices"]}
    assert by_code["159934"]["asset_subtype"] == GOLD_ETF
    assert "宏观/商品" in "".join(by_code["159934"]["reasons"])
    assert by_code["159866"]["asset_subtype"] == QDII_ETF
    assert by_code["159866"]["confidence"] == "低"
    assert "境外市场" in "".join(by_code["159866"]["reasons"])
    assert by_code["561550"]["asset_subtype"] == EQUITY_ETF
    assert "权益ETF" in by_code["561550"]["routing_note"]


def test_bond_intraday_never_enters_buy_reduce_or_t_ranking():
    advice = {
        "as_of": "2026-09-22", "code": "159649", "asset_subtype": BOND_ETF,
        "buy_zone": [99, 100], "reduce_zone": [101, 102],
        "invalidation_price": 98, "target_price": 102,
        "technical_features": {"ATR": .2}, "reference_volume": 100000,
        "discipline": {"asset_subtype": BOND_ETF},
    }
    quote = {"price": 101.5, "change_pct": .1, "volume": 90000, "amount": 9000000,
             "as_of": "2026-09-22T14:30:00+08:00", "provisional": True,
             "source": "test"}
    verdict = classify_quote(advice, quote, advice["reference_volume"], BOND_ETF)
    assert verdict["status"] == "防守资产盘中正常"
    assert "压力位/买卖区" in verdict["distance"]
    pos = position("159649", "国开债ETF")
    fetcher = SimpleNamespace(fetch_intraday_quote=lambda *args: quote)
    text = build_intraday(SimpleNamespace(positions=[pos]), [advice], fetcher,
                          dt.datetime(2026, 9, 22, 14, 30, tzinfo=CN_TZ))
    assert "债券ETF不展示股票式买入区/减仓区/压力位" in text
    assert "不做T" in text
    assert "已进入减仓区" not in text and "接近买入区" not in text


def test_qdii_intraday_degrades_market_timing_and_volume():
    advice = {"asset_subtype": QDII_ETF, "buy_zone": [95, 99],
              "reduce_zone": [106, 109], "technical_features": {"ATR": 2}}
    verdict = classify_quote(advice, {"price": 100, "change_pct": 1, "volume": 1000},
                             800, QDII_ETF)
    assert "A股盘中成交量逻辑不适用于QDII" in verdict["volume_note"]
    assert any("境外市场开闭市" in item for item in verdict["flags"])


def test_advice_history_persists_subtype_and_routing_version():
    pos = position("159649", "国开债ETF")
    view = {"position": pos, "available": True, "weight": .22,
            "data_date": "2026-09-21", "indicators": {"ATR": .02}}
    advice = build_rule_advice(view, market_frame(100))
    discipline = build_discipline(view, market_frame(100), advice, "现金正常")
    report = {"advices": [advice], "views": [view], "stock_data": {},
              "disciplines": {pos.code: discipline}}
    row = make_records(report, MORNING, "test")[0]
    assert row["asset_subtype"] == BOND_ETF
    assert row["rule_version"] == "asset-routing-v1"
    assert row["discipline"]["asset_subtype"] == BOND_ETF
