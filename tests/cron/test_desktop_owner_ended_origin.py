"""Ended Desktop-owner receipts converge without loading the origin's UI session."""
from contextlib import contextmanager

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from cron import delivery_queue, desktop_origin as route, scheduler, scheduler_tick
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from tools import async_delegation as ledger


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = time.time()
    monkeypatch.setattr(route, 'time', SimpleNamespace(time=lambda: now))
    return now


@pytest.fixture
def owner():
    db = SessionDB(get_hermes_home() / 'state.db')
    db.create_session('owner', 'desktop', user_id='alice')
    job = {'id': 'native', 'name': 'native goal', 'execution_id': 'run-1', 'deliver': 'origin',
           'origin': {'platform': route.PLATFORM, 'chat_id': 'owner',
                      'profile_home': str(get_hermes_home().resolve()), 'principal_id': 'alice'}}
    yield db, job
    db.close()


@pytest.fixture
def matrix_sender(monkeypatch):
    from gateway.config import Platform
    config = MagicMock()
    config.platforms = {Platform.MATRIX: SimpleNamespace(enabled=True)}
    monkeypatch.setattr('gateway.config.load_gateway_config', lambda: config)
    monkeypatch.setattr(scheduler, 'load_config', lambda: {'cron': {'wrap_response': False}})
    send = AsyncMock(return_value={'success': True})
    monkeypatch.setattr('gateway.outbound.send._send_to_platform', send)
    return send


def record(key):
    with ledger._transaction() as conn:
        row = conn.execute('SELECT delivery_state,result_json FROM async_delegations WHERE delegation_id=?', (key,)).fetchone()
    return row[0], json.loads(row[1])['owner_followthrough']


def idle_tick_housekeeping(monkeypatch):
    from cron import bot_chat_legacy, scheduler_authority
    monkeypatch.setattr(bot_chat_legacy, 'drain_legacy_pending', lambda: None)
    monkeypatch.setattr(scheduler_authority, 'reconcile_pending', lambda **kw: None)
    scheduler_tick._pre_dispatch_housekeeping(SimpleNamespace(_maybe_reap_dead_owners=lambda: None), False)


def end(db, now, age, reason='startup_orphan_reap', sid='owner'):
    db._write_sql('UPDATE sessions SET ended_at=?,end_reason=? WHERE id=?', (now - age, reason, sid))


def deliver_reopened(db, job, key):
    @contextmanager
    def database(_session):
        yield db
    server = SimpleNamespace(_session_db=database, _session_home=lambda session: get_hermes_home(),
                             _session_auth_user_id=lambda session: 'alice')
    session = {'session_key': 'owner', 'agent': SimpleNamespace(session_id='owner')}
    db.reopen_session('owner')
    events = route.pending_for_session(server, session)
    assert [e['delegation_id'] for e in events] == [key]
    claim = ledger.claim_event_delivery(events[0], 'reopened-owner')
    assert claim and route.begin_followthrough(events[0], claim)
    route.settle_followthrough(events[0], claim, {'status': 'settled', 'text': 'owner received result'})
    assert record(key)[0] == 'delivered'


@pytest.mark.parametrize('reason', ['startup_orphan_reap', 'agent_close', 'ws_orphan_reap',
                                    'superseded_by_resume', 'user'])
def test_recent_end_stays_pending_and_reopen_delivers(owner, clock, reason):
    db, job = owner
    key = route.publish_completion(job, 'result')['delivery_id']
    end(db, clock, 6 * 86400, reason)
    assert route.sweep_ended_origin_completions() == 0
    assert record(key) == ('pending', {'status': 'pending'})
    deliver_reopened(db, job, key)


@pytest.mark.parametrize('ending', ['startup_orphan_reap', 'user', 'expiry', 'missing', 'identity'])
def test_aged_or_missing_origin_settles_from_periodic_hook_and_compacts(owner, monkeypatch, clock, ending):
    db, job = owner
    key = route.publish_completion(job, 'result')['delivery_id']
    if ending == 'missing':
        db._write_sql('DELETE FROM sessions WHERE id=?', ('owner',))
    elif ending == 'identity':
        db._write_sql('UPDATE sessions SET user_id=? WHERE id=?', ('someone-else', 'owner'))
    else:
        end(db, clock, 7 * 86400, ending)
        idle_tick_housekeeping(monkeypatch)
        assert record(key)[0] == 'pending'  # Strictly older than the grace, not at its boundary.
        end(db, clock, 7 * 86400 + 10, ending)
        if ending == 'expiry':
            db._write_sql('UPDATE sessions SET expiry_finalized=1 WHERE id=?', ('owner',))
    idle_tick_housekeeping(monkeypatch)
    state, reason = record(key)
    assert state == 'dropped'
    assert reason['status'] == 'dropped' and reason['finished_at'] == clock
    assert reason['reason'] == ('origin_identity_gone' if ending in ('missing', 'identity') else 'origin_ended')
    assert route.sweep_ended_origin_completions() == 0
    with ledger._transaction() as conn:
        route.compact_completion_history(conn, clock + 1, keep=0)
        assert set(json.loads(conn.execute('SELECT event_json FROM async_delegations WHERE delegation_id=?', (key,)).fetchone()[0])) == {'payload_sha256'}
    assert route.publish_completion(job, 'result')['status'] == 'dropped'


