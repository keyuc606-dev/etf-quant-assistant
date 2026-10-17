import datetime as dt
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quant_assistant import v3, scheduled_execution
from quant_assistant.execution_window import CN_TZ, execution_allowed
from quant_assistant.trading_calendar import TradingDayStatus
from test_execution_plans import _reports, _quote


def at(hour, minute, second=0):
    return dt.datetime(2026, 10, 16, hour, minute, second, tzinfo=CN_TZ)


@pytest.mark.parametrize('now,session,allowed', [
    (at(10, 30), 'morning', True), (at(14, 0), 'closing', True),
    (at(14, 15), 'closing', True), (at(14, 15, 1), 'closing', False),
    (at(15, 0), 'closing', False), (at(13, 30), 'morning', False),
])
def test_beijing_windows_independent_of_runner_timezone(now, session, allowed):
    assert execution_allowed(now.astimezone(dt.timezone.utc), session) is allowed


def test_late_scheduler_never_touches_formal_state_or_sends(capsys):
    store, fetcher, action = Mock(), Mock(), Mock()
    fetcher.a_share_trading_day_status.return_value = TradingDayStatus.OPEN
    assert not scheduled_execution.run('closing', at(14, 15, 1), store, fetcher, action)
    store.load.assert_not_called()
    action.assert_not_called()
    assert 'execution cutoff exceeded' in capsys.readouterr().out


def test_after_close_generation_is_review_only_and_no_network():
    reports, pm = _reports()
    fetcher = Mock()
    for build in (lambda: v3.build_morning_execution(reports, fetcher, at(15, 30)),
                  lambda: v3.build_closing_execution(pm, [], fetcher, at(15, 30))):
        text = build()
        assert 'REVIEW' in text and '下一交易日' in text
        assert not any(action in text for action in ('ADD', 'REDUCE', 'RISK_EXIT'))
    assert not fetcher.mock_calls


@pytest.mark.parametrize('age,quality', [(300, 'HIGH'), (301, 'LOW'), (-31, 'LOW'), (900, 'LOW')])
def test_quote_age_and_future_clock_guard(age, quality):
    quote = {**_quote(), 'as_of': (at(10, 30) - dt.timedelta(seconds=age)).isoformat()}
    assert v3._execution_data_quality('HIGH', quote, at(10, 30)) == quality


def test_generation_crossing_cutoff_cannot_persist_or_send(monkeypatch):
    store, fetcher = Mock(), Mock()
    def build(*args):
        args[4].update(fresh_provisional=True, morning_advice=True, degraded=False,
                       closing_observations=[{'advice_id': 'must-not-save'}])
        return 'REDUCE'
    monkeypatch.setattr(v3, 'build_closing_execution', build)
    monkeypatch.setattr(v3, 'PortfolioManager', Mock())
    monkeypatch.setattr(v3, 'TradingService', Mock())
    store.load.return_value = ({}, 'sha')
    times = iter([at(14, 14), at(14, 15, 1)])
    with pytest.raises(RuntimeError, match='execution cutoff exceeded'):
        v3.notify_intraday(store=store, fetcher=fetcher, clock=lambda: next(times))
    store.save.assert_not_called()
    fetcher.send_telegram_message.assert_not_called()


def test_quote_ages_during_generation_fail_closed():
    with pytest.raises(RuntimeError, match='stale quote'):
        v3._require_current_quotes({'quote_times': [at(14, 0).isoformat()]}, at(14, 6))


def test_closing_health_without_baseline_still_exposes_verified_quote_timestamp():
    _, pm = _reports()
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = {**_quote(), 'as_of': at(14, 0).isoformat()}
    quality = {}
    text = v3.build_closing_execution(pm, [], fetcher, at(14, 0), quality)
    assert '14:00:00' in text and '无10:30基准' in text
    assert '没有需要你现在处理的动作' in text
    assert quality['fresh_provisional']
    assert quality['closing_observations'] == []


def test_formal_morning_write_cannot_bypass_cutoff():
    store = Mock()
    with pytest.raises(RuntimeError, match='execution cutoff exceeded'):
        v3.save_morning_advice({}, at(15, 30), store, advice_session='10:30_EXECUTION')
    store.load.assert_not_called()


def test_primary_and_manual_disaster_race_uses_state_sha_not_github_concurrency():
    class CASStore:
        def __init__(self):
            self.state, self.version, self.loads = {}, 0, 0
            self.lock, self.barrier = threading.Lock(), threading.Barrier(2)

        def load(self, *_args):
            with self.lock:
                self.loads += 1
                initial = self.loads <= 2
                value = copy.deepcopy(self.state), self.version
            if initial:
                self.barrier.wait(timeout=5)
            return value

        def save(self, state, sha, *_args):
            with self.lock:
                if sha != self.version:
                    raise RuntimeError('409 SHA conflict')
                self.state, self.version = copy.deepcopy(state), self.version + 1

    store, fetcher, action = CASStore(), Mock(), Mock()
    fetcher.a_share_trading_day_status.return_value = TradingDayStatus.OPEN

    def invoke():
        try:
            return scheduled_execution.run('closing', at(14, 0), store, fetcher, action)
        except RuntimeError as error:
            assert '409 SHA conflict' in str(error)
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: invoke(), range(2)))
    assert sorted(outcomes) == [False, True]
    action.assert_called_once()
    assert store.state['execution_deliveries']['2026-10-16:closing']['status'] == 'sent'


def test_skipped_action_never_claims_telegram_sent():
    from test_execution_schedule import Store
    store, fetcher = Store(), Mock()
    fetcher.a_share_trading_day_status.return_value = TradingDayStatus.OPEN
    assert not scheduled_execution.run('closing', at(14, 0), store, fetcher, lambda: False)
    assert store.state['execution_deliveries']['2026-10-16:closing']['status'] == 'skipped'
    assert not scheduled_execution.run('closing', at(14, 5), store, fetcher, Mock())
