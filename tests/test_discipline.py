import datetime as dt
from types import SimpleNamespace

import pandas as pd

from quant_assistant.analysis.discipline import build_discipline, cash_defense, t_opportunity
from quant_assistant.advice_performance import make_records
from quant_assistant.data.fetcher import CN_TZ
from quant_assistant.v3 import build_intraday


NOW = dt.datetime(2026, 9, 21, 9, 20, tzinfo=CN_TZ)
POS = SimpleNamespace(code="600000", name="虚构标的", asset_type="STOCK", cost_price=108,
                      current_price=100, market=SimpleNamespace(name="A_SH"))
ADVICE = {"code": "600000", "action": "WATCH", "buy_range": (98, 101),
          "reduce_range": (110, 112), "stop": 95, "target": 115, "confidence": "中",
          "ai_note": None}


def frame(price=100, ma20=105, ma5=99, macd=-1, atr=2, rsi=40, volume=1):
    return pd.DataFrame([{ "收盘": price, "MA20": ma20, "MA5": ma5,
                           "MACD": macd, "ATR": atr, "RSI14": rsi,
                           "量比": volume, "K": 30, "D": 40}] * 20)


def verdict(data=None, price=100, cost=108, executions=None, advice=None, quote=None):
    pos = SimpleNamespace(**{**POS.__dict__, "cost_price": cost})
    view = {"position": pos, "available": True, "pnl_pct": price / cost - 1,
            "weight": .2}
    return build_discipline(view, data if data is not None else frame(price),
                            advice or ADVICE, "账户纪律：现金正常", executions, NOW, quote)


def sale(price=96):
    return [{"code": "600000", "side": "SELL", "price": price,
             "executed_at": "2026-09-18T14:00:00+08:00"}]


def test_sold_then_rallied_without_reentry_and_unknown_history():
    d = verdict(executions=sale())
    assert d["discipline_state"] == "卖飞/减仓后续涨"
    assert "禁止情绪化追回" in d["forbidden_action"]
    assert verdict()["sale_status"] == "unknown"
    assert verdict(executions=sale() + [{"code": "600000", "side": "BUY",
           "price": 98, "executed_at": "2026-09-21T09:10:00+08:00"}])["sale_status"] == "unknown"


def test_reentry_requires_structure_not_cost():
    strong = frame(price=100, ma20=98, ma5=101, macd=1, volume=1.2)
    strong["K"] = 55
    strong["D"] = 40
    assert verdict(strong, executions=sale())["discipline_state"] == "回踩后重新评估"
    assert "加仓" in verdict(cost=100)["forbidden_action"]
    assert "回补" not in verdict(cost=100)["discipline_action"]


def test_weak_shallow_loss_and_deep_break():
    assert "反弹减仓" in verdict()["discipline_action"]
    broken = verdict(price=90, cost=120, advice={**ADVICE, "stop": 95})
    assert broken["discipline_state"] == "深套/趋势破坏"
    assert "越跌越补" in broken["forbidden_action"]


def test_profit_stall_and_t_space():
    hot = frame(price=110, ma20=105, ma5=111, macd=1, rsi=75)
    assert verdict(hot, price=110, cost=90)["discipline_state"] == "冲高滞涨"
    assert "不建议做T" in t_opportunity(ADVICE, 2)
    narrow = {"price": 100, "high": 101, "low": 99, "open": 100,
              "previous_close": 99, "provisional": True}
    wide = {"price": 100, "high": 108, "low": 96, "open": 100,
            "previous_close": 99, "provisional": True}
    assert "无T空间" in t_opportunity(ADVICE, 2, narrow)
    assert "具备T机会" in t_opportunity(ADVICE, 2, wide)


def test_cash_defense_and_history_label():
    pm = SimpleNamespace(total_assets=100, cash=5)
    assert "现金偏低" in cash_defense(pm, [{"weight": .6}])
    pm.cash = 40
    assert "现金充足" in cash_defense(pm, [{"weight": .2}])
    d = verdict()
    report = {"advices": [ADVICE], "views": [{"position": POS, "available": True,
              "weight": .2, "data_date": "2026-09-18", "indicators": {"ATR": 2}}],
              "stock_data": {}, "disciplines": {POS.code: d}}
    row = make_records(report, NOW, "test")[0]
    assert row["discipline_version"] == "discipline-v1"
    assert row["rule_version"] == "asset-routing-v1"


def test_intraday_displays_discipline_and_no_stale_price():
    d = verdict(executions=sale())
    advice = {"as_of": NOW.date().isoformat(), "code": POS.code,
              "buy_zone": [98, 101], "reduce_zone": [110, 112],
              "technical_features": {"ATR": 2}, "asset_type": "STOCK",
              "discipline": d}
    quote = {"price": 100, "change_pct": 0, "volume": 1000, "amount": 100000,
             "provisional": True}
    class Fetcher:
        def fetch_intraday_quote(self, *_):
            return quote
    text = build_intraday(SimpleNamespace(positions=[POS]), [advice], Fetcher(),
                          dt.datetime(2026, 9, 21, 14, 30, tzinfo=CN_TZ))
    assert "冲突，人工复核" in text
    assert "provisional" in text
