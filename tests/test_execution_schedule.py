import copy
import datetime as dt
from pathlib import Path
from unittest.mock import Mock

import pytest

from quant_assistant import scheduled_execution as scheduler
from quant_assistant.data.fetcher import CN_TZ, DataFetcher
from quant_assistant.trading_calendar import TradingDayStatus


class Store:
    def __init__(self):
        self.state = {"account_snapshot_b64": "unchanged", "telegram": {"next_update_id": 42},
                      "advice_records": [{"advice_session": "09:20_LEGACY"}]}

    def load(self, *_args):
        return copy.deepcopy(self.state), "sha"

    def save(self, state, *_args):
        self.state = copy.deepcopy(state)


@pytest.mark.parametrize("session,hour,minute", [("morning", 10, 30), ("closing", 14, 10)])
def test_actual_incident_day_and_redundant_events(session, hour, minute, tmp_path, monkeypatch):
    store, action = Store(), Mock()
    original = copy.deepcopy(store.state)
    fetcher = DataFetcher(tmp_path)
    monkeypatch.setattr(fetcher, "_maybe_refresh_trade_calendar", lambda: None)
    assert fetcher.a_share_trading_day_status(dt.date(2026, 10, 15)) is TradingDayStatus.OPEN
    now = dt.datetime(2026, 10, 15, hour, minute, tzinfo=CN_TZ)
    # Simulate advice persisted by the normal pipeline; completion must reload it.
    def persist():
        store.state["advice_records"].append({"advice_session": "10:30_EXECUTION"})
    action.side_effect = persist
    assert scheduler.run(session, now, store, fetcher, action)
    assert not scheduler.run(session, now + dt.timedelta(minutes=5), store, fetcher, action)
    action.assert_called_once()
    assert store.state["account_snapshot_b64"] == original["account_snapshot_b64"]
    assert store.state["telegram"] == original["telegram"]
    assert store.state["advice_records"][0] == original["advice_records"][0]
    assert len(store.state["advice_records"]) == 2
    assert store.state["execution_deliveries"][f"2026-10-15:{session}"]["status"] == "sent"


@pytest.mark.parametrize("session,hour,minute", [("morning", 10, 29), ("closing", 14, 9)])
def test_never_send_before_scheduled_time(session, hour, minute):
    store, action, fetcher = Mock(), Mock(), Mock()
    fetcher.a_share_trading_day_status.return_value = TradingDayStatus.OPEN
    assert not scheduler.run(session, dt.datetime(2026, 10, 15, hour, minute, tzinfo=CN_TZ),
                             store, fetcher, action)
    store.load.assert_not_called()
    action.assert_not_called()


@pytest.mark.parametrize("status", [TradingDayStatus.CLOSED, TradingDayStatus.UNKNOWN])
def test_nontrading_or_unknown_day_has_no_state_or_telegram_effects(status):
    store, action, fetcher = Mock(), Mock(), Mock()
    fetcher.a_share_trading_day_status.return_value = status
    assert not scheduler.run("morning", dt.datetime(2026, 10, 17, 10, 30, tzinfo=CN_TZ),
                             store, fetcher, action)
    store.load.assert_not_called()
    action.assert_not_called()


def test_ambiguous_send_failure_is_not_blindly_retried():
    store, fetcher = Store(), Mock()
    fetcher.a_share_trading_day_status.return_value = TradingDayStatus.OPEN
    action = Mock(side_effect=RuntimeError("network interrupted after send"))
    now = dt.datetime(2026, 10, 15, 10, 30, tzinfo=CN_TZ)
    with pytest.raises(RuntimeError):
        scheduler.run("morning", now, store, fetcher, action)
    with pytest.raises(RuntimeError, match="Ambiguous delivery"):
        scheduler.run("morning", now, store, fetcher, action)
    action.assert_called_once()
    assert store.state["execution_deliveries"]["2026-10-15:morning"]["status"] == "sending"


@pytest.mark.parametrize("file,primary,backup,command", [
    ("account-daily-telegram.yml", "30 2 * * 1-5", "35,40,45 2 * * 1-5", "morning"),
    ("account-intraday-telegram.yml", "10 6 * * 1-5", "15,20,25 6 * * 1-5", "closing"),
])
def test_workflow_schedule_and_shared_lock(file, primary, backup, command):
    text = (Path(__file__).resolve().parents[1] / ".github/workflows" / file).read_text(encoding="utf-8")
    assert f'cron: "{primary}"' in text
    assert f'cron: "{backup}"' in text
    assert f"python -m quant_assistant.scheduled_execution {command}" in text
    assert "group: account-cloud-state" in text
    assert "cancel-in-progress: false" in text
    assert "20 1 " not in text  # no 09:20 notification
