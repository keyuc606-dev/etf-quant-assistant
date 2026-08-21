import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from ..allocation import compute_allocation
from ..config import ETF_POOL, STRATEGY_PARAMS, TARGET_WEIGHTS
from ..models import Market
from ..rebalance import generate_rebalance_plan


BACKTEST_NAV_NOTE = (
    "回测使用天天基金累计净值成交（分红按净值再投资、不含场内溢折价与买卖价差），"
    "该模式下 QDII 溢价闸门无市价可比、不生效；结果应视为理想成交假设下的上界。"
    "日常周度信号使用场内价格，敏感性对比请运行 --market 市价模式。"
)
BACKTEST_MARKET_NOTE = (
    "回测信号与成交均使用场内前复权价格（data/cache/{code}_daily.csv），"
    "QDII 溢价闸门按 市价/单位净值 计算并生效，与生产周度管线口径一致。"
)


@dataclass
class PortfolioBacktestConfig:
    initial_capital: float = 1_000_000.0
    cash_yield_annual: float = 0.02
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    slippage_pct: float = 0.001
    lot_size: int = 100
    min_trade_amount: float = 2000.0
    target_weights: Optional[Dict[str, float]] = None


@dataclass
class PortfolioBacktestResult:
    name: str
    start_date: Optional[datetime.date]
    end_date: Optional[datetime.date]
    config: PortfolioBacktestConfig
    metrics: dict
    annual_returns: List[dict]
    equity_curve: List[dict]
    trades: List[dict] = field(default_factory=list)
    signals_log: List[dict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass
class BacktestPosition:
    code: str
    name: str
    market: Market
    shares: int
    current_price: float

    @property
    def market_value(self) -> float:
        return self.shares * self.current_price


class PortfolioBacktestEngine:

    HIGHER_EQUITY_TARGET_WEIGHTS = {
        "511010": 0.30,
        "511360": 0.175,
        "510300": 0.15,
        "512890": 0.15,
        "510500": 0.0,
        "518880": 0.10,
        "513100": 0.125,
        "513500": 0.0,
    }

    def __init__(self, config: Optional[PortfolioBacktestConfig] = None):
        self.config = config or PortfolioBacktestConfig()

    def run(self, nav_data: Dict[str, pd.DataFrame], start_date: datetime.date,
            variant: str = "full",
            market_data: Optional[Dict[str, pd.DataFrame]] = None) -> PortfolioBacktestResult:
        """跑组合级回测。

        默认（净值模式）：信号与成交都用累计净值，历史长、含分红，但无溢折价，
        QDII 溢价闸门不生效。传入 ``market_data``（场内日线，含可选"单位净值"列）
        则切换为市价模式：信号与成交均按场内价格，溢价闸门生效，与生产口径一致。
        """
        market_mode = market_data is not None and any(
            df is not None and not df.empty for df in market_data.values()
        )
        if market_mode:
            price_table = self._market_price_table(market_data)
            all_history = self._market_allocation_frames(market_data)
        else:
            price_table = self._price_table(nav_data)
            all_history = self._allocation_frames(nav_data)
        price_table = price_table[price_table.index >= pd.Timestamp(start_date)]
        if price_table.empty:
            raise ValueError("回测价格数据为空，无法回测")

        benchmark = self._benchmark_series(price_table)
        signal_dates = set(self._weekly_signal_dates(price_table.index))
        first_dates = self._first_dates(market_data if market_mode else nav_data)
        params = dict(STRATEGY_PARAMS)
        if variant == "no_circuit":
            params["disable_circuit_breaker"] = True
            name = "关闭断路器"
        elif variant == "no_trend":
            params["disable_trend_filter"] = True
            name = "关闭趋势过滤"
        elif variant == "higher_equity":
            name = "权益中枢上调"
        else:
            name = "完整规则"
        base_target_weights = self._target_weights_for_variant(variant)

        cash = self.config.initial_capital
        shares = {code: 0 for code in ETF_POOL}
        state = {"circuit_breaker_high_water": self.config.initial_capital, "events": []}
        pending_trades = None
        pending_signal_date = None
        equity_curve = []
        trades = []
        signals_log = []

        previous_date = None

        for idx, ts in enumerate(price_table.index):
            date = ts.date()
            if previous_date is not None:
                cash = self._accrue_cash_yield(cash, (date - previous_date).days)
            previous_date = date
            prices = self._prices_for_day(price_table, ts)

            if pending_trades is not None and pending_signal_date is not None and date > pending_signal_date:
                cash = self._execute_trades(pending_trades, prices, shares, cash, date, trades)
                state = self._apply_pending_breaker_reset(state, date)
                pending_trades = None
                pending_signal_date = None

            total_assets = cash + self._position_value(shares, prices)
            bm_value = benchmark.get(ts)
            equity_curve.append({
                "date": date,
                "equity": total_assets,
                "cash": cash,
                "benchmark": bm_value,
            })

            if ts in signal_dates and idx < len(price_table.index) - 1:
                signal_data = self._market_data_until(all_history, ts, first_dates)
                allocation = compute_allocation(
                    market_data=signal_data,
                    total_assets=total_assets,
                    state=state,
                    as_of_date=date,
                    params=params,
                    target_weights=base_target_weights,
                )
                state = allocation["state_updates"]
                adjusted_target_weights = self._adjust_targets_for_availability(
                    allocation["target_weights"], prices, first_dates, date
                )
                positions = self._positions(shares, prices)
                plan = generate_rebalance_plan(
                    target_weights=adjusted_target_weights,
                    positions=positions,
                    cash=cash,
                    prices=prices,
                    allocation_result=allocation,
                    params=self._planner_params(params),
                )
                pending_trades = plan["trades"]
                pending_signal_date = date
                signals_log.append({
                    "date": date,
                    "drawdown_action": allocation["drawdown"]["action"],
                    "drawdown_pct": allocation["drawdown"]["drawdown_pct"],
                    "target_weights": adjusted_target_weights,
                    "trade_count": len(plan["trades"]),
                    "blocked": bool(plan.get("blocked")),
                    "blocked_reason": plan.get("message", ""),
                    "warnings": allocation["warnings"],
                })

        metrics = self._metrics(equity_curve)
        metrics["total_trades"] = len(trades)
        annual_returns = self._annual_returns(equity_curve)
        price_label = "场内价格" if market_mode else "累计净值"
        notes = [
            BACKTEST_MARKET_NOTE if market_mode else BACKTEST_NAV_NOTE,
            f"成交假设：周五收盘出信号，下一交易日按{price_label}成交；"
            "买入加 0.1% 滑点，卖出扣 0.1% 滑点。",
            "闲置现金按年化2%计息（货币基金保守近似），主要影响 2020-08 前短融无数据区间。",
        ]
        notes.extend(self._data_substitution_notes(start_date, first_dates))
        blocked_weeks = sum(1 for row in signals_log if row.get("blocked"))
        if blocked_weeks:
            notes.append(
                f"警告：{blocked_weeks} 个信号周因数据门禁（陈旧/缺失）未生成再平衡清单，"
                "回测可能低估规则效果，请核对数据完整性。"
            )
        return PortfolioBacktestResult(
            name=name,
            start_date=equity_curve[0]["date"] if equity_curve else None,
            end_date=equity_curve[-1]["date"] if equity_curve else None,
            config=self.config,
            metrics=metrics,
            annual_returns=annual_returns,
            equity_curve=equity_curve,
            trades=trades,
            signals_log=signals_log,
            notes=notes,
        )

    def _target_weights_for_variant(self, variant: str) -> Dict[str, float]:
        if variant == "higher_equity":
            return dict(self.HIGHER_EQUITY_TARGET_WEIGHTS)
        if self.config.target_weights is not None:
            return dict(self.config.target_weights)
        return dict(TARGET_WEIGHTS)

    def _accrue_cash_yield(self, cash: float, days: int) -> float:
        if cash <= 0 or days <= 0 or self.config.cash_yield_annual <= 0:
            return cash
        return cash * ((1 + self.config.cash_yield_annual) ** (days / 365.0))

    def _planner_params(self, params: dict) -> dict:
        merged = dict(params)
        merged["commission_rate"] = self.config.commission_rate
        merged["min_commission"] = self.config.min_commission
        merged["lot_size"] = self.config.lot_size
        merged["min_trade_amount"] = self.config.min_trade_amount
        return merged

    @staticmethod
    def _price_table(nav_data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        frames = []
        for code, df in nav_data.items():
            if df is None or df.empty:
                continue
            clean = df.copy()
            clean["日期"] = pd.to_datetime(clean["日期"])
            clean = clean.dropna(subset=["日期", "累计净值"]).sort_values("日期")
            frames.append(clean.set_index("日期")["累计净值"].rename(code))
        if not frames:
            return pd.DataFrame()
        table = pd.concat(frames, axis=1).sort_index()
        return table.ffill()

    @staticmethod
    def _market_price_table(market_data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        frames = []
        for code, df in market_data.items():
            if df is None or df.empty or "收盘" not in df.columns:
                continue
            clean = df.copy()
            clean["日期"] = pd.to_datetime(clean["日期"])
            clean["收盘"] = pd.to_numeric(clean["收盘"], errors="coerce")
            clean = clean.dropna(subset=["日期", "收盘"]).sort_values("日期")
            frames.append(clean.set_index("日期")["收盘"].rename(code))
        if not frames:
            return pd.DataFrame()
        table = pd.concat(frames, axis=1).sort_index()
        return table.ffill()

    @staticmethod
    def _benchmark_series(price_table: pd.DataFrame) -> dict:
        if "510300" not in price_table:
            return {}
        series = price_table["510300"].dropna()
        if series.empty:
            return {}
        first = float(series.iloc[0])
        return {idx: 1_000_000.0 * float(val) / first for idx, val in series.items() if first > 0}

    @staticmethod
    def _weekly_signal_dates(index) -> List[pd.Timestamp]:
        frame = pd.DataFrame({"date": index}, index=index)
        weekly = frame.resample("W-FRI").last().dropna()
        return list(weekly["date"])

    @staticmethod
    def _first_dates(nav_data: Dict[str, pd.DataFrame]) -> Dict[str, datetime.date]:
        first_dates = {}
        for code, df in nav_data.items():
            if df is None or df.empty:
                continue
            first_dates[code] = pd.to_datetime(df["日期"]).min().date()
        return first_dates

    @staticmethod
    def _allocation_frames(nav_data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        frames = {}
        for code, df in nav_data.items():
            if df is None or df.empty:
                continue
            clean = df.copy()
            clean["日期"] = pd.to_datetime(clean["日期"])
            clean["收盘"] = pd.to_numeric(clean["累计净值"], errors="coerce")
            clean["开盘"] = clean["收盘"]
            clean["最高"] = clean["收盘"]
            clean["最低"] = clean["收盘"]
            frames[code] = (clean[["日期", "开盘", "收盘", "最高", "最低"]]
                            .dropna()
                            .sort_values("日期")
                            .reset_index(drop=True))
        return frames

    @staticmethod
    def _market_allocation_frames(market_data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """市价模式的信号帧：收盘=场内价，保留"单位净值"列供 QDII 溢价闸门使用。

        注意不能在净值帧里附单位净值——累计净值/单位净值之差是累计分红而非溢价，
        会造成溢价闸门误判。
        """
        frames = {}
        for code, df in market_data.items():
            if df is None or df.empty or "收盘" not in df.columns:
                continue
            clean = df.copy()
            clean["日期"] = pd.to_datetime(clean["日期"])
            clean["收盘"] = pd.to_numeric(clean["收盘"], errors="coerce")
            clean["开盘"] = clean["收盘"]
            clean["最高"] = clean["收盘"]
            clean["最低"] = clean["收盘"]
            cols = ["日期", "开盘", "收盘", "最高", "最低"]
            if "单位净值" in clean.columns:
                clean["单位净值"] = pd.to_numeric(clean["单位净值"], errors="coerce")
                cols.append("单位净值")
            frames[code] = (clean[cols]
                            .dropna(subset=["日期", "收盘"])
                            .sort_values("日期")
                            .reset_index(drop=True))
        return frames

    @staticmethod
    def _data_substitution_notes(start_date: datetime.date,
                                 first_dates: Dict[str, datetime.date]) -> List[str]:
        notes = []
        first_512890 = first_dates.get("512890")
        if first_512890 is not None and start_date < first_512890:
            sub_end = first_512890 - datetime.timedelta(days=1)
            notes.append(
                f"数据替代：512890 上市前（{start_date} ~ {sub_end}）以 510300 数据替代该腿，"
                "该区间收益/回撤反映的是替代标的特性，非红利低波本身。"
            )
        late_codes = sorted(
            (code for code, first in first_dates.items()
             if first is not None and first > start_date),
            key=lambda c: first_dates[c],
        )
        if late_codes:
            spans = "、".join(
                f"{code} 自 {first_dates[code].isoformat()} 起有数据" for code in late_codes
            )
            notes.append(
                f"数据起点晚于回测起点：{spans}；此前该腿预算回落短融ETF/现金，"
                "早期年份结果含构造成分，且标的池为事后选定（生存者偏差），解读时请注意。"
            )
        return notes

    def _market_data_until(self, all_history: Dict[str, pd.DataFrame], ts: pd.Timestamp,
                           first_dates: Dict[str, datetime.date]) -> Dict[str, pd.DataFrame]:
        data = {}
        for code, df in all_history.items():
            sliced = df[df["日期"] <= ts].copy()
            if not sliced.empty:
                data[code] = sliced
        date = ts.date()
        if self._needs_512890_substitute(date, first_dates) and "510300" in data:
            data["512890"] = data["510300"].copy()
        return data

    @staticmethod
    def _needs_512890_substitute(date: datetime.date, first_dates: Dict[str, datetime.date]) -> bool:
        first = first_dates.get("512890")
        return first is None or date < first

    def _adjust_targets_for_availability(self, target_weights: Dict[str, float], prices: Dict[str, float],
                                         first_dates: Dict[str, datetime.date],
                                         date: datetime.date) -> Dict[str, float]:
        adjusted = dict(target_weights)
        if self._needs_512890_substitute(date, first_dates):
            weight = adjusted.get("512890", 0.0)
            if weight > 0 and prices.get("510300") is not None:
                adjusted["510300"] = adjusted.get("510300", 0.0) + weight
                adjusted["512890"] = 0.0
        for code, weight in list(adjusted.items()):
            if code == "511360" or weight <= 0:
                continue
            if prices.get(code) is None:
                adjusted[code] = 0.0
                adjusted["511360"] = adjusted.get("511360", 0.0) + weight
        return adjusted

    @staticmethod
    def _prices_for_day(price_table: pd.DataFrame, ts: pd.Timestamp) -> Dict[str, float]:
        row = price_table.loc[ts]
        prices = {}
        for code, value in row.items():
            if pd.notna(value):
                prices[code] = float(value)
        return prices

    @staticmethod
    def _position_value(shares: Dict[str, int], prices: Dict[str, float]) -> float:
        value = 0.0
        for code, qty in shares.items():
            price = prices.get(code)
            if price is not None:
                value += qty * price
        return value

    def _positions(self, shares: Dict[str, int], prices: Dict[str, float]) -> List[BacktestPosition]:
        positions = []
        for code, qty in shares.items():
            if qty <= 0 or prices.get(code) is None:
                continue
            positions.append(BacktestPosition(
                code=code,
                name=ETF_POOL.get(code, {}).get("name", code),
                market=Market.ETF,
                shares=qty,
                current_price=prices[code],
            ))
        return positions

    def _execute_trades(self, plan_trades: List[dict], prices: Dict[str, float],
                        shares: Dict[str, int], cash: float,
                        date: datetime.date, trade_log: List[dict]) -> float:
        for trade in plan_trades:
            code = trade["code"]
            price = prices.get(code)
            if price is None:
                continue
            qty = int(trade["shares"])
            if qty <= 0:
                continue
            if trade["action"] == "SELL":
                qty = min(qty, shares.get(code, 0))
                if qty <= 0:
                    continue
                exec_price = price * (1 - self.config.slippage_pct)
                amount = exec_price * qty
                fee = self._commission(amount)
                shares[code] = shares.get(code, 0) - qty
                cash += amount - fee
            else:
                exec_price = price * (1 + self.config.slippage_pct)
                while qty > 0:
                    amount = exec_price * qty
                    fee = self._commission(amount)
                    if amount + fee <= cash:
                        break
                    qty -= self.config.lot_size
                if qty <= 0:
                    continue
                amount = exec_price * qty
                fee = self._commission(amount)
                shares[code] = shares.get(code, 0) + qty
                cash -= amount + fee
            trade_log.append({
                "date": date,
                "code": code,
                "name": trade["name"],
                "action": trade["action"],
                "shares": qty,
                "price": exec_price,
                "amount": amount,
                "fee": fee,
                "reason": trade["reason"],
            })
        return cash

    def _commission(self, amount: float) -> float:
        if amount <= 0:
            return 0.0
        return max(amount * self.config.commission_rate, self.config.min_commission)

    @staticmethod
    def _apply_pending_breaker_reset(state: dict, date: datetime.date) -> dict:
        pending = state.get("pending_breaker_reset") if isinstance(state, dict) else None
        if not isinstance(pending, dict):
            return state
        updated = dict(state)
        reset_to = float(pending.get("reset_to", 0.0) or 0.0)
        updated["circuit_breaker_high_water"] = reset_to
        updated.pop("pending_breaker_reset", None)
        events = list(updated.get("events") or [])
        events.append({
            "date": date.isoformat(),
            "type": "circuit_breaker_reset_after_execution",
            "message": "断路器清零卖出已确认执行，高点已重置",
            "reset_to": reset_to,
        })
        updated["events"] = events
        return updated

    def _metrics(self, equity_curve: List[dict]) -> dict:
        if not equity_curve:
            return {}
        initial = self.config.initial_capital
        final = equity_curve[-1]["equity"]
        total_return = final / initial - 1 if initial > 0 else 0.0
        days = max((equity_curve[-1]["date"] - equity_curve[0]["date"]).days, 1)
        annual_return = (final / initial) ** (365.0 / days) - 1 if final > 0 else -1.0
        max_dd = self._max_drawdown([row["equity"] for row in equity_curve])
        calmar = annual_return / max_dd if max_dd > 0 else None
        return {
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_dd,
            "calmar_ratio": calmar,
            "final_equity": final,
            "initial_capital": initial,
            "trading_days": len(equity_curve),
            "total_trades": 0,
        }

    @staticmethod
    def _max_drawdown(values: List[float]) -> float:
        peak = values[0]
        max_dd = 0.0
        for value in values:
            if value > peak:
                peak = value
            dd = (peak - value) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @staticmethod
    def _annual_returns(equity_curve: List[dict]) -> List[dict]:
        if not equity_curve:
            return []
        by_year = {}
        for row in equity_curve:
            by_year.setdefault(row["date"].year, []).append(row)
        result = []
        prev_strategy = equity_curve[0]["equity"]
        prev_benchmark = equity_curve[0]["benchmark"]
        for year in sorted(by_year):
            rows = by_year[year]
            last = rows[-1]
            strategy_return = last["equity"] / prev_strategy - 1 if prev_strategy > 0 else 0.0
            benchmark_return = None
            strategy_drawdown = PortfolioBacktestEngine._max_drawdown([r["equity"] for r in rows])
            benchmark_values = [r["benchmark"] for r in rows if r["benchmark"] is not None]
            benchmark_drawdown = (PortfolioBacktestEngine._max_drawdown(benchmark_values)
                                  if benchmark_values else None)
            if prev_benchmark is not None and last["benchmark"] is not None and prev_benchmark > 0:
                benchmark_return = last["benchmark"] / prev_benchmark - 1
            result.append({
                "year": year,
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "excess_return": (strategy_return - benchmark_return
                                  if benchmark_return is not None else None),
                "strategy_max_drawdown": strategy_drawdown,
                "benchmark_max_drawdown": benchmark_drawdown,
            })
            prev_strategy = last["equity"]
            if last["benchmark"] is not None:
                prev_benchmark = last["benchmark"]
        return result
