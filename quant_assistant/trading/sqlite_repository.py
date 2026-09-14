import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

from ..config import DATA_DIR
from .repository import TradingRepository


SCHEMA_VERSION = 2
DEFAULT_DATABASE_PATH = DATA_DIR / "trading.sqlite3"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opening_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL CHECK (cash >= 0),
    cash_flows_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opening_positions (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT NOT NULL,
    asset_type TEXT NOT NULL DEFAULT 'ETF' CHECK (asset_type IN ('ETF', 'STOCK')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    cost_price REAL NOT NULL CHECK (cost_price >= 0),
    current_price REAL NOT NULL CHECK (current_price >= 0),
    sector TEXT NOT NULL DEFAULT '',
    last_updated TEXT,
    pe REAL NOT NULL DEFAULT 0,
    pb REAL NOT NULL DEFAULT 0,
    roe REAL NOT NULL DEFAULT 0,
    market_cap REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS executions (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL UNIQUE,
    external_id TEXT,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    code TEXT NOT NULL,
    asset_type TEXT NOT NULL DEFAULT 'ETF' CHECK (asset_type IN ('ETF', 'STOCK')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price REAL NOT NULL CHECK (price > 0),
    fee REAL NOT NULL CHECK (fee >= 0),
    executed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    related_plan_id TEXT,
    note TEXT,
    realized_pnl REAL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS executions_external_id_unique
ON executions(external_id)
WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS cash_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    cash_event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type IN ('DEPOSIT', 'WITHDRAWAL')),
    amount REAL NOT NULL CHECK (amount > 0),
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
"""


class SQLiteTradingRepository(TradingRepository):
    """schema v1 的 SQLite 实现，兼容 Phase 1 数据库文件。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else DEFAULT_DATABASE_PATH
        self._active_connection: Optional[sqlite3.Connection] = None
        self.initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._active_connection is not None:
            yield self._active_connection
            return
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize_schema(self) -> None:
        with self._connection() as connection:
            connection.executescript(SCHEMA_SQL)
            opening_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(opening_positions)")
            }
            if "asset_type" not in opening_columns:
                connection.execute(
                    "ALTER TABLE opening_positions ADD COLUMN asset_type TEXT NOT NULL DEFAULT 'ETF'"
                )
            execution_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(executions)")
            }
            if "asset_type" not in execution_columns:
                connection.execute(
                    "ALTER TABLE executions ADD COLUMN asset_type TEXT NOT NULL DEFAULT 'ETF'"
                )
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) "
                "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
                (SCHEMA_VERSION,),
            )

    def get_schema_version(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
            return int(row["version"] or 0)

    @contextmanager
    def transaction(self):
        if self._active_connection is not None:
            raise RuntimeError("不支持嵌套成交台账事务")
        connection = self._connect()
        self._active_connection = connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield self
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            self._active_connection = None
            connection.close()

    def load_opening_account(self) -> Optional[dict]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM opening_account WHERE id = 1").fetchone()
        if row is None:
            return None
        return {
            "cash": row["cash"],
            "cash_flows": json.loads(row["cash_flows_json"]),
            "created_at": row["created_at"],
        }

    def load_opening_positions(self) -> List[dict]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM opening_positions ORDER BY code").fetchall()
        return [
            {
                "code": row["code"], "name": row["name"], "market": row["market"],
                "asset_type": row["asset_type"],
                "shares": row["quantity"], "cost_price": row["cost_price"],
                "current_price": row["current_price"], "sector": row["sector"],
                "last_updated": row["last_updated"], "pe": row["pe"], "pb": row["pb"],
                "roe": row["roe"], "market_cap": row["market_cap"],
            }
            for row in rows
        ]

    def save_opening_snapshot(self, account: dict, positions: List[dict]) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO opening_account(id, cash, cash_flows_json, created_at) VALUES (1, ?, ?, ?)",
                (account["cash"], json.dumps(account.get("cash_flows", []), ensure_ascii=False),
                 account["created_at"]),
            )
            for position in positions:
                connection.execute(
                    """
                    INSERT INTO opening_positions(
                        code, name, market, asset_type, quantity, cost_price, current_price,
                        sector, last_updated, pe, pb, roe, market_cap
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        position["code"], position["name"], position["market"],
                        position.get("asset_type", "ETF"),
                        position["shares"], position["cost_price"], position["current_price"],
                        position.get("sector", ""), position.get("last_updated"),
                        position.get("pe", 0.0), position.get("pb", 0.0),
                        position.get("roe", 0.0), position.get("market_cap", 0.0),
                    ),
                )

    def append_execution(self, execution: dict) -> dict:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO executions(
                    execution_id, external_id, side, code, asset_type, quantity, price, fee,
                    executed_at, source, related_plan_id, note, realized_pnl, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution["execution_id"], execution.get("external_id"), execution["side"],
                    execution["code"], execution.get("asset_type", "ETF"), execution["quantity"],
                    execution["price"], execution["fee"],
                    execution["executed_at"], execution["source"], execution.get("related_plan_id"),
                    execution.get("note"), execution.get("realized_pnl"), execution["created_at"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?",
                (execution["execution_id"],),
            ).fetchone()
        return dict(row)

    def get_execution_by_external_id(self, external_id: str) -> Optional[dict]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM executions WHERE external_id = ?", (external_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_executions(self, limit: Optional[int] = None,
                        newest_first: bool = False) -> List[dict]:
        order = "DESC" if newest_first else "ASC"
        sql = f"SELECT * FROM executions ORDER BY sequence {order}"
        params = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_cash_events(self) -> List[dict]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM cash_events ORDER BY sequence").fetchall()
        return [dict(row) for row in rows]

    def append_cash_event(self, cash_event: dict) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO cash_events(
                    cash_event_id, event_type, amount, occurred_at, source, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cash_event["cash_event_id"], cash_event["event_type"], cash_event["amount"],
                    cash_event["occurred_at"], cash_event["source"], cash_event.get("note"),
                    cash_event["created_at"],
                ),
            )
