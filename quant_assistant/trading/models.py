from dataclasses import dataclass
from typing import Callable, Dict, List


AccountState = Dict[str, object]
AccountReducer = Callable[[dict, List[dict], List[dict], List[dict]], AccountState]


@dataclass(frozen=True)
class OpeningSnapshot:
    account: dict
    positions: List[dict]


@dataclass
class TradeResult:
    execution: dict
    cash: float
    quantity: int
    average_cost: float
    duplicate: bool = False
