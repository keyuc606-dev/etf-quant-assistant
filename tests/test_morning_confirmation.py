import copy
import datetime as dt
from types import SimpleNamespace

import pytest

from quant_assistant.analysis.market_filter import execution_evidence, DIAGNOSTIC_FIELDS
from quant_assistant.analysis.final_decision import build_intraday_final_decision
from quant_assistant.advice_performance import make_records
from quant_assistant.v3 import compact_execution, build_morning_execution, build_closing_execution
from quant_assistant.monthly_review import build_monthly_review, render_markdown
from test_market_filter import scenario, decide, quote, frame, NOW, BENCHMARKS, reports


def test_morning_rebound_without_support_break_defers_mechanical_sale():
    result = decide(scenario(bench=101, low=95))
    assert result['final_action'] == 'HOLD'
    assert result['suggested_quantity'] == 0
    assert result['conditional_quantity'] > 0
    assert result['confirmation_state'] == 'DEFERRED_REBOUND'
    assert result['decision_confidence'] != 'HIGH'
    assert result['rebound_from_low_pct'] == pytest.approx(97 / 95 - 1)
    assert '11:00' in result['trigger_condition']
    assert '14:10' in result['trigger_condition']


def test_low_and_support_break_is_conditional_without_elapsed_observations():
    result = decide(scenario(price=94, bench=97))
    assert result['final_action'] == 'HOLD'
    assert result['confirmation_state'] == 'WAIT_CONFIRMATION'
    assert result['weakness_persistence'] == 'UNKNOWN'


def test_verified_elapsed_weakness_can_confirm_morning_reduce():
    values = scenario(price=94, bench=97, record={'morning_observation': {
        'signal_as_of': NOW.replace(hour=10, minute=0).isoformat(),
        'trend_break_candidate': True, 'support_break': True, 'relative_return_day': -.02}})
    result = decide(values)
    assert result['weakness_persistence'] == 'CONFIRMED'
    assert result['relative_deteriorating'] is True
    assert result['final_action'] == 'REDUCE'
    assert result['decision_confidence'] == 'MEDIUM'


@pytest.mark.parametrize('stamp', [NOW.replace(minute=15), NOW - dt.timedelta(days=1), NOW + dt.timedelta(minutes=5)])
def test_short_stale_or_future_observation_never_proves_persistence(stamp):
    values = scenario(price=94, bench=97, record={'morning_observation': {
        'signal_as_of': stamp.isoformat(), 'trend_break_candidate': True,
        'support_break': True, 'relative_return_day': -.02}})
    assert decide(values)['final_action'] == 'HOLD'


def test_invalidation_exit_is_immediate_even_with_rebound():
    result = decide(scenario(price=89, low=85, bench=97))
    assert result['final_action'] == 'RISK_EXIT'
    assert result['suggested_quantity'] == 1000
    assert result['confirmation_state'] == 'RISK_EXIT_IMMEDIATE'


@pytest.mark.parametrize('hour,minute,expected', [(9, 40, 'LOW'), (10, 30, 'MEDIUM'), (14, 10, 'HIGH')])
def test_time_confidence_uses_china_time(hour, minute, expected):
    pos, record, q, ma, _ = scenario()
    now = NOW.replace(hour=hour, minute=minute)
    q['as_of'] = now.isoformat()
    evidence = execution_evidence(pos, record, q, ma, now=now.astimezone(dt.timezone.utc))
    assert evidence['time_of_day_confidence'] == expected


@pytest.mark.parametrize('price,expected,state', [(101, 'HOLD', 'CANCELLED_BY_REBOUND'), (94, 'REDUCE', 'CONFIRMED')])
def test_afternoon_independently_confirms_or_cancels(price, expected, state):
    pos, record, _, ma, _ = scenario()
    record.update(morning_reduce_candidate=True)
    later = NOW.replace(hour=14, minute=10)
    q = quote(price, now=later)
    evidence = execution_evidence(pos, record, q, ma, {'market_regime': 'RANGE'},
        {code: frame() for code in BENCHMARKS}, {code: quote(99, now=later) for code in BENCHMARKS}, later)
    result = build_intraday_final_decision(pos, record, q, '未触发', {} if price > 100 else ma, evidence)
    assert result['final_action'] == expected
    assert result['confirmation_state'] == state


def test_rebound_protection_does_not_grant_add():
    result = decide(scenario(bench=101, low=95), total_assets=1000000, cash=500000, equity_exposure=.1)
    assert result['rebound_protected']
    assert result['final_action'] != 'ADD'


def test_missing_or_impossible_low_is_unknown():
    for low in (None, 0, 100):
        result = decide(scenario(price=94, low=low))
        assert result['rebound_from_low_pct'] is None
        assert result['rebound_protected'] is False


def test_compact_reports_cap_three_four_lines_no_tables_and_audit_full():
    report = reports()
    fetcher = SimpleNamespace(fetch_hist=lambda *a, **kw: frame(),
        fetch_intraday_quote=lambda code, market, now: quote(97 if code in BENCHMARKS else 94, now=now))
    detail = []
    text = build_morning_execution(report, fetcher, NOW, detail=detail)
    assert text.count('结论：') == 3
    assert '|' not in text and 'REL_WEAK' not in text and 'MA20' not in text
    assert '另有3项候选' in text
    blocks = text.split('\n1. ')[1].split('其余')[0]
    assert len(blocks.splitlines()) == 12
    assert all(len(line) <= 45 for line in text.splitlines() if line.startswith('原因：'))
    assert all(key in '\n'.join(detail) for key in DIAGNOSTIC_FIELDS)
    records = make_records(report, NOW, 'test', '10:30_EXECUTION')
    assert records[0]['morning_observation']['signal_price'] == 94
    assert records[0]['morning_reduce_candidate']
    before = copy.deepcopy(records)
    pm = SimpleNamespace(positions=[v['position'] for v in report['views']], cash=1000000)
    quality = {}
    text = build_closing_execution(pm, records, fetcher, NOW.replace(hour=14, minute=10), quality)
    assert text.count('结论：') == 3 and '|' not in text
    assert len(quality['closing_observations']) == 6
    assert records == before