@pytest.mark.parametrize('ended_before_publish', [False, True])
def test_failure_alert_once_while_pending_then_drop_without_second_enqueue(owner, monkeypatch, matrix_sender, clock, ended_before_publish):
    db, job = owner
    if ended_before_publish:
        end(db, clock, 3600 + 10)
    receipt = route.publish_completion(job, 'worker failed', for_failure=True)
    assert receipt['status'] == 'pending'
    key = receipt['delivery_id']
    end(db, clock, 3600 + 10)
    enqueue = Mock(wraps=delivery_queue.enqueue)
    monkeypatch.setattr(delivery_queue, 'enqueue', enqueue)
    idle_tick_housekeeping(monkeypatch)
    assert record(key) == ('pending', {'status': 'pending', 'alert': 'pending'})
    assert route.sweep_ended_origin_completions() == 0
    assert enqueue.call_count == 1
    assert scheduler.drain_delivery_queue({}, None) == 1
    assert delivery_queue.get_status(key + '-origin-ended-alert')['status'] == 'delivered'
    platform, _config, chat_id, content = matrix_sender.call_args.args
    assert platform.value == 'matrix' and chat_id == '!ZudlWNSxyHvqGwIkdv:sppun.com'
    assert key in content and 'native goal' in content and 'worker failed' in content
    end(db, clock, 7 * 86400 + 10)
    assert route.sweep_ended_origin_completions() == 1
    assert record(key)[0] == 'dropped' and record(key)[1]['alert'] == 'pending'
    assert enqueue.call_count == 1
    assert route.publish_completion(job, 'worker failed', for_failure=True)['status'] == 'dropped'
    assert route.sweep_ended_origin_completions() == 0
    assert scheduler.drain_delivery_queue({}, None) == 0
    assert matrix_sender.call_count == 1


@pytest.mark.parametrize('age', [3590, 3600])
def test_recent_failure_end_has_no_alert_or_write_transaction(owner, monkeypatch, clock, age):
    db, job = owner
    key = route.publish_completion(job, 'failed', for_failure=True)['delivery_id']
    end(db, clock, age)
    with monkeypatch.context() as patch:
        patch.setattr(ledger, '_transaction', Mock(side_effect=AssertionError('unexpected writer')))
        patch.setattr(delivery_queue, 'enqueue', Mock(side_effect=AssertionError('unexpected alert')))
        assert route.sweep_ended_origin_completions() == 0
    assert record(key) == ('pending', {'status': 'pending'})


def test_alerted_failure_can_still_deliver_after_reopen(owner, clock, monkeypatch):
    db, job = owner
    key = route.publish_completion(job, 'failed', for_failure=True)['delivery_id']
    end(db, clock, 3600 + 10)
    assert route.sweep_ended_origin_completions() == 1
    db.reopen_session('owner')
    monkeypatch.setattr(delivery_queue, 'enqueue', Mock(side_effect=AssertionError('duplicate alert')))
    assert route.sweep_ended_origin_completions() == 0
    deliver_reopened(db, job, key)


def test_unavailable_alert_is_recorded_once_and_warned(owner, monkeypatch, caplog, clock):
    db, job = owner
    key = route.publish_completion(job, 'failed', for_failure=True)['delivery_id']
    end(db, clock, 3600 + 10)
    enqueue = Mock(side_effect=OSError('queue unavailable'))
    monkeypatch.setattr(delivery_queue, 'enqueue', enqueue)
    assert route.sweep_ended_origin_completions() == 1
    assert record(key) == ('pending', {'status': 'pending', 'alert': 'unavailable'})
    assert key in caplog.text and 'native goal' in caplog.text and 'unavailable' in caplog.text
    end(db, clock, 7 * 86400 + 10)
    assert route.sweep_ended_origin_completions() == 1
    assert record(key)[0] == 'dropped' and record(key)[1]['alert'] == 'unavailable'
    assert enqueue.call_count == 1


@pytest.mark.parametrize('compressed', [False, True])
def test_live_idle_or_compression_tip_stays_pending(owner, compressed, clock):
    db, job = owner
    key = route.publish_completion(job, 'result')['delivery_id']
    if compressed:
        end(db, clock, 8 * 86400, 'compression')
        db.create_session('tip', 'desktop', user_id='alice', parent_session_id='owner')
    assert route.sweep_ended_origin_completions() == 0
    assert record(key) == ('pending', {'status': 'pending'})
    if compressed:
        end(db, clock, 6 * 86400, sid='tip')
        assert route.sweep_ended_origin_completions() == 0  # Tip's end, not root's older timestamp.
        end(db, clock, 8 * 86400, sid='tip')
        assert route.sweep_ended_origin_completions() == 1
        assert record(key)[1]['reason'] == 'origin_ended'


