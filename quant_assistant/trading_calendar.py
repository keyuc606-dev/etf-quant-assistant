"""Offline, auditable A-share exchange calendar semantics.

The runtime cache contains confirmed *open* sessions.  Bundled annual closure
plans keep the notification gate useful in a fresh CI checkout.  A date outside
both known ranges is deliberately UNKNOWN: notification code must fail closed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class TradingDayStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AnnualClosurePlan:
    year: int
    closures: tuple[tuple[dt.date, dt.date], ...]
    source: str


# Shanghai Stock Exchange annual closure notices.  Weekend make-up workdays are
# intentionally not special-cased: Chinese exchanges remain closed on weekends.
_PLANS = {
    2024: AnnualClosurePlan(
        2024,
        (
            (dt.date(2024, 1, 1), dt.date(2024, 1, 1)),
            (dt.date(2024, 2, 9), dt.date(2024, 2, 18)),
            (dt.date(2024, 4, 4), dt.date(2024, 4, 6)),
            (dt.date(2024, 5, 1), dt.date(2024, 5, 5)),
            (dt.date(2024, 6, 8), dt.date(2024, 6, 10)),
            (dt.date(2024, 9, 15), dt.date(2024, 9, 17)),
            (dt.date(2024, 10, 1), dt.date(2024, 10, 7)),
        ),
        "SSE annual closure notice (2023-12-26), https://www.sse.com.cn/disclosure/dealinstruc/closed/",
    ),
    2025: AnnualClosurePlan(
        2025,
        (
            (dt.date(2025, 1, 1), dt.date(2025, 1, 1)),
            (dt.date(2025, 1, 28), dt.date(2025, 2, 4)),
            (dt.date(2025, 4, 4), dt.date(2025, 4, 6)),
            (dt.date(2025, 5, 1), dt.date(2025, 5, 5)),
            (dt.date(2025, 5, 31), dt.date(2025, 6, 2)),
            (dt.date(2025, 10, 1), dt.date(2025, 10, 8)),
        ),
        "SSE annual closure notice (2024-12-23), https://www.sse.com.cn/disclosure/dealinstruc/closed/",
    ),
    2026: AnnualClosurePlan(
        2026,
        (
            (dt.date(2026, 1, 1), dt.date(2026, 1, 3)),
            (dt.date(2026, 2, 15), dt.date(2026, 2, 23)),
            (dt.date(2026, 4, 4), dt.date(2026, 4, 6)),
            (dt.date(2026, 5, 1), dt.date(2026, 5, 5)),
            (dt.date(2026, 6, 19), dt.date(2026, 6, 21)),
            (dt.date(2026, 9, 25), dt.date(2026, 9, 27)),
            (dt.date(2026, 10, 1), dt.date(2026, 10, 7)),
        ),
        "SSE notices 2025-45 and 2026-22, https://www.sse.com.cn/disclosure/announcement/general/c/c_20260915_10832273.shtml",
    ),
}


def annual_trade_dates(year: int) -> set[dt.date] | None:
    """Return every confirmed open session for a bundled complete year."""
    plan = _PLANS.get(year)
    if plan is None:
        return None
    day = dt.date(year, 1, 1)
    end = dt.date(year, 12, 31)
    result = set()
    while day <= end:
        closed = day.weekday() >= 5 or any(start <= day <= stop for start, stop in plan.closures)
        if not closed:
            result.add(day)
        day += dt.timedelta(days=1)
    return result


def is_a_share_trading_day(day: dt.date, trading_dates: Iterable[dt.date]) -> bool:
    """Pure membership function shared by notification and review code."""
    return day in set(trading_dates)


def status_from_dates(day: dt.date, trading_dates: Iterable[dt.date]) -> TradingDayStatus:
    dates = set(trading_dates)
    if not dates or day < min(dates) or day > max(dates):
        return TradingDayStatus.UNKNOWN
    return TradingDayStatus.OPEN if is_a_share_trading_day(day, dates) else TradingDayStatus.CLOSED


def bundled_status(day: dt.date) -> TradingDayStatus:
    dates = annual_trade_dates(day.year)
    if dates is None:
        return TradingDayStatus.UNKNOWN
    return TradingDayStatus.OPEN if day in dates else TradingDayStatus.CLOSED


def combined_trade_dates(year: int, cached_dates: Iterable[dt.date] = ()) -> set[dt.date]:
    """Use complete bundled year plus any audited runtime-cache sessions."""
    bundled = annual_trade_dates(year) or set()
    return bundled | {day for day in cached_dates if day.year == year}


def is_last_trading_day_of_month(day: dt.date, trading_dates: Iterable[dt.date]) -> bool:
    dates = set(trading_dates)
    return day in dates and not any(
        candidate.year == day.year and candidate.month == day.month and candidate > day
        for candidate in dates
    )


def bundled_calendar_sources() -> dict[int, str]:
    return {year: plan.source for year, plan in sorted(_PLANS.items())}