def test_monthly_legacy_and_observed_metrics_never_invent_minutes_or_fills():
    record = {'advice_id': 'one', 'as_of': str(NOW.date()), 'code': '159160',
        'reference_close': .72, 'final_action': 'HOLD', 'rule_version': 'test',
        'morning_confirmation_version': 'v1', 'morning_reduce_candidate': True,
        'time_of_day_confidence': 'MEDIUM', 'confirmation_state': 'DEFERRED_REBOUND',
        'signal_price': .72, 'signal_as_of': NOW.isoformat()}
    closing = {'advice_id': 'one', 'price': .74, 'observed_at': NOW.replace(hour=14, minute=10).isoformat(),
        'confirmation_state': 'CANCELLED_BY_REBOUND'}
    executions = [{'related_plan_id': 'one', 'side': 'SELL', 'quantity': 100,
        'price': .72, 'fee': 1, 'executed_at': NOW.replace(hour=11, minute=0).isoformat()}]
    legacy = {'advice_id': 'legacy', 'as_of': str(NOW.date()), 'code': '510300', 'reference_close': 4}
    report = build_monthly_review([record, legacy], executions, {}, '2026-10', NOW.date(), [closing])
    metrics = report['morning_confirmation']
    assert metrics['morning_reduce_candidate_count'] == 1
    assert metrics['morning_reduce_executed_count'] == 1
    assert metrics['morning_reduce_cancelled_by_rebound'] == 1
    assert metrics['morning_to_1410_return'] == pytest.approx(.74 / .72 - 1)
    assert metrics['morning_sell_opportunity_cost_to_1410'] == pytest.approx(3)
    assert metrics['rebound_after_morning_signal_30m'] is None
    assert metrics['rebound_after_morning_signal_60m'] is None
    assert metrics['legacy_count'] == 1
    assert report['rows'][1]['time_of_day_confidence'] is None
    assert 'morning_reduce_candidate_count' in render_markdown(report)
    assert 'confirmation_state' not in legacy


def test_monthly_snapshots_require_correct_time_and_explicit_trade_link():
    record = {'advice_id': 'one', 'as_of': str(NOW.date()), 'code': '159160',
        'reference_close': .72, 'rule_version': 'test', 'morning_reduce_candidate': True,
        'signal_price': .72, 'signal_as_of': NOW.isoformat(), 'intraday_followups': {
            '30': {'as_of': (NOW + dt.timedelta(minutes=30)).isoformat(), 'price': .73},
            '60': {'as_of': (NOW + dt.timedelta(minutes=40)).isoformat(), 'price': .75}}}
    report = build_monthly_review([record], [{'side': 'SELL', 'quantity': 100, 'price': .72}], {}, '2026-10', NOW.date())
    row = report['rows'][0]
    assert row['rebound_after_morning_signal_30m'] == pytest.approx(.73 / .72 - 1)
    assert row['rebound_after_morning_signal_60m'] is None
    assert row['morning_reduce_executed_count'] == 0
    assert row['morning_sell_avoided_loss_to_1410'] is None


@pytest.mark.parametrize('corrupt', [False, True])
def test_closing_audit_is_read_back_before_send_and_retry_preserves_first(tmp_path, monkeypatch, corrupt):
    from unittest.mock import Mock
    from quant_assistant import v3
    first = {'advice_id': 'one', 'price': .74}
    class Store:
        state = {'advice_records': [{'advice_id': 'one'}]}
        saves = 0
        def load(self, *args):
            state = copy.deepcopy(self.state)
            if corrupt and self.saves:
                state['closing_observations'] = []
            return state, 'sha'
        def save(self, state, sha, message):
            self.state = copy.deepcopy(state)
            self.saves += 1
    store = Store()
    def build(pm, records, fetcher, now, quality, executions, detail):
        quality.update(morning_advice=True, fresh_provisional=True, degraded=False,
                       closing_observations=[first])
        return '【14:10 尾盘策略】'
    monkeypatch.setattr(v3, 'build_closing_execution', build)
    monkeypatch.setattr(v3, 'PortfolioManager', lambda: None)
    monkeypatch.setattr(v3, 'TradingService', lambda: SimpleNamespace(repository=SimpleNamespace(list_executions=lambda: [])))
    monkeypatch.setattr(v3, 'REPORT_DIR', tmp_path)
    fetcher = Mock()
    notifier = SimpleNamespace(bot_token='fake', chat_id='fake')
    if corrupt:
        with pytest.raises(RuntimeError, match='回读失败'):
            v3.notify_intraday(NOW.replace(hour=14, minute=10), store, fetcher, notifier)
        fetcher.send_telegram_message.assert_not_called()
    else:
        v3.notify_intraday(NOW.replace(hour=14, minute=10), store, fetcher, notifier)
        assert store.state['closing_observations'] == [first]
        assert store.state['advice_records'] == [{'advice_id': 'one'}]
        first = {'advice_id': 'one', 'price': .75}
        v3.notify_intraday(NOW.replace(hour=14, minute=11), store, fetcher, notifier)
        assert store.saves == 1
        assert store.state['closing_observations'][0]['price'] == .74
