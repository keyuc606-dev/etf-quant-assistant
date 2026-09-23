import datetime as dt
from types import SimpleNamespace

import pandas as pd

from quant_assistant.advice_performance import make_records
from quant_assistant.analysis.ma_discipline import (
    VERSION, build_ma_discipline, intraday_ma_discipline,
)
from quant_assistant.asset_routing import BOND_ETF, EQUITY_ETF, QDII_ETF, STOCK
from quant_assistant.data.fetcher import CN_TZ
from quant_assistant.models import Market, StockPosition
from quant_assistant.portfolio.account_report import generate_account_reports
from quant_assistant.portfolio.holdings import PortfolioManager
from quant_assistant.v3 import build_intraday


NOW = dt.datetime(2026, 9, 22, 14, 30, tzinfo=CN_TZ)
ADVICE = {"buy_range": [98, 101], "reduce_range": [110, 112],
          "stop": 95, "target": 114, "action": "WATCH"}


def ma_frame(*, price=100, ma5=101, ma10=99, ma20=96, atr=2,
             volume_ratio=1.0, prev_price=99.5, prev_ma5=100, prev_ma10=99,
             prev_ma20=96):
    rows = []
    for index in range(24):
        rows.append({"日期": pd.Timestamp("2026-08-01") + pd.Timedelta(days=index),
                     "开盘": 100, "收盘": 100, "最高": 101, "最低": 99,
                     "成交量": 1000, "MA5": 100, "MA10": 99, "MA20": 96,
                     "ATR": atr, "量比": 1.0})
    rows[-2].update({"收盘": prev_price, "MA5": prev_ma5, "MA10": prev_ma10,
                     "MA20": prev_ma20})
    rows[-1].update({"收盘": price, "MA5": ma5, "MA10": ma10, "MA20": ma20,
                     "ATR": atr, "量比": volume_ratio})
    return pd.DataFrame(rows)


def result(frame, subtype=STOCK, advice=None, forbidden="禁止仅凭成本价加仓",
           weight=.15, cash="账户纪律：现金正常"):
    pos = SimpleNamespace(code="600000", name="测试", market=Market.A_SH,
                          asset_type="STOCK", asset_subtype=subtype)
    view = {"position": pos, "asset_subtype": subtype, "available": True,
            "weight": weight}
    discipline = {"forbidden_action": forbidden}
    return build_ma_discipline(view, frame, advice or ADVICE, discipline, cash)


def test_atr_normalized_overheat_and_pullback_states():
    hot = result(ma_frame(price=106, ma5=100, ma10=98, ma20=95, atr=2))
    assert hot["ma_state"] == "single_day_momentum_burst"
    assert "单日MA5乖离机械减仓" in hot["ma_forbidden_action"]
    pullback = result(ma_frame(price=99, ma5=100, ma10=98, ma20=95))
    assert pullback["ma_state"] == "MA5跌破但MA10仍守住"
    assert "直接当作加仓" in pullback["ma_forbidden_action"]
    assert pullback["ma_action_hint"] == "重新评估/加仓候选"


def test_ma10_and_effective_ma20_break_are_risk_hints_not_mechanical_sales():
    ma10 = result(ma_frame(price=98, ma5=100, ma10=99, ma20=95))
    assert ma10["ma_state"] == "MA10跌破"
    assert ma10["ma_action_hint"] == "风险收缩/减仓观察"
    broken = result(ma_frame(price=94, ma5=98, ma10=99, ma20=100,
                             prev_price=99, prev_ma20=100))
    assert broken["ma_state"] == "MA20有效跌破/趋势破坏"
    assert broken["ma_action_hint"] == "清仓复核"
    assert "多重确认" in broken["ma_confirmation"]


def test_cross_and_standing_ma10_are_candidates_and_high_cross_conflicts():
    cross = result(ma_frame(price=100, ma5=101, ma10=100, ma20=96,
                            volume_ratio=1.5, prev_ma5=99, prev_ma10=100))
    assert cross["ma_state"] == "MA5上穿MA10且放量"
    assert cross["ma_action_hint"] == "重新评估/加仓候选"
    standing = result(ma_frame(price=100, ma5=101, ma10=99.5, ma20=96,
                               volume_ratio=1.5, prev_price=99.5,
                               prev_ma5=100, prev_ma10=99))
    assert standing["ma_state"] == "站稳MA10且量价齐升"
    high = result(ma_frame(price=108, ma5=107, ma10=106, ma20=100,
                           volume_ratio=1.5, prev_price=106,
                           prev_ma5=105, prev_ma10=105.5),
                  advice={**ADVICE, "reduce_range": [108, 110]},
                  forbidden="禁止无视压力追高")
    assert high["ma_conflict_flag"] is True
    assert high["ma_action_hint"] == "信号冲突，人工复核"


