import datetime
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from quant_assistant.models import Market, RiskAlert, StockPosition
from quant_assistant.portfolio.account_report import (
    MAX_FOCUS_POSITIONS,
    _position_views,
    generate_account_reports,
    select_focus_positions,
)
from quant_assistant.portfolio.holdings import PortfolioManager


def position(index: int, cost: float = 1.0, price: float = 1.0) -> StockPosition:
    return StockPosition(
        code=f"159{index:03d}", name=f"测试ETF{index}", market=Market.ETF,
        shares=1000, cost_price=cost, current_price=price, sector=f"主题{index}",
    )


def signal_frame(weak: bool = False) -> pd.DataFrame:
    if weak:
        ma5 = [2.0, 1.0]
        ma20 = [1.5, 1.5]
    else:
        ma5 = [1.0, 1.0]
        ma20 = [1.5, 1.5]
    return pd.DataFrame({
        "日期": pd.to_datetime(["2026-09-10", "2026-09-11"]),
        "收盘": [1.0, 1.0], "MA5": ma5, "MA20": ma20,
    })


class AccountReportTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def manager(self, positions, cash=100000.0):
        manager = PortfolioManager(self.root / "missing.json")
        manager.positions = positions
        manager.cash = cash
        return manager

    def test_focus_positions_are_limited_to_five(self):
        positions = [position(i, cost=2.0, price=1.0) for i in range(7)]
        manager = self.manager(positions)
        data = {item.code: signal_frame(weak=True) for item in positions}

        focus = select_focus_positions(_position_views(manager, data))

        self.assertEqual(len(focus), MAX_FOCUS_POSITIONS)

    def test_quiet_positions_are_collapsed_into_other_holdings(self):
        positions = [position(i) for i in range(8)]
        manager = self.manager(positions, cash=1_000_000.0)
        data = {item.code: signal_frame() for item in positions}

        paths = generate_account_reports(
            manager, data, report_dir=self.root,
            generated_at=datetime.datetime(2026, 9, 13, 9, 20),
        )
        daily = paths["daily"].read_text(encoding="utf-8")

        self.assertEqual(paths["focus_count"], 0)
        self.assertIn("其余 8 只持仓", daily)
        self.assertEqual(daily.count("### "), 0)

    def test_report_uses_neutral_language_without_fake_trade_direction(self):
        item = position(1, cost=2.0, price=1.0)
        manager = self.manager([item])
        paths = generate_account_reports(
            manager,
            {item.code: signal_frame(weak=True)},
            alerts=[RiskAlert("DANGER", "个股止损线", "测试亏损已触及止损线", item.code)],
            report_dir=self.root,
        )
        daily = paths["daily"].read_text(encoding="utf-8")
        detail = paths["detail"].read_text(encoding="utf-8")

        self.assertIn("高亏损仓位 / 重点复核", daily)
        self.assertIn("- 交易纪律：", daily)
        self.assertIn("## 交易纪律实验标签（discipline-v1）", detail)
        self.assertIn("当前尚未接入 AI 综合判断", daily)
        self.assertNotIn("建议买入", daily + detail)
        self.assertNotIn("建议卖出", daily + detail)
        self.assertNotIn("金叉买入信号", daily + detail)
        self.assertNotIn("死叉卖出信号", daily + detail)

    def test_generates_daily_and_detail_with_complete_appendix(self):
        positions = [position(i) for i in range(6)]
        manager = self.manager(positions)
        data = {item.code: signal_frame() for item in positions}

        paths = generate_account_reports(manager, data, report_dir=self.root)
        detail = paths["detail"].read_text(encoding="utf-8")

        self.assertTrue(paths["daily"].exists())
        self.assertTrue(paths["detail"].exists())
        self.assertIn("## 完整持仓", detail)
        self.assertIn("## 全部技术指标：趋势与动量", detail)
        self.assertIn("MA60", detail)
        self.assertIn("## 全部技术指标：波动与状态", detail)
        self.assertIn("BOLL上轨", detail)
        self.assertIn("距涨停", detail)
        self.assertIn("## 行情数据来源与完整性", detail)
        for item in positions:
            self.assertIn(item.code, detail)


if __name__ == "__main__":
    unittest.main()
