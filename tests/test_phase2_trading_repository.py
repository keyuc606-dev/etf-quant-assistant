import copy
import inspect
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from quant_assistant.trading.repository import TradingRepository
from quant_assistant.trading.service import TradingService
from quant_assistant.trading.sqlite_repository import SQLiteTradingRepository

from test_phase1_trading import portfolio_data


class FakeTradingRepository(TradingRepository):
    """无数据库、无文件系统的 Repository 测试替身。"""

    def __init__(self):
        self.schema_version = 0
        self.account = None
        self.positions = []
        self.executions = []
        self.cash_events = []

    def initialize_schema(self):
        self.schema_version = 1

    def get_schema_version(self):
        return self.schema_version

    @contextmanager
    def transaction(self):
        snapshot = copy.deepcopy(
            (self.account, self.positions, self.executions, self.cash_events)
        )
        try:
            yield self
        except Exception:
            self.account, self.positions, self.executions, self.cash_events = snapshot
            raise

    def load_opening_account(self):
        return copy.deepcopy(self.account)

    def load_opening_positions(self):
        return copy.deepcopy(self.positions)

    def save_opening_snapshot(self, account, positions):
        if self.account is not None:
            raise ValueError("already initialized")
        self.account = copy.deepcopy(account)
        self.positions = copy.deepcopy(positions)

    def append_execution(self, execution):
        if any(item["execution_id"] == execution["execution_id"] for item in self.executions):
            raise ValueError("duplicate execution_id")
        external_id = execution.get("external_id")
        if external_id and self.get_execution_by_external_id(external_id):
            raise ValueError("duplicate external_id")
        saved = copy.deepcopy(execution)
        saved["sequence"] = len(self.executions) + 1
        self.executions.append(saved)
        return copy.deepcopy(saved)

    def get_execution_by_external_id(self, external_id):
        for item in self.executions:
            if item.get("external_id") == external_id:
                return copy.deepcopy(item)
        return None

    def list_executions(self, limit=None, newest_first=False):
        rows = list(reversed(self.executions)) if newest_first else list(self.executions)
        if limit is not None:
            rows = rows[:limit]
        return copy.deepcopy(rows)

    def list_cash_events(self):
        return copy.deepcopy(self.cash_events)

    def append_cash_event(self, cash_event):
        self.cash_events.append(copy.deepcopy(cash_event))


class MemoryProjectionTradingService(TradingService):
    def __init__(self, repository, projection):
        super().__init__(repository=repository, portfolio_path=Path("unused.json"))
        self.projection = copy.deepcopy(projection)
        self.backup = None

    def _read_projection(self, optional=False):
        return copy.deepcopy(self.projection)

    def _write_projection(self, data):
        self.backup = copy.deepcopy(self.projection)
        self.projection = copy.deepcopy(data)

    def _snapshot_projection_files(self):
        return copy.deepcopy(self.projection)

    def _restore_projection_files(self, snapshot):
        self.projection = copy.deepcopy(snapshot)


def opening_from_portfolio(data):
    return {
        "cash": data["cash"],
        "cash_flows": data["cash_flows"],
        "created_at": "2026-09-13T00:00:00+00:00",
    }, data["positions"]


def execution(execution_id, external_id=None, price=4.0):
    return {
        "execution_id": execution_id,
        "external_id": external_id,
        "side": "BUY",
        "code": "510300",
        "quantity": 100,
        "price": price,
        "fee": 1.0,
        "executed_at": "2026-09-13T01:00:00+00:00",
        "source": "test",
        "related_plan_id": None,
        "note": None,
        "realized_pnl": None,
        "created_at": "2026-09-13T01:00:01+00:00",
    }


class TradingRepositoryTest(unittest.TestCase):
    def test_service_uses_fake_repository_without_sqlite_or_filesystem(self):
        repository = FakeTradingRepository()
        repository.initialize_schema()
        projection = portfolio_data()
        account, positions = opening_from_portfolio(projection)
        repository.save_opening_snapshot(account, positions)
        service = MemoryProjectionTradingService(repository, projection)

        result = service.record_trade("BUY", "510300", 100, 4.0, fee=1.0)

        self.assertEqual(result.quantity, 1100)
        self.assertAlmostEqual(result.cash, 9599.0)
        self.assertEqual(result.execution["sequence"], 1)
        self.assertEqual(len(repository.executions), 1)
        self.assertEqual(service.projection["positions"][0]["shares"], 1100)

    def test_service_source_does_not_import_or_call_sqlite3(self):
        source = inspect.getsource(TradingService)

        self.assertNotIn("sqlite3", source)
        self.assertNotIn("SELECT ", source)
        self.assertNotIn("INSERT ", source)
        self.assertNotIn("BEGIN ", source)

    def test_fake_repository_transaction_rolls_back(self):
        repository = FakeTradingRepository()
        repository.initialize_schema()
        account, positions = opening_from_portfolio(portfolio_data())
        repository.save_opening_snapshot(account, positions)

        with self.assertRaisesRegex(RuntimeError, "stop"):
            with repository.transaction():
                repository.append_execution(execution("exec-1"))
                raise RuntimeError("stop")

        self.assertEqual(repository.list_executions(), [])

    def test_sqlite_repository_initializes_opening_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = SQLiteTradingRepository(Path(temp_dir) / "ledger.sqlite3")
            account, positions = opening_from_portfolio(portfolio_data())
            with repository.transaction():
                repository.save_opening_snapshot(account, positions)

            self.assertEqual(repository.get_schema_version(), 2)
            self.assertEqual(repository.load_opening_account()["cash"], 10000.0)
            self.assertEqual(repository.load_opening_positions()[0]["shares"], 1000)

    def test_sqlite_repository_execution_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = SQLiteTradingRepository(Path(temp_dir) / "ledger.sqlite3")
            with repository.transaction():
                repository.append_execution(execution("exec-1"))
                repository.append_execution(execution("exec-2", price=4.1))

            rows = repository.list_executions(limit=1, newest_first=True)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["execution_id"], "exec-2")

    def test_sqlite_repository_external_id_is_unique(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = SQLiteTradingRepository(Path(temp_dir) / "ledger.sqlite3")
            repository.append_execution(execution("exec-1", external_id="broker-1"))

            with self.assertRaises(sqlite3.IntegrityError):
                repository.append_execution(execution("exec-2", external_id="broker-1"))

    def test_sqlite_repository_transaction_rolls_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = SQLiteTradingRepository(Path(temp_dir) / "ledger.sqlite3")
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with repository.transaction():
                    repository.append_execution(execution("exec-1"))
                    raise RuntimeError("stop")

            self.assertEqual(repository.list_executions(), [])

    def test_current_database_reopens_without_reinitialization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.sqlite3"
            repository = SQLiteTradingRepository(path)
            account, positions = opening_from_portfolio(portfolio_data())
            with repository.transaction():
                repository.save_opening_snapshot(account, positions)
                repository.append_execution(execution("legacy-exec", "legacy-ext"))
            before_version = repository.get_schema_version()

            reopened = SQLiteTradingRepository(path)

            self.assertEqual(before_version, 2)
            self.assertEqual(reopened.get_schema_version(), 2)
            self.assertEqual(reopened.get_execution_by_external_id("legacy-ext")["execution_id"], "legacy-exec")


if __name__ == "__main__":
    unittest.main()