def test_stall_divergence_coordinates_with_discipline_v1():
    frame = ma_frame(price=105, ma5=104, ma10=102, ma20=98, atr=2)
    frame.loc[frame.index[-6]:, "收盘"] = [100, 101, 103, 105, 106, 105]
    stalled = result(frame)
    assert stalled["ma_state"] == "连续拉升后滞涨/量价背离"
    assert "discipline-v1" in stalled["ma_reason"]


def test_no_volume_rally_requires_explicit_limit_status():
    record = {"asset_subtype": STOCK, "technical_features":
              {"MA5": 100, "MA10": 99, "MA20": 96, "ATR": 2},
              "reference_volume": 1000,
              "ma_discipline": result(ma_frame())}
    quote = {"price": 102, "previous_close": 100, "volume": 200,
             "session_progress": .5}
    degraded = intraday_ma_discipline(record, quote)
    assert "无量拉升" not in degraded["ma_state"]
    available = intraday_ma_discipline(record, {**quote, "limit_status": "not_sealed"})
    assert available["ma_state"] != "persistent_overheat"
    sealed = intraday_ma_discipline(record, {**quote, "limit_status": "sealed"})
    assert "观察" in sealed["ma_action_hint"]


def test_intraday_old_morning_record_degrades_without_guessing_cross():
    record = {"asset_subtype": STOCK, "technical_features":
              {"MA5": 100, "MA10": 99, "MA20": 96, "ATR": 2}}
    pullback = intraday_ma_discipline(record, {"price": 99.5})
    assert pullback["ma_state"] == "MA5跌破但MA10仍守住"
    normal = intraday_ma_discipline(record, {"price": 101})
    assert normal["ma_state"] == "均线结构未触发"
    assert "不补猜历史交叉" in normal["ma_reason"]


def test_bond_disabled_and_qdii_avoids_a_share_volume_logic():
    bond = result(ma_frame(), BOND_ETF)
    assert bond["ma_state"] == "不适用" and bond["applicability"] == "none"
    qdii = result(ma_frame(volume_ratio=3), QDII_ETF)
    assert qdii["applicability"] == "partial"
    assert "不使用A股成交量" in qdii["ma_confirmation"]
    assert "放量" not in qdii["ma_state"]
    qdii_intraday = intraday_ma_discipline(
        {"asset_subtype": QDII_ETF, "ma_discipline": qdii,
         "technical_features": {"MA5": 100, "MA10": 99, "MA20": 96, "ATR": 2}},
        {"price": 110, "previous_close": 100, "limit_status": "sealed"})
    assert qdii_intraday["ma_state"] == qdii["ma_state"]


def test_single_day_limit_like_bar_is_not_persistent_overheat():
    frame = ma_frame(price=110, ma5=102, ma10=100, ma20=96, atr=2,
                     prev_price=100, prev_ma5=100, prev_ma10=99)
    burst = result(frame, advice={**ADVICE, "reduce_range": [109, 112]})
    assert burst["ma_state"] == "single_day_momentum_burst"
    assert "SINGLE_DAY_MOMENTUM_BURST" in burst["signal_codes"]
    assert "PERSISTENT_OVERHEAT" not in burst["signal_codes"]
    assert burst["ma_action_hint"] == "单日强势脉冲，等待次日确认"


def test_intraday_unsealed_burst_with_reversal_enters_reduce_review():
    record = {"asset_subtype": STOCK, "technical_features":
              {"MA5": 100, "MA10": 99, "MA20": 96, "ATR": 2},
              "ma_discipline": result(ma_frame())}
    reviewed = intraday_ma_discipline(record, {
        "price": 109, "previous_close": 100, "high": 111,
        "limit_status": "not_sealed", "intraday_reversal": True})
    assert reviewed["ma_state"] == "single_day_momentum_burst"
    assert "减仓复核" in reviewed["ma_action_hint"]
    missing_seal = intraday_ma_discipline(record, {"price": 109, "previous_close": 100})
    assert missing_seal["ma_action_hint"] == "单日强势脉冲，等待次日确认"


def test_multi_day_rally_sustained_bias_and_target_is_persistent_overheat():
    frame = ma_frame(price=108, ma5=104, ma10=101, ma20=96, atr=2,
                     prev_price=106, prev_ma5=102, prev_ma10=100)
    frame.loc[frame.index[-5]:, "收盘"] = [100, 102, 104, 106, 108]
    frame.loc[frame.index[-5]:, "MA5"] = [99, 100, 101, 102, 104]
    hot = result(frame, advice={**ADVICE, "reduce_range": [108, 112]})
    assert hot["ma_state"] == "persistent_overheat"
    assert hot["ma_action_hint"] == "分批锁利复核"
    assert "PERSISTENT_OVERHEAT" in hot["signal_codes"]


