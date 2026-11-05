import datetime as dt
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from quant_assistant import automation_trigger as trigger, scheduled_execution
from quant_assistant.execution_window import CN_TZ, execution_allowed
from quant_assistant.trading_calendar import TradingDayStatus
from test_execution_schedule import Store

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 11, 5, 2, 25, tzinfo=dt.timezone.utc)


@pytest.fixture
def commits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.check_output(['git', *args], text=True).strip()
    git('init')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Test')
    Path('.automation-triggers').mkdir()
    for session in trigger.TRIGGERS:
        Path(trigger.TRIGGERS[session]).write_text('2026-11-04T00:00:00Z\n')
    git('add', '.')
    git('commit', '-m', 'bootstrap')
    return git, git('rev-parse', 'HEAD')


@pytest.mark.parametrize('session', ['morning', 'closing'])
@pytest.mark.parametrize('extra', ['', 'code.py', '.automation-triggers/other.txt', 'other_trigger', 'delete'])
def test_complete_push_path_mapping(commits, session, extra):
    git, before = commits
    path = Path(trigger.TRIGGERS[session])
    path.write_text('2026-11-05T02:25:00.123456Z\n')
    if extra == 'delete':
        path.unlink()
    elif extra == 'other_trigger':
        Path(trigger.TRIGGERS['closing' if session == 'morning' else 'morning']).write_text('2026-11-05T02:25:00Z\n')
    elif extra:
        Path(extra).write_text('changed')
    git('add', '-A')
    git('commit', '-m', 'trigger')
    after = git('rev-parse', 'HEAD')
    assert trigger.admitted(session, before, after, NOW) is (extra == '')
    assert not trigger.admitted('closing' if session == 'morning' else 'morning', before, after, NOW)


@pytest.mark.parametrize('content,expected', [
    ('2026-11-05T02:25:00Z\n', True),
    ('2026-11-05T02:05:00+00:00\n', True),
    ('2026-11-05T02:04:59Z\n', False),
    ('2026-11-05T02:26:01Z\n', False),
    ('2026-11-04T02:25:00Z\n', False),
    ('2026-99-05T02:25:00Z\n', False),
    ('2026-11-05T02:25:00Z\nsecret=bad', False),
    ('nonce-only', False),
])
def test_timestamp_only_and_replay_age(monkeypatch, content, expected):
    mock = Mock(side_effect=['M\t.automation-triggers/morning.txt\n', content])
    monkeypatch.setattr(trigger.subprocess, 'check_output', mock)
    assert trigger.admitted('morning', 'a'*40, 'b'*40, NOW) is expected


def test_invalid_refs_never_invoke_git(monkeypatch):
    mock = Mock()
    monkeypatch.setattr(trigger.subprocess, 'check_output', mock)
    for before in ('0'*40, '--bad', ''):
        assert not trigger.admitted('morning', before, 'b'*40, NOW)
    mock.assert_not_called()


@pytest.mark.parametrize('file,session', [
    ('account-daily-telegram.yml', 'morning'), ('account-intraday-telegram.yml', 'closing')])
def test_workflow_push_and_manual_safety_contract(file, session):
    text = (ROOT / '.github/workflows' / file).read_text(encoding='utf-8')
    push = text.split('  push:', 1)[1].split('  workflow_dispatch:', 1)[0]
    assert f'paths: ["{trigger.TRIGGERS[session]}"]' in push
    assert 'branches: [main]' in push
    assert 'schedule:' not in text
    assert 'fetch-depth: 0' in text
    assert f'SESSION: {session}' in text
    assert 'python -m quant_assistant.automation_trigger' in text
    assert "needs.validate.outputs.admitted == 'true'" in text
    assert "github.event_name == 'workflow_dispatch' && inputs.dry_run == false" in text
    assert f'python -m quant_assistant.scheduled_execution {session}' in text
    assert 'group: account-cloud-state' in text
    assert 'contents: read' in text
    assert 'secrets.' not in text.split('  validate:')[1].split('  notify:')[0]
    stamp = (ROOT / trigger.TRIGGERS[session]).read_text().strip()
    assert dt.datetime.fromisoformat(stamp.replace('Z', '+00:00')).tzinfo


@pytest.mark.parametrize('session,hour,minute,allowed', [
    ('morning', 10, 24, False), ('morning', 10, 25, True),
    ('morning', 10, 45, True), ('morning', 11, 31, False),
    ('closing', 13, 49, False), ('closing', 13, 50, True),
    ('closing', 14, 10, True), ('closing', 14, 16, False)])
def test_buffer_windows_and_repeated_connector_updates(session, hour, minute, allowed):
    now = dt.datetime(2026, 11, 5, hour, minute, tzinfo=CN_TZ)
    assert execution_allowed(now, session) is allowed
    store, fetcher, action = Store(), Mock(), Mock()
    fetcher.a_share_trading_day_status.return_value = TradingDayStatus.OPEN
    assert scheduled_execution.run(session, now, store, fetcher, action) is allowed
    assert not scheduled_execution.run(session, now, store, fetcher, action)
    assert action.call_count == int(allowed)