@pytest.mark.parametrize('claimed_at', [None, 'fresh', 'expired'])
def test_claim_lease_is_respected(owner, claimed_at, clock):
    db, job = owner
    key = route.publish_completion(job, 'result')['delivery_id']
    stamp = {'fresh': clock, 'expired': clock - ledger._CLAIM_LEASE_S - 10}.get(claimed_at)
    with ledger._transaction() as conn:
        conn.execute('UPDATE async_delegations SET delivery_claim=?,delivery_claimed_at=? WHERE delegation_id=?', ('claim', stamp, key))
    end(db, clock, 8 * 86400)
    assert route.sweep_ended_origin_completions() == (1 if claimed_at == 'expired' else 0)
    assert record(key)[0] == ('dropped' if claimed_at == 'expired' else 'pending')


@pytest.mark.parametrize('age', [10, 8 * 86400])
def test_publish_after_end_stays_pending_until_periodic_sweep(owner, clock, age):
    db, job = owner
    end(db, clock, age)
    receipt = route.publish_completion(job, 'result')
    assert receipt['status'] == 'pending'
    assert record(receipt['delivery_id']) == ('pending', {'status': 'pending'})
    assert route.sweep_ended_origin_completions() == (1 if age > 7 * 86400 else 0)


def test_invalid_binding_is_not_classified_as_ended(owner, clock):
    db, job = owner
    key = route.publish_completion(job, 'result')['delivery_id']
    with ledger._transaction() as conn:
        event = json.loads(conn.execute('SELECT event_json FROM async_delegations WHERE delegation_id=?', (key,)).fetchone()[0])
        event['cron_origin']['profile_home'] = '/different-profile'
        conn.execute('UPDATE async_delegations SET event_json=? WHERE delegation_id=?', (json.dumps(event), key))
    end(db, clock, 8 * 86400)
    assert route.sweep_ended_origin_completions() == 0
    assert record(key)[0] == 'pending'


@pytest.mark.parametrize('terminal_row', [False, True])
def test_no_candidates_opens_no_session_db_or_write_transaction(owner, monkeypatch, terminal_row):
    _db, job = owner
    if terminal_row:
        key = route.publish_completion(job, 'result')['delivery_id']
        with ledger._transaction() as conn:
            conn.execute('UPDATE async_delegations SET delivery_state=? WHERE delegation_id=?', ('delivered', key))
    monkeypatch.setattr('hermes_state.SessionDB', Mock(side_effect=AssertionError('unexpected SessionDB')))
    monkeypatch.setattr(ledger, '_transaction', Mock(side_effect=AssertionError('unexpected transaction')))
    trace, connect = [], route.sqlite3.connect
    def read_connection(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(trace.append)
        return conn
    monkeypatch.setattr(route.sqlite3, 'connect', read_connection)
    assert route.sweep_ended_origin_completions() == 0
    assert trace and all(sql.lstrip().upper().startswith('SELECT') for sql in trace)


@pytest.mark.parametrize('race', ['reopen', 'claim'])
def test_candidate_is_revalidated_after_preflight(owner, monkeypatch, clock, race):
    db, job = owner
    key = route.publish_completion(job, 'result')['delivery_id']
    end(db, clock, 8 * 86400)
    settle = route._settle_ended_origin_candidate
    def before_lock(database, candidate, now):
        if race == 'reopen':
            db.reopen_session('owner')
        else:
            assert ledger.claim_completion_delivery(key, 'racing-owner')
        return settle(database, candidate, now)
    monkeypatch.setattr(route, '_settle_ended_origin_candidate', before_lock)
    assert route.sweep_ended_origin_completions() == 0
    assert record(key)[0] == 'pending'


def test_alert_enqueue_survives_settlement_rollback_without_duplicate_send(owner, matrix_sender, clock):
    import sqlite3
    db, job = owner
    key = route.publish_completion(job, 'failed', for_failure=True)['delivery_id']
    end(db, clock, 8 * 86400)
    with ledger._transaction() as conn:
        conn.execute("CREATE TRIGGER refuse_drop BEFORE UPDATE OF delivery_state ON async_delegations WHEN NEW.delivery_state='dropped' BEGIN SELECT RAISE(ABORT, 'settlement failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match='settlement failed'):
        route.sweep_ended_origin_completions()
    assert record(key)[0] == 'pending'
    assert scheduler.drain_delivery_queue({}, None) == 1
    with ledger._transaction() as conn:
        conn.execute('DROP TRIGGER refuse_drop')
    assert route.sweep_ended_origin_completions() == 1
    assert record(key)[1]['alert'] == 'delivered'
    assert scheduler.drain_delivery_queue({}, None) == 0
    assert matrix_sender.call_count == 1
