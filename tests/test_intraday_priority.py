import datetime as dt
from types import SimpleNamespace

import pytest

from quant_assistant.analysis.discipline import t_opportunity
from quant_assistant.data.fetcher import CN_TZ
from quant_assistant.models import Market
from quant_assistant.v3 import build_intraday, classify_quote


DAY = "2026-09-22"
NOW = dt.datetime(2026, 9, 22, 14, 45, tzinfo=CN_TZ)


def advice(code="002074", **overrides):
    return {"as_of": DAY, "code": code, "buy_zone": [26.056, 26.375],
            "reduce_zone": [27.691, 28.010], "invalidation_price": 24.67,
            "target_price": 28.5, "technical_features": {"ATR": .5},
            "asset_type": "STOCK", **overrides}


def quote(price, **overrides):
    return {"price": price, "change_pct": 0, "volume": 1000, "amount": 100000,
            "as_of": NOW.isoformat(), "provisional": True, **overrides}


def test_guoxuan_and_woer_distinct_statuses():
    guoxuan = classify_quote(advice(), quote(27.680))
    assert guoxuan["status"] == "接近减仓区"
    assert guoxuan["distance"] == "距减仓区0.04%"
    woer = advice("002130", buy_zone=[16.954, 17.276],
                  reduce_zone=[17.614, 17.936], invalidation_price=16.2,
                  target_price=18.2, technical_features={"ATR": .4})
    assert classify_quote(woer, quote(16.820))["status"] == "跌破买入区但未失效"


@pytest.mark.parametrize("price,status", [
    (26.2, "已进入买入区"), (26.5, "接近买入区"),
    (24.8, "接近失效位"), (24.6, "已失效"),
    (27.8, "已进入减仓区"), (28.6, "目标已达"),
    (28.1, "已超过减仓区"), (27.0, "暂不动作"),
])
def test_price_states(price, status):
    assert classify_quote(advice(), quote(price))["status"] == status


def test_gap_direction_requires_open_and_previous_close():
    assert "向上跳空4.0%" in classify_quote(advice(), quote(27, open=104, previous_close=100))["flags"]
    assert "向下跳空4.0%" in classify_quote(advice(), quote(27, open=96, previous_close=100))["flags"]
    assert not any("跳空" in flag for flag in classify_quote(advice(), quote(27, open=96))["flags"])


def test_volume_uses_session_progress_and_degrades_without_baseline():
    morning = quote(27, volume=30, as_of="2026-09-22T10:30:00+08:00")
    assert classify_quote(advice(), morning, 100)["volume_note"] == "成交量正常"
    assert classify_quote(advice(), quote(27, volume=200), 100)["volume_note"] == "成交量异常"
    assert classify_quote(advice(), morning)["volume_note"] == "成交量判断不可用"
    assert classify_quote(advice(), {**morning, "as_of": None}, 100)["volume_note"] == "成交量判断不可用"


def test_t_requires_valid_range_and_cost_buffer():
    plan = {"buy_range": [98, 101], "reduce_range": [110, 112], "asset_type": "STOCK"}
    base = quote(100, open=100, previous_close=99, high=101, low=99)
    assert "无T空间" in t_opportunity(plan, 2, base)
    assert "具备T机会" in t_opportunity(plan, 2, {**base, "high": 108, "low": 96})
    assert "不建议做T" in t_opportunity(plan, 2, {**base, "high": None})


def test_sorted_focus_compact_detail_and_conflict():
    positions = [SimpleNamespace(code=f"{i:06d}", name=f"示例{i}", market=Market.A_SH)
                 for i in range(7)]
    prices = [27.0, 27.65, 27.68, 26.2, 24.6, 26.5, 27.1]
    records = [advice(pos.code, discipline={"forbidden_action": "禁止越跌越补、继续摊平"}
                      if i == 3 else {}) for i, pos in enumerate(positions)]

    class Fetcher:
        def fetch_intraday_quote(self, code, *_):
            return quote(prices[int(code)])

    detail = []
    text = build_intraday(SimpleNamespace(positions=positions), records, Fetcher(), NOW, detail=detail)
    assert text.index("示例4") < text.index("示例2") < text.index("示例1")
    assert "冲突，人工复核" in text
    assert "其余：" in text and "示例6" in text
    assert text.count("｜纪律：") == 5
    assert "成交量" not in text and "成交量判断不可用" in "\n".join(detail)
