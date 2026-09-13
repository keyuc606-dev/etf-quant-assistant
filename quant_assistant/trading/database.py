import sqlite3
from pathlib import Path
from typing import Optional

from ..config import DATA_DIR


SCHEMA_VERSION = 1
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


class TradingDatabase:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else DEFAULT_DATABASE_PATH

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) "
            "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (SCHEMA_VERSION,),
        )
        return connection
