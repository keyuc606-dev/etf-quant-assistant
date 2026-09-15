import datetime
import copy
import json
import os
import re
import shutil
import uuid
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import DATA_DIR, ETF_POOL
from ..news.themes import ACCOUNT_THEME_MAP
from ..portfolio.holdings import PortfolioManager
from .models import TradeResult
from .repository import TradingRepository
from .sqlite_repository import SQLiteTradingRepository


MONEY_EPSILON = Decimal("0.0000001")
ETF_CODE_PATTERN = re.compile(r"^(?:15|16|18|50|51|52|56|58)\d{4}$")
STOCK_CODE_PATTERN = re.compile(r"^(?:00|30|60|68)\d{4}$")


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class TradingService:
    """SQLite 成交事实源与 portfolio.json 兼容投影服务。"""

    def __init__(self, database_path: Optional[Path] = None,
                 portfolio_path: Optional[Path] = None,
                 repository: Optional[TradingRepository] = None):
        if repository is not None and database_path is not None:
            raise ValueError("repository 与 database_path 不能同时指定")
        self.repository = repository or SQLiteTradingRepository(database_path)
        self.portfolio_path = Path(portfolio_path) if portfolio_path is not None else DATA_DIR / "portfolio.json"

    def initialize_from_portfolio(self) -> dict:
        """将当前账户作为初始快照导入，不伪造历史成交。"""
        pm = PortfolioManager(self.portfolio_path)
        with self.repository.transaction():
            if self.repository.is_initialized():
                raise ValueError("成交台账已经初始化，不能重复导入初始快照")
            created_at = _now_iso()
            account = {"cash": pm.cash, "cash_flows": pm.cash_flows, "created_at": created_at}
            positions = []
            for position in pm.positions:
                positions.append({
                    "code": position.code, "name": position.name,
                    "market": position.market.value, "shares": position.shares,
                    "asset_type": position.asset_type or (
                        "ETF" if position.market.value == "ETF" else "STOCK"
                    ),
                    "cost_price": position.cost_price, "current_price": position.current_price,
                    "sector": position.sector,
                    "last_updated": position.last_updated.isoformat() if position.last_updated else None,
                    "pe": position.pe, "pb": position.pb, "roe": position.roe,
                    "market_cap": position.market_cap,
                })
            self.repository.save_opening_snapshot(account, positions)
        return {"cash": pm.cash, "positions": len(pm.positions), "executions": 0}

    @staticmethod
    def _apply_trade(state: dict, side: str, code: str, quantity: int,
                     price: Decimal, fee: Decimal,
                     asset_type: Optional[str] = None) -> Tuple[Optional[Decimal], Optional[dict]]:
        positions = state["positions"]
        cash = _decimal(state["cash"])
        amount = price * quantity
        current = positions.get(code)
        resolved_type = asset_type or (
            current.get("asset_type") if current else None
        ) or ("ETF" if ETF_CODE_PATTERN.fullmatch(code) else "STOCK")
        if current is not None:
            current_type = current.get("asset_type") or (
                "ETF" if current.get("market") == "ETF" else "STOCK"
            )
            if current_type != resolved_type:
                raise ValueError(f"资产类型不一致：当前为 {current_type}，输入为 {resolved_type}")

        if side == "BUY":
            cash_needed = amount + fee
            if cash + MONEY_EPSILON < cash_needed:
                raise ValueError(
                    f"现金不足：需要 ￥{float(cash_needed):,.2f}，当前 ￥{float(cash):,.2f}"
                )
            old_quantity = current["shares"] if current else 0
            old_cost = _decimal(current["cost_price"]) if current else Decimal("0")
            new_quantity = old_quantity + quantity
            average_cost = (old_cost * old_quantity + amount + fee) / new_quantity
            if current is None:
                meta = ACCOUNT_THEME_MAP.get(code, ETF_POOL.get(code, {}))
                current = {
                    "code": code,
                    "name": meta.get("name", f"{resolved_type} {code}"),
                    "market": "ETF" if resolved_type == "ETF" else (
                        "上海" if code.startswith(("6", "68")) else "深圳"
                    ),
                    "asset_type": resolved_type,
                    "shares": new_quantity,
                    "cost_price": float(average_cost),
                    "current_price": float(price),
                    "sector": "",
                    "last_updated": None,
                    "pe": 0.0,
                    "pb": 0.0,
                    "roe": 0.0,
                    "market_cap": 0.0,
                }
                positions[code] = current
            else:
                current["shares"] = new_quantity
                current["cost_price"] = float(average_cost)
            state["cash"] = float(cash - cash_needed)
            return None, current

        if current is None or current["shares"] < quantity:
            held = current["shares"] if current else 0
            raise ValueError(f"持仓不足：拟卖出 {quantity} 份，当前仅 {held} 份")
        average_cost = _decimal(current["cost_price"])
        realized_pnl = quantity * (price - average_cost) - fee
        remaining = current["shares"] - quantity
        state["cash"] = float(cash + amount - fee)
        if remaining == 0:
            del positions[code]
            return realized_pnl, None
        current["shares"] = remaining
        return realized_pnl, current

    def is_initialized(self) -> bool:
        return self.repository.is_initialized()

    @staticmethod
    def _validate_trade(side: str, code: str, quantity: int,
                        price: float, fee: float,
                        asset_type: Optional[str] = None) -> Tuple[str, str, str, int, Decimal, Decimal]:
        normalized_side = str(side).upper()
        normalized_code = str(code).strip()
        if normalized_side not in ("BUY", "SELL"):
            raise ValueError("side 必须是 BUY 或 SELL")
        normalized_type = str(asset_type).upper() if asset_type else (
            "ETF" if ETF_CODE_PATTERN.fullmatch(normalized_code) else "STOCK"
        )
        if normalized_type not in ("ETF", "STOCK"):
            raise ValueError("asset_type 必须是 ETF 或 STOCK")
        pattern = ETF_CODE_PATTERN if normalized_type == "ETF" else STOCK_CODE_PATTERN
        if not pattern.fullmatch(normalized_code):
            raise ValueError(f"{normalized_type}代码格式无效: {normalized_code}")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("quantity 必须是正整数")
        price_value = _decimal(price)
        fee_value = _decimal(fee)
        if not price_value.is_finite() or price_value <= 0:
            raise ValueError("price 必须大于 0")
        if not fee_value.is_finite() or fee_value < 0:
            raise ValueError("fee 必须大于或等于 0")
        return normalized_side, normalized_code, normalized_type, quantity, price_value, fee_value

    @staticmethod
    def _assert_idempotent_match(row: dict, side: str, code: str,
                                 quantity: int, price: Decimal, fee: Decimal) -> None:
        matches = (
            row["side"] == side and row["code"] == code and
            row["quantity"] == quantity and
            _decimal(row["price"]) == price and _decimal(row["fee"]) == fee
        )
        if not matches:
            raise ValueError("external_id 已存在，但成交参数不一致")

    def record_trade(self, side: str, code: str, quantity: int, price: float,
                     fee: float = 0.0, external_id: Optional[str] = None,
                     source: str = "manual", related_plan_id: Optional[str] = None,
                     note: Optional[str] = None, executed_at: Optional[str] = None,
                     execution_id: Optional[str] = None,
                     asset_type: Optional[str] = None) -> TradeResult:
        side, code, asset_type, quantity, price_value, fee_value = self._validate_trade(
            side, code, quantity, price, fee, asset_type
        )
        normalized_external_id = external_id.strip() if external_id and external_id.strip() else None
        normalized_source = source.strip() if source and source.strip() else "manual"
        execution_time = executed_at or _now_iso()
        created_at = _now_iso()
        new_execution_id = execution_id or str(uuid.uuid4())

        projection_snapshot = None
        try:
            with self.repository.transaction():
                if not self.repository.is_initialized():
                    raise ValueError(
                        "成交台账尚未初始化；请先运行 portfolio-reconcile --initialize"
                    )

                if normalized_external_id is not None:
                    existing = self.repository.get_execution_by_external_id(
                        normalized_external_id
                    )
                    if existing is not None:
                        self._assert_idempotent_match(
                            existing, side, code, quantity, price_value, fee_value
                        )
                        state = self.repository.rebuild_account_state(
                            self._rebuild_from_facts
                        )
                        position = state["positions"].get(code)
                        return TradeResult(
                            execution=existing, cash=float(state["cash"]),
                            quantity=position["shares"] if position else 0,
                            average_cost=position["cost_price"] if position else 0.0,
                            duplicate=True,
                        )

                state = self.repository.rebuild_account_state(self._rebuild_from_facts)
                realized_pnl, updated_position = self._apply_trade(
                    state, side, code, quantity, price_value, fee_value, asset_type
                )
                execution = {
                    "execution_id": new_execution_id,
                    "external_id": normalized_external_id,
                    "side": side,
                    "code": code,
                    "asset_type": asset_type,
                    "quantity": quantity,
                    "price": float(price_value),
                    "fee": float(fee_value),
                    "executed_at": execution_time,
                    "source": normalized_source,
                    "related_plan_id": related_plan_id,
                    "note": note,
                    "realized_pnl": float(realized_pnl) if realized_pnl is not None else None,
                    "created_at": created_at,
                }
                execution = self.repository.append_execution(execution)
                projection_snapshot = self._snapshot_projection_files()
                self._write_projection(self._projection_from_state(state, traded_code=code))
                result = TradeResult(
                    execution=execution, cash=float(state["cash"]),
                    quantity=updated_position["shares"] if updated_position else 0,
                    average_cost=updated_position["cost_price"] if updated_position else 0.0,
                )
            return result
        except Exception:
            if projection_snapshot is not None:
                self._restore_projection_files(projection_snapshot)
            raise

    def preview_trade(self, side: str, code: str, quantity: int, price: float,
                      fee: float = 0.0, asset_type: Optional[str] = None) -> dict:
        """校验一笔成交并返回成交后摘要，但不写 SQLite 或持仓投影。"""
        side, code, asset_type, quantity, price_value, fee_value = self._validate_trade(
            side, code, quantity, price, fee, asset_type
        )
        if not self.repository.is_initialized():
            raise ValueError("成交台账尚未初始化；请先运行 portfolio-reconcile --initialize")
        state = copy.deepcopy(
            self.repository.rebuild_account_state(self._rebuild_from_facts)
        )
        realized_pnl, updated_position = self._apply_trade(
            state, side, code, quantity, price_value, fee_value, asset_type
        )
        return {
            "side": side,
            "code": code,
            "asset_type": asset_type,
            "quantity": quantity,
            "price": float(price_value),
            "fee": float(fee_value),
            "cash": float(state["cash"]),
            "position_quantity": updated_position["shares"] if updated_position else 0,
            "average_cost": updated_position["cost_price"] if updated_position else 0.0,
            "realized_pnl": float(realized_pnl) if realized_pnl is not None else None,
        }

    def _rebuild_from_facts(self, opening: dict, positions: List[dict],
                            cash_events: List[dict], executions: List[dict]) -> dict:
        state = {
            "cash": float(opening["cash"]),
            "cash_flows": list(opening.get("cash_flows", [])),
            "positions": {item["code"]: dict(item) for item in positions},
        }
        for row in cash_events:
            amount = _decimal(row["amount"])
            signed = amount if row["event_type"] == "DEPOSIT" else -amount
            state["cash"] = float(_decimal(state["cash"]) + signed)
            state["cash_flows"].append({
                "date": row["occurred_at"],
                "amount": float(signed),
                "note": row["note"] or row["event_type"],
            })
        for row in executions:
            self._apply_trade(
                state, row["side"], row["code"], row["quantity"],
                _decimal(row["price"]), _decimal(row["fee"]), row.get("asset_type"),
            )
        return state

    def rebuild_portfolio(self) -> dict:
        state = self.repository.rebuild_account_state(self._rebuild_from_facts)
        return self._projection_from_state(state)

    def recent_executions(self, limit: int = 20) -> List[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit 必须是正整数")
        if not self.repository.is_initialized():
            raise ValueError("成交台账尚未初始化；请先运行 portfolio-reconcile --initialize")
        return self.repository.list_executions(limit=limit, newest_first=True)

    def reconcile(self, repair: bool = False) -> dict:
        expected = self.rebuild_portfolio()
        actual = self._read_projection()
        differences = self._compare_projection(expected, actual)
        repaired = False
        if differences and repair:
            self._write_projection(expected)
            differences = []
            repaired = True
        return {"ok": not differences, "differences": differences, "repaired": repaired}

    def _projection_from_state(self, state: dict, traded_code: Optional[str] = None) -> dict:
        actual = self._read_projection(optional=True)
        actual_positions = {
            item.get("code"): item for item in actual.get("positions", [])
        } if actual else {}
        positions = []
        for code in sorted(state["positions"]):
            item = dict(state["positions"][code])
            live = actual_positions.get(code)
            if live:
                for field in ("current_price", "last_updated", "pe", "pb", "roe", "market_cap"):
                    if field in live:
                        item[field] = live[field]
            if code == traded_code and live is None:
                item["last_updated"] = datetime.datetime.now().isoformat()
            positions.append(item)
        return {
            "cash": float(state["cash"]),
            "cash_flows": list(state["cash_flows"]),
            "positions": positions,
        }

    def _read_projection(self, optional: bool = False) -> dict:
        if not self.portfolio_path.exists():
            if optional:
                return {}
            raise ValueError(f"portfolio.json 不存在: {self.portfolio_path}")
        try:
            data = json.loads(self.portfolio_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"portfolio.json 无法读取: {error}") from error
        if isinstance(data, list):
            return {"cash": 0.0, "cash_flows": [], "positions": data}
        if not isinstance(data, dict) or not isinstance(data.get("positions"), list):
            raise ValueError("portfolio.json 格式无效")
        data.setdefault("cash", 0.0)
        data.setdefault("cash_flows", [])
        return data

    @staticmethod
    def _compare_projection(expected: dict, actual: dict) -> List[str]:
        differences = []
        if abs(float(expected["cash"]) - float(actual.get("cash", 0.0))) > 1e-6:
            differences.append(
                f"现金不一致: SQLite={expected['cash']:.6f}, portfolio.json={float(actual.get('cash', 0.0)):.6f}"
            )
        if expected.get("cash_flows", []) != actual.get("cash_flows", []):
            differences.append("cash_flows 不一致")
        expected_positions = {item["code"]: item for item in expected["positions"]}
        actual_positions = {item["code"]: item for item in actual.get("positions", [])}
        for code in sorted(set(expected_positions) | set(actual_positions)):
            left = expected_positions.get(code)
            right = actual_positions.get(code)
            if left is None:
                differences.append(f"{code} 仅存在于 portfolio.json")
                continue
            if right is None:
                differences.append(f"{code} 仅存在于 SQLite 重建结果")
                continue
            if int(left["shares"]) != int(right.get("shares", 0)):
                differences.append(
                    f"{code} 数量不一致: SQLite={left['shares']}, portfolio.json={right.get('shares')}"
                )
            if abs(float(left["cost_price"]) - float(right.get("cost_price", 0.0))) > 1e-9:
                differences.append(
                    f"{code} 平均成本不一致: SQLite={left['cost_price']:.9f}, "
                    f"portfolio.json={float(right.get('cost_price', 0.0)):.9f}"
                )
            for field in ("name", "market", "asset_type", "sector"):
                if left.get(field, "") != right.get(field, ""):
                    differences.append(f"{code} {field} 不一致")
        return differences

    def _write_projection(self, data: dict) -> None:
        self.portfolio_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = self.portfolio_path.with_suffix(".json.bak")
        temp_path = self.portfolio_path.with_suffix(".json.tmp")
        if self.portfolio_path.exists():
            shutil.copy2(self.portfolio_path, backup_path)
        try:
            temp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp_path, self.portfolio_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _snapshot_projection_files(self) -> dict:
        paths = (self.portfolio_path, self.portfolio_path.with_suffix(".json.bak"))
        return {path: path.read_bytes() if path.exists() else None for path in paths}

    @staticmethod
    def _restore_projection_files(snapshot: dict) -> None:
        for path, content in snapshot.items():
            if content is None:
                if path.exists():
                    path.unlink()
            else:
                temp_path = path.with_suffix(path.suffix + ".restore-tmp")
                temp_path.write_bytes(content)
                os.replace(temp_path, path)
