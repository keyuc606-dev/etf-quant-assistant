import datetime
import unittest

import pandas as pd

from quant_assistant.backtest.portfolio_engine import PortfolioBacktestConfig, PortfolioBacktestEngine
from quant_assistant.config import ETF_POOL


def make_nav_data(start="2019-01-01", end="2020-04-30", shock=False):
    dates = pd.bdate_range(start=start, end=end)
    data = {}
    for idx, code in enumerate(ETF_POOL):
        values = []
        for i, _date in enumerate(dates):
            value = 1.0 + i * 0.001 + idx * 0.01
            if shock and _date >= pd.Timestamp("2020-01-20"):
                value *= 0.88
            if shock and _date >= pd.Timestamp("2020-02-03"):
                value *= 1.03
            values.append(value)
        data[code] = pd.DataFrame({"日期": dates, "累计净值": values})
    return data


def make_market_data(nav_data, qdii_premium=None):
    """由净值数据派生场内价格帧；qdii_premium 对两只 QDII 统一设置溢价。"""
    market = {}
    for code, df in nav_data.items():
        clean = df.copy()
        clean["收盘"] = clean["累计净值"]
        if qdii_premium is not None and code in ("513100", "513500"):
            clean["单位净值"] = clean["收盘"] / (1.0 + qdii_premium)
        market[code] = clean
    return market


class PortfolioBacktestEngineTest(unittest.TestCase):

    def test_weekly_signal_trades_on_next_trading_day(self):
        engine = PortfolioBacktestEngine(PortfolioBacktestConfig(initial_capital=1_000_000.0))
        result = engine.run(
            nav_data=make_nav_data(),
            start_date=datetime.date(2020, 1, 6),
            variant="full",
        )
        self.assertTrue(result.signals_log)
        self.assertTrue(result.trades)
        first_signal = result.signals_log[0]["date"]
        first_trade = result.trades[0]["date"]
        self.assertEqual(first_signal.weekday(), 4)
        self.assertGreater(first_trade, first_signal)
        self.assertEqual(first_trade.weekday(), 0)

    def test_circuit_breaker_state_evolves_with_phase21_reset(self):
        engine = PortfolioBacktestEngine(PortfolioBacktestConfig(initial_capital=1_000_000.0))
        result = engine.run(
            nav_data=make_nav_data(shock=True),
            start_date=datetime.date(2020, 1, 6),
            variant="full",
        )
        actions = [row["drawdown_action"] for row in result.signals_log]
        self.assertIn("risk_zero", actions)
        zero_idx = actions.index("risk_zero")
        self.assertIn("none", actions[zero_idx + 1:])
        zero_row = result.signals_log[zero_idx]
        self.assertTrue(any("等待确认卖出执行后重置高点" in msg for msg in zero_row["warnings"]))

    def test_cash_yield_accrues_on_idle_cash(self):
        dates = pd.bdate_range(start="2024-01-01", end="2025-01-01")
        nav_data = {
            "510300": pd.DataFrame({"日期": dates, "累计净值": [1.0] * len(dates)})
        }
        cash_only_weights = {code: 0.0 for code in ETF_POOL}
        engine = PortfolioBacktestEngine(PortfolioBacktestConfig(
            initial_capital=1_000_000.0,
            cash_yield_annual=0.05,
            target_weights=cash_only_weights,
        ))
        result = engine.run(
            nav_data=nav_data,
            start_date=datetime.date(2024, 1, 1),
            variant="full",
        )
        self.assertFalse(result.trades)
        self.assertAlmostEqual(result.metrics["annual_return"], 0.05, places=4)

    def test_market_mode_runs_and_uses_market_note(self):
        nav_data = make_nav_data()
        engine = PortfolioBacktestEngine(PortfolioBacktestConfig(initial_capital=1_000_000.0))
        result = engine.run(
            nav_data=nav_data,
            start_date=datetime.date(2020, 1, 6),
            variant="full",
            market_data=make_market_data(nav_data),
        )
        self.assertTrue(any("场内前复权价格" in note for note in result.notes))
        self.assertTrue(any("溢价闸门" in note and "生效" in note for note in result.notes))

    def test_market_mode_premium_gate_blocks_qdii_buy(self):
        nav_data = make_nav_data()
        market_data = make_market_data(nav_data, qdii_premium=0.05)
        engine = PortfolioBacktestEngine(PortfolioBacktestConfig(initial_capital=1_000_000.0))
        result = engine.run(
            nav_data=nav_data,
            start_date=datetime.date(2020, 1, 6),
            variant="full",
            market_data=market_data,
        )
        qdii_buys = [t for t in result.trades
                     if t["action"] == "BUY" and t["code"] in ("513100", "513500")]
        self.assertEqual(qdii_buys, [])
        # 溢价超限的 QDII 权重应归零，预算回短融
        overweight = [row for row in result.signals_log
                      if row["target_weights"].get("513100", 0) > 0
                      or row["target_weights"].get("513500", 0) > 0]
        self.assertEqual(overweight, [])

    def test_market_mode_premium_gate_not_falsely_triggered_without_unit_nav(self):
        """净值模式（无单位净值列）不得把累计净值/单位净值误当溢价。"""
        nav_data = make_nav_data()
        engine = PortfolioBacktestEngine(PortfolioBacktestConfig(initial_capital=1_000_000.0))
        result = engine.run(
            nav_data=nav_data,
            start_date=datetime.date(2020, 1, 6),
            variant="full",
        )
        qdii_buys = [t for t in result.trades
                     if t["action"] == "BUY" and t["code"] in ("513100", "513500")]
        self.assertTrue(qdii_buys)

    def test_substitution_window_is_annotated_in_notes(self):
        nav_data = make_nav_data()
        # 512890 数据起点显著晚于回测起点
        late = nav_data["512890"]
        nav_data["512890"] = late[late["日期"] >= pd.Timestamp("2020-02-03")]
        engine = PortfolioBacktestEngine(PortfolioBacktestConfig(initial_capital=1_000_000.0))
        result = engine.run(
            nav_data=nav_data,
            start_date=datetime.date(2020, 1, 6),
            variant="full",
        )
        self.assertTrue(any("数据替代" in note and "510300" in note for note in result.notes))


if __name__ == "__main__":
    unittest.main()
