from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from typing import List, Optional

from .models import AccountReducer, AccountState


class TradingRepository(ABC):
    """成交事实存储接口；业务层不得依赖具体数据库 API。"""

    @abstractmethod
    def initialize_schema(self) -> None:
        pass

    @abstractmethod
    def get_schema_version(self) -> int:
        pass

    @abstractmethod
    def transaction(self) -> AbstractContextManager:
        pass

    @abstractmethod
    def load_opening_account(self) -> Optional[dict]:
        pass

    @abstractmethod
    def load_opening_positions(self) -> List[dict]:
        pass

    @abstractmethod
    def save_opening_snapshot(self, account: dict, positions: List[dict]) -> None:
        pass

    @abstractmethod
    def append_execution(self, execution: dict) -> dict:
        pass

    @abstractmethod
    def get_execution_by_external_id(self, external_id: str) -> Optional[dict]:
        pass

    @abstractmethod
    def list_executions(self, limit: Optional[int] = None,
                        newest_first: bool = False) -> List[dict]:
        pass

    @abstractmethod
    def list_cash_events(self) -> List[dict]:
        pass

    @abstractmethod
    def append_cash_event(self, cash_event: dict) -> None:
        pass

    def is_initialized(self) -> bool:
        return self.load_opening_account() is not None

    def rebuild_account_state(self, reducer: AccountReducer) -> AccountState:
        account = self.load_opening_account()
        if account is None:
            raise ValueError("成交台账尚未初始化")
        return reducer(
            account,
            self.load_opening_positions(),
            self.list_cash_events(),
            self.list_executions(),
        )
