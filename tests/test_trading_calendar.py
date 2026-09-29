import datetime as dt
from types import SimpleNamespace

import quant_assistant.data.fetcher as fetcher_module
from quant_assistant.__main__ import (
    _notification_trading_day_gate, cmd_notify_intraday, cmd_notify_morning_execution,
)
from quant_assistant.data.fetcher import CN_TZ, DataFetcher
from quant_assistant.trading_calendar import (
    TradingDayStatus, annual_trade_dates, is_a_share_trading_day,
    is_last_trading_day_of_month,
)


def test_2026_exchange_calendar_regressions():
    dates = annual_trade_dates(2026)
    assert is_a_share_trading_day(dt.date(2026, 9, 24), dates)
    assert not is_a_share_trading_day(dt.date(2026, 9, 20), dates)  # weekend
    assert not is_a_share_trading_day(dt.date(2026, 9, 25), dates)  # Mid-Autumn Friday
    assert is_a_share_trading_day(dt.date(2026, 9, 28), dates)      # first session after holiday
    assert not is_a_share_trading_day(dt.date(2026, 10, 10), dates)  # make-up work Saturday
    assert not is_a_share_trading_day(dt.date(2026, 10, 1), dates)
    assert is_a_share_trading_day(dt.date(2026, 10, 8), dates)


def test_last_trading_day_of_month_uses_exchange_sessions():
    dates = annual_trade_dates(2026)
    assert is_last_trading_day_of_month(dt.date(2026, 9, 30), dates)
    assert not is_last_trading_day_of_month(dt.date(2026, 9, 29), dates)


def test_data_source_failure_uses_cache_then_fails_closed(monkeypatch, tmp_path):
    fetcher = DataFetcher(tmp_path)
    cached = {dt.date(2027, 1, 4)}
    monkeypatch.setattr(fetcher_module, "_cached_trade_date_objects", lambda: cached)
    monkeypatch.setattr(fetcher, "_maybe_refresh_trade_calendar", lambda: None)
    assert fetcher.a_share_trading_day_status(dt.date(2027, 1, 4)) is TradingDayStatus.OPEN

    cached.clear()
    assert fetcher.a_share_trading_day_status(dt.date(2027, 1, 4)) is TradingDayStatus.UNKNOWN


def test_notification_gate_logs_closed_and_unknown_without_notifying(capsys):
    closed = SimpleNamespace(a_share_trading_day_status=lambda _day: TradingDayStatus.CLOSED)
    unknown = SimpleNamespace(a_share_trading_day_status=lambda _day: TradingDayStatus.UNKNOWN)
    now = dt.datetime(2026, 9, 25, 10, 30, tzinfo=CN_TZ)
    assert not _notification_trading_day_gate("10:30", now, closed)
    assert "休市，静默跳过 10:30 推送" in capsys.readouterr().out
    assert not _notification_trading_day_gate("14:35", now, unknown)
    assert "交易日状态未知，静默跳过 14:35 推送" in capsys.readouterr().out


def test_closed_day_commands_exit_before_reports_state_or_telegram(monkeypatch):
    monkeypatch.setattr("quant_assistant.__main__._notification_trading_day_gate",
                        lambda _session: False)
    monkeypatch.setattr("quant_assistant.__main__.cmd_daily",
                        lambda _args: (_ for _ in ()).throw(AssertionError("report generated")))
    monkeypatch.setattr("quant_assistant.v3.notify_morning_execution",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("notified")))
    monkeypatch.setattr("quant_assistant.v3.notify_intraday",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("notified")))
    cmd_notify_morning_execution(SimpleNamespace(days=120))
    cmd_notify_intraday(SimpleNamespace())


def test_open_day_gate_continues():
    fetcher = SimpleNamespace(a_share_trading_day_status=lambda _day: TradingDayStatus.OPEN)
    now = dt.datetime(2026, 9, 28, 10, 30, tzinfo=CN_TZ)
    assert _notification_trading_day_gate("10:30", now, fetcher)


def test_open_day_intraday_command_calls_normal_pipeline(monkeypatch):
    called = []
    monkeypatch.setattr("quant_assistant.__main__._notification_trading_day_gate",
                        lambda _session: True)
    monkeypatch.setattr("quant_assistant.v3.notify_intraday",
                        lambda: called.append(True) or "report.md")
    cmd_notify_intraday(SimpleNamespace())
    assert called == [True]