def test_morning_compact_display_and_advice_history_persistence(tmp_path):
    pm = PortfolioManager(tmp_path / "portfolio.json")
    pm.cash = 10000
    pos = StockPosition(code="561550", name="权益ETF", market=Market.ETF,
                        shares=100, cost_price=99, current_price=100,
                        asset_type="ETF", asset_subtype=EQUITY_ETF)
    pm.positions = [pos]
    raw = ma_frame(price=100, ma5=101, ma10=100, ma20=96, volume_ratio=1.5,
                   prev_ma5=99, prev_ma10=100)
    raw["MACD"] = 1
    raw["RSI14"] = 50
    raw["K"] = 60
    raw["D"] = 40
    raw["MA60"] = 95
    raw["BOLL_UP"] = 110
    raw["BOLL_DN"] = 90
    raw["ATR_PCT"] = .02
    reports = generate_account_reports(pm, {pos.code: raw}, report_dir=tmp_path,
                                       generated_at=dt.datetime(2026, 8, 25, 9, 20,
                                                                tzinfo=CN_TZ))
    telegram = reports["telegram"].read_text(encoding="utf-8")
    assert "均线纪律：MA5上穿MA10且放量" in telegram
    row = make_records(reports, dt.datetime(2026, 8, 25, 9, 20, tzinfo=CN_TZ), "test")[0]
    assert row["ma_discipline_version"] == VERSION
    assert row["ma_state"] == "MA5上穿MA10且放量"
    assert row["ma_discipline"]["ma_confirmation"]


def test_intraday_fuses_ma_risk_into_priority_and_text():
    positions = [SimpleNamespace(code="000001", name="普通触发", market=Market.A_SH,
                                 asset_type="STOCK", asset_subtype=STOCK),
                 SimpleNamespace(code="000002", name="均线风险", market=Market.A_SH,
                                 asset_type="STOCK", asset_subtype=STOCK)]
    base = {"as_of": NOW.date().isoformat(), "buy_zone": [98, 101],
            "reduce_zone": [110, 112], "invalidation_price": 95,
            "target_price": 114, "reference_volume": 1000,
            "discipline": {"forbidden_action": "禁止仅凭成本价加仓"},
            "asset_subtype": STOCK}
    records = [
        {**base, "code": "000001", "technical_features":
         {"MA5": 100, "MA10": 99, "MA20": 96, "ATR": 2},
         "ma_discipline": result(ma_frame())},
        {**base, "code": "000002", "technical_features":
         {"MA5": 101, "MA10": 100, "MA20": 96, "ATR": 2},
         "ma_discipline": result(ma_frame(price=98, ma5=101, ma10=100, ma20=96))},
    ]

    class Fetcher:
        def fetch_intraday_quote(self, code, *_):
            price = 110.5 if code == "000001" else 97
            return {"price": price, "change_pct": 0, "volume": 500,
                    "amount": 100000, "as_of": NOW.isoformat(),
                    "provisional": True}

    text = build_intraday(SimpleNamespace(positions=positions), records, Fetcher(), NOW)
    assert text.index("均线风险") < text.index("普通触发")
    assert "跌破买入区 + MA10失守" in text


def test_intraday_target_text_distinguishes_burst_from_persistent_heat():
    position = SimpleNamespace(code="000725", name="京东方A", market=Market.A_SZ,
                               asset_type="STOCK", asset_subtype=STOCK)
    base = {"as_of": NOW.date().isoformat(), "code": "000725",
            "buy_zone": [98, 101], "reduce_zone": [110, 112],
            "invalidation_price": 95, "target_price": 110,
            "reference_volume": 1000, "asset_type": "STOCK",
            "asset_subtype": STOCK,
            "discipline": {"forbidden_action": "禁止仅凭成本价加仓"},
            "technical_features": {"MA5": 100, "MA10": 99, "MA20": 96, "ATR": 2}}

    class Fetcher:
        def fetch_intraday_quote(self, *_):
            return {"price": 110.5, "change_pct": 9.9, "volume": 500,
                    "amount": 100000, "as_of": NOW.isoformat(),
                    "provisional": True}

    burst = {**base, "ma_discipline": {
        **result(ma_frame(price=110, ma5=102, prev_price=100)),
        "ma_state": "single_day_momentum_burst"}}
    burst_text = build_intraday(SimpleNamespace(positions=[position]), [burst], Fetcher(), NOW)
    assert "目标已达 + 单日强势脉冲：观察，不因单日乖离机械减仓" in burst_text

    persistent = {**base, "ma_discipline": {
        **result(ma_frame()), "ma_state": "persistent_overheat",
        "ma_action_hint": "分批锁利复核"}}
    persistent_text = build_intraday(
        SimpleNamespace(positions=[position]), [persistent], Fetcher(), NOW)
    assert "目标已达 + 持续性过热：分批锁利复核" in persistent_text
