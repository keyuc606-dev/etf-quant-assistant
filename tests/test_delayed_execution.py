import copy
import datetime as dt
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quant_assistant import v3
from quant_assistant.data.fetcher import CN_TZ, DataFetcher
from test_execution_plans import _reports, _quote


@pytest.mark.parametrize('hour,minute', [(15, 56), (10, 30)])
def test_missing_quotes_notify_without_fabricating_baseline(tmp_path, monkeypatch, hour, minute):
    reports, _ = _reports()
    now = dt.datetime(2026, 10, 14, hour, minute, tzinfo=CN_TZ)
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = None
    # Reproduce the actual after-close fetcher guard; no network is reached.
    if hour == 15:
        assert DataFetcher().fetch_intraday_quote('000001', reports['views'][0]['position'].market, now) is None
    save = Mock()
    monkeypatch.setattr(v3, 'save_morning_advice', save)
    monkeypatch.setattr(v3, 'REPORT_DIR', tmp_path)
    path = v3.notify_morning_execution(reports, now, Mock(), fetcher,
                                     SimpleNamespace(bot_token='fake', chat_id='fake'))
    assert '降级通知' in path.read_text(encoding='utf-8')
    assert '不生成交易动作' in path.read_text(encoding='utf-8')
    assert ('收盘后' in path.read_text(encoding='utf-8')) == (hour == 15)
    save.assert_not_called()
    fetcher.send_telegram_message.assert_called_once()


def test_fresh_morning_still_saves_baseline(tmp_path, monkeypatch):
    reports, _ = _reports()
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = _quote()
    save = Mock()
    monkeypatch.setattr(v3, 'save_morning_advice', save)
    monkeypatch.setattr(v3, 'REPORT_DIR', tmp_path)
    v3.notify_morning_execution(reports, dt.datetime(2026, 9, 28, 10, 30, tzinfo=CN_TZ),
                               Mock(), fetcher, SimpleNamespace(bot_token='fake', chat_id='fake'))
    save.assert_called_once()
    assert save.call_args.kwargs['advice_session'] == '10:30_EXECUTION'


def test_fundamental_outage_still_sends_no_action_report(tmp_path, monkeypatch):
    reports, _ = _reports()
    reports['views'][0]['data_date'] = '2026-10-14'
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = {
        **_quote(100), 'as_of': '2026-10-15T10:30:00+08:00'}
    fetcher.fetch_fundamental.side_effect = RuntimeError('optional source unavailable')
    fetcher.send_telegram_message.return_value = {'ok': True, 'message_id': 123}
    fetcher.send_telegram_document.return_value = {'ok': True, 'message_id': 124}
    save = Mock()
    monkeypatch.setattr(v3, 'save_morning_advice', save)
    monkeypatch.setattr(v3, 'REPORT_DIR', tmp_path)
    path = v3.notify_morning_execution(reports, dt.datetime(2026, 10, 15, 10, 30, tzinfo=CN_TZ),
                                     Mock(), fetcher, SimpleNamespace(bot_token='fake', chat_id='fake'))
    assert '没有需要你现在处理的动作' in path.read_text(encoding='utf-8')
    fetcher.fetch_fundamental.assert_called_once()
    fetcher.send_telegram_message.assert_called_once()
    save.assert_called_once()


def test_afternoon_fresh_quotes_cannot_be_saved_as_1030_baseline(tmp_path, monkeypatch):
    reports, _ = _reports()
    fetcher, save = Mock(), Mock()
    fetcher.fetch_intraday_quote.return_value = {
        **_quote(), 'as_of': '2026-10-15T13:30:00+08:00'}
    monkeypatch.setattr(v3, 'save_morning_advice', save)
    monkeypatch.setattr(v3, 'REPORT_DIR', tmp_path)
    path = v3.notify_morning_execution(reports, dt.datetime(2026, 10, 15, 13, 30, tzinfo=CN_TZ),
                                     Mock(), fetcher, SimpleNamespace(bot_token='fake', chat_id='fake'))
    assert '超出早盘运行窗口' in path.read_text(encoding='utf-8')
    save.assert_not_called()
    fetcher.fetch_intraday_quote.assert_not_called()
    fetcher.send_telegram_message.assert_called_once()


@pytest.mark.parametrize('hour,minute', [(14, 10), (16, 0)])
def test_closing_without_quotes_preserves_legacy_account(tmp_path, monkeypatch, hour, minute):
    _, pm = _reports()
    original = {'account_snapshot_b64': 'unchanged', 'advice_records': [
        {'as_of': '2026-10-14', 'code': '000001', 'advice_session': '09:20_LEGACY'}],
        'telegram_offset': 42}
    store = Mock()
    store.load.return_value = (copy.deepcopy(original), 'sha')
    fetcher = Mock()
    fetcher.fetch_intraday_quote.return_value = None
    monkeypatch.setattr(v3, 'PortfolioManager', lambda: pm)
    monkeypatch.setattr(v3, 'TradingService', lambda: SimpleNamespace(repository=SimpleNamespace(list_executions=lambda: [])))
    monkeypatch.setattr(v3, 'REPORT_DIR', tmp_path)
    path = v3.notify_intraday(dt.datetime(2026, 10, 14, hour, minute, tzinfo=CN_TZ),
                             store, fetcher, SimpleNamespace(bot_token='fake', chat_id='fake'))
    assert ('降级通知' in path.read_text(encoding='utf-8')) == (hour == 16)
    store.save.assert_not_called()
    assert store.load.return_value[0] == original
    fetcher.send_telegram_message.assert_called_once()
