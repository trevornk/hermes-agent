"""Real cron creation/delivery entrypoints and Desktop admission, with no network model."""
import json
import queue
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from cron import desktop_origin as route
from cron.jobs import create_job, get_job, update_job
from cron.scheduler_delivery import _deliver_result
from tools import async_delegation as ledger
from tui_gateway import server
from tests.tui_gateway.test_auto_continue import turn_env, marker_home, _session


@pytest.fixture
def owner(monkeypatch):
    home = get_hermes_home()
    db = SessionDB(home / 'state.db')
    db.create_session('owner', 'desktop', user_id='basic:alice')
    db.create_session('other', 'desktop', user_id='basic:alice')
    @contextmanager
    def database(session):
        yield db
    monkeypatch.setattr(server, '_session_db', database)
    monkeypatch.setattr(server, '_session_home', lambda session: home)
    agent = SimpleNamespace(session_id='owner', clear_interrupt=lambda: None)
    session = _session(agent=agent, session_key='owner', auth_user_id='basic:alice', profile_home=str(home))
    monkeypatch.setitem(server._sessions, 'ui-owner', session)
    from tools.process_registry import process_registry
    monkeypatch.setattr(process_registry, 'completion_queue', queue.Queue())
    yield session, db
    db.close()


def commission(session):
    token = server._current_runtime_session_record.set(session)
    try:
        job = create_job('bounded result', 'in 10m', name='native fixture')
    finally:
        server._current_runtime_session_record.reset(token)
    job['execution_id'] = 'execution-1'
    return job


def record(key):
    with ledger._transaction() as conn:
        row = conn.execute('SELECT delivery_state,result_json,event_json FROM async_delegations WHERE delegation_id=?', (key,)).fetchone()
    return row[0], json.loads(row[1]), json.loads(row[2])


def publish(session, failure=False):
    job = commission(session)
    _deliver_result(job, 'worker failed' if failure else 'worker result', for_failure=failure)
    receipt = job['_origin_delivery_receipt']
    assert receipt['status'] == 'pending'
    return job, record(receipt['delivery_id'])[2]


def test_creation_captures_only_authenticated_runtime_and_cannot_retarget(owner, monkeypatch):
    session, _db = owner
    job = commission(session)
    assert job['deliver'] == 'origin'
    assert job['origin'] == {'platform': route.PLATFORM, 'chat_id': 'owner',
                             'profile_home': str(get_hermes_home().resolve()), 'principal_id': 'basic:alice'}
    assert get_job(job['id'])['origin'] == job['origin']
    with pytest.raises(ValueError, match='cannot be changed'):
        update_job(job['id'], {'origin': {**job['origin'], 'chat_id': 'other'}})
    monkeypatch.setenv('HERMES_SESSION_PLATFORM', route.PLATFORM)
    monkeypatch.setenv('HERMES_SESSION_CHAT_ID', 'owner')
    assert route.capture_origin() is None
    with pytest.raises(ValueError, match='authenticated'):
        create_job('forged', 'in 10m', origin=job['origin'])
    assert create_job('no authority', 'in 10m')['origin'] is None


@pytest.mark.parametrize('failure', [False, True])
def test_scheduler_publish_idempotent_success_failure_and_wrong_profile(owner, failure):
    session, _db = owner
    job, event = publish(session, failure)
    key = event['delegation_id']
    assert event['status'] == ('error' if failure else 'completed')
    assert route.publish_completion(job, 'worker failed' if failure else 'worker result', for_failure=failure)['delivery_id'] == key
    with pytest.raises(ValueError, match='different payload'):
        route.publish_completion(job, 'replacement', for_failure=failure)
    job['origin'] = {**job['origin'], 'profile_home': '/wrong-profile'}
    with pytest.raises(ValueError, match='mismatch'):
        route.publish_completion(job, 'worker result', for_failure=failure)
    assert record(key)[0] == 'pending'


def test_owner_gate_compression_disconnect_other_session_and_principal(owner):
    session, db = owner
    _job, event = publish(session)
    session['transport'] = None
    assert server._session_owns_notification_event('ui-owner', session, event)
    for field, value in [('auth_user_id', 'basic:mallory'), ('agent', SimpleNamespace(session_id='other'))]:
        foreign = {**session, field: value}
        assert not server._session_owns_notification_event('ui-owner', foreign, event)
    db._write_sql("UPDATE sessions SET ended_at=1,end_reason='compression' WHERE id='owner'")
    db.create_session('compressed', 'desktop', user_id='basic:alice', parent_session_id='owner')
    assert not server._session_owns_notification_event('ui-owner', session, event)
    session['agent'].session_id = 'compressed'
    session['session_key'] = 'compressed'
    assert server._session_owns_notification_event('new-ui', session, event)


@pytest.mark.parametrize('failure', [False, True])
def test_actual_desktop_turn_terminal_receipt_and_duplicate_fence(owner, turn_env, monkeypatch, failure):
    session, _db = owner
    _job, event = publish(session, failure)
    seen, frames = [], []
    monkeypatch.setattr(server, 'write_json', lambda frame: frames.append(frame) or True)
    monkeypatch.setattr(server, '_start_usage_ticker', lambda *args: (threading.Event(), SimpleNamespace(join=lambda: None)))
    monkeypatch.setattr(server.threading, 'Thread', server._RealThread)
    def run(message, **kwargs):
        assert record(event['delegation_id'])[0] == 'started'
        seen.append(message)
        return {'final_response': 'owner followed through', 'messages': []}
    session['agent'].run_conversation = run
    assert server._notif_claim_turn(session)
    server._notif_dispatch_event('ui-owner', session, event, 'native worker completed')
    session['_run_thread'].join(10)
    assert not session['_run_thread'].is_alive()
    state, result, _ = record(event['delegation_id'])
    assert state == 'delivered'
    assert result['owner_followthrough']['status'] == 'settled'
    assert len(seen) == 1
    assert any(frame.get('params', {}).get('type') == 'message.complete' for frame in frames)
    assert server._notif_claim_turn(session)
    server._notif_dispatch_event('ui-owner', session, event, 'duplicate')
    assert len(seen) == 1


def test_restart_replay_pending_and_interrupted_model_escalation(owner, monkeypatch):
    session, db = owner
    _job, event = publish(session)
    key = event['delegation_id']
    with ledger._transaction() as conn:
        conn.execute('UPDATE async_delegations SET completed_at=1 WHERE delegation_id=?', (key,))
    replay = queue.Queue()
    ledger.restore_undelivered_completions(replay)
    restored = replay.get_nowait()
    assert restored['delegation_id'] == key
    assert route.belongs_to_session(server, session, restored)
    claim = ledger.claim_event_delivery(restored, 'fixture')
    assert claim
    assert route.begin_followthrough(restored, claim)
    monkeypatch.setattr(ledger, '_owner_liveness', lambda: lambda pid, birth: False)
    route.recover_interrupted(server, session)
    assert record(key)[0] == 'dropped'
    assert record(key)[1]['owner_followthrough']['status'] == 'unknown'
    assert ledger.claim_event_delivery(restored, 'duplicate') is None
    notices = db._read_all("SELECT content FROM messages WHERE session_id='owner'")
    assert len(notices) == 1 and 'outcome is unknown' in notices[0][0]
    route.recover_interrupted(server, session)
    assert len(db._read_all("SELECT id FROM messages WHERE session_id='owner'")) == 1


def test_refused_admission_returns_pending_without_model_attempt(owner, monkeypatch):
    session, _db = owner
    _job, event = publish(session)
    monkeypatch.setattr(server, '_run_prompt_submit', lambda *args, **kwargs: False)
    assert server._notif_claim_turn(session)
    server._notif_dispatch_event('ui-owner', session, event, 'completion')
    state, result, _ = record(event['delegation_id'])
    assert state == 'pending' and result['owner_followthrough']['status'] == 'pending'
    assert not session['running']
    assert ledger.claim_event_delivery(event, 'retry')


def test_failed_model_is_terminal_and_does_not_claim_success(owner, turn_env, monkeypatch):
    session, _db = owner
    _job, event = publish(session)
    monkeypatch.setattr(server, 'write_json', lambda frame: True)
    monkeypatch.setattr(server, '_start_usage_ticker', lambda *args: (threading.Event(), SimpleNamespace(join=lambda: None)))
    monkeypatch.setattr(server.threading, 'Thread', server._RealThread)
    def run(*args, **kwargs):
        raise RuntimeError('bounded fixture provider failure')
    session['agent'].run_conversation = run
    assert server._notif_claim_turn(session)
    server._notif_dispatch_event('ui-owner', session, event, 'completion')
    session['_run_thread'].join(10)
    state, result, _ = record(event['delegation_id'])
    assert state == 'dropped'
    assert result['owner_followthrough']['status'] == 'failed'
    assert ledger.claim_event_delivery(event, 'retry') is None


def test_actual_cron_tool_captures_desktop_origin(owner):
    from tools.cronjob_tools import cronjob
    session, _db = owner
    script_dir = get_hermes_home() / 'scripts'
    script_dir.mkdir(exist_ok=True)
    (script_dir / 'bounded.py').write_text("print('bounded result')\n")
    token = server._current_runtime_session_record.set(session)
    try:
        result = json.loads(cronjob(action='create', schedule='in 10m', script='bounded.py',
                                   no_agent=True, paused=True, deliver='origin'))
    finally:
        server._current_runtime_session_record.reset(token)
    assert result.get('success'), result
    from cron.jobs import list_jobs
    job = next(j for j in list_jobs(include_disabled=True) if j['script'] == 'bounded.py')
    assert job['origin']['chat_id'] == 'owner'


@pytest.mark.parametrize('published_before_crash', [False, True])
def test_actual_delivery_queue_recovers_only_idempotent_owner_publication(owner, monkeypatch, published_before_crash):
    from cron import delivery_queue as handoff
    session, _db = owner
    job = commission(session)
    handoff.enqueue(job['execution_id'], job, 'durable result')
    assert handoff.claim_next()
    if published_before_crash:
        route.publish_completion(job, 'durable result')
    assert handoff.enqueue_and_wait(job['execution_id'], job, 'durable result', timeout=0) is None
    assert handoff.get_status(job['execution_id'])['status'] == 'delivering'
    monkeypatch.setattr(handoff, '_PROCESS_ID', 'replacement-gateway')
    monkeypatch.setattr(handoff, '_owner_is_live', lambda *args: False)
    handoff.recover_abandoned()
    assert handoff.get_status(job['execution_id'])['status'] == 'pending'
    assert handoff.drain(lambda j, content, failure: _deliver_result(j, content, for_failure=failure)) == 1
    with ledger._transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM async_delegations WHERE delegation_id LIKE 'cron-owner-%'").fetchone()[0] == 1
    assert handoff.get_status(job['execution_id'])['status'] == 'delivered'


def test_submit_exception_persists_notice_before_terminalization(owner, monkeypatch):
    session, db = owner
    _job, event = publish(session)
    def fail(*args, **kwargs):
        raise RuntimeError('thread creation failed')
    monkeypatch.setattr(server, '_run_prompt_submit', fail)
    assert server._notif_claim_turn(session)
    with pytest.raises(RuntimeError, match='thread creation'):
        server._notif_dispatch_event('ui-owner', session, event, 'completion')
    assert record(event['delegation_id'])[0] == 'dropped'
    rows = db._read_all("SELECT content FROM messages WHERE display_kind='cron_completion'")
    assert len(rows) == 1 and 'outcome is unknown' in rows[0][0]


@pytest.mark.parametrize('state', ['delivered', 'dropped'])
def test_terminal_payload_compaction_keeps_idempotency_tombstone(owner, state):
    session, _db = owner
    job, event = publish(session)
    key = event['delegation_id']
    with ledger._transaction() as conn:
        conn.execute('UPDATE async_delegations SET delivery_state=?,updated_at=1 WHERE delegation_id=?', (state, key))
    ledger._prune_durable_records()
    assert record(key)[0] == state
    assert set(record(key)[2]) == {'payload_sha256'}
    assert route.publish_completion(job, 'worker result')['status'] == state
    with pytest.raises(ValueError, match='different payload'):
        route.publish_completion(job, 'different result')


def test_failed_started_write_reoffers_pending_without_false_unknown(owner):
    import sqlite3
    session, db = owner
    _job, event = publish(session)
    with ledger._transaction() as conn:
        conn.execute("CREATE TRIGGER fixture_reject_start BEFORE UPDATE OF delivery_state ON async_delegations WHEN NEW.delivery_state='started' BEGIN SELECT RAISE(ABORT, 'fixture start write failure'); END")
    assert server._notif_claim_turn(session)
    with pytest.raises(sqlite3.IntegrityError, match='fixture start write'):
        server._notif_dispatch_event('ui-owner', session, event, 'completion')
    assert not session['running']
    assert record(event['delegation_id'])[0] == 'pending'
    assert db._read_all("SELECT id FROM messages WHERE display_kind='cron_completion'") == []
    with ledger._transaction() as conn:
        assert conn.execute('SELECT delivery_claim FROM async_delegations WHERE delegation_id=?', (event['delegation_id'],)).fetchone()[0] is None
        conn.execute('DROP TRIGGER fixture_reject_start')
    replay = queue.Queue()
    ledger.restore_undelivered_completions(replay)
    restored = replay.get_nowait()
    assert restored['delegation_id'] == event['delegation_id']
    claim = ledger.claim_event_delivery(restored, 'restarted-owner')
    assert claim and route.begin_followthrough(restored, claim)


def test_start_exception_after_commit_preserves_unknown_no_replay(owner, monkeypatch):
    session, db = owner
    _job, event = publish(session)
    real = route.begin_followthrough
    def committed_then_failed(event, claim):
        assert real(event, claim)
        raise RuntimeError('post-commit uncertainty')
    monkeypatch.setattr(route, 'begin_followthrough', committed_then_failed)
    assert server._notif_claim_turn(session)
    with pytest.raises(RuntimeError, match='post-commit uncertainty'):
        server._notif_dispatch_event('ui-owner', session, event, 'completion')
    assert record(event['delegation_id'])[0] == 'dropped'
    assert len(db._read_all("SELECT id FROM messages WHERE display_kind='cron_completion'")) == 1
    assert ledger.claim_event_delivery(event, 'restarted-owner') is None


@pytest.mark.parametrize('secondary', [False, True])
def test_actual_owner_poller_replays_when_shared_offer_is_consumed_elsewhere(owner, turn_env, monkeypatch, secondary):
    """A foreign consumer repeatedly returns the only queue copy: owner must still progress."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.process_registry import process_registry
    from tools import process_registry_awaiting
    session, db = owner
    home = get_hermes_home()
    token = None
    if secondary:
        home = home / 'profiles' / 'quant'
        home.mkdir(parents=True)
        token = set_hermes_home_override(str(home))
        db = SessionDB(home / 'state.db')
        db.create_session('owner', 'desktop', user_id='basic:alice')
        @contextmanager
        def database(_session):
            yield db
        monkeypatch.setattr(server, '_session_db', database)
        monkeypatch.setattr(server, '_session_home', lambda _session: home)
        session['profile_home'] = str(home)
    try:
        _job, event = publish(session)
        # Model the real competing consumer, retaining no in-memory copy here.
        while not process_registry.completion_queue.empty():
            ledger.return_completion_offer(process_registry.completion_queue.get_nowait())
        monkeypatch.setattr(process_registry, 'restore_completions', lambda: None)
        monkeypatch.setattr(ledger, 'maybe_sweep_orphaned_completions', lambda _queue: 0)
        monkeypatch.setattr(server, 'write_json', lambda frame: True)
        monkeypatch.setattr(server, '_start_usage_ticker', lambda *args: (threading.Event(), SimpleNamespace(join=lambda: None)))
        monkeypatch.setattr(server.threading, 'Thread', server._RealThread)
        for name in ('_poll_bot_completion_notice_once', '_maybe_fire_tui_loop_tick', '_maybe_fire_tui_heartbeat_tick', '_notif_poll_kanban'):
            monkeypatch.setattr(server, name, lambda *args: None)
        seen = []
        session['agent'].run_conversation = lambda text, **kwargs: seen.append(text) or {'final_response': 'owner followed through', 'messages': []}
        stop = threading.Event()
        monkeypatch.setattr(process_registry_awaiting, 'take_ready', lambda *args: stop.set() or [])
        server._notification_poller_scoped_loop(stop, 'ui-owner', session)
        thread = session.get('_run_thread')
        assert thread is not None, 'durable pending receipt never reached owner admission'
        thread.join(10)
        assert not thread.is_alive()
        assert record(event['delegation_id'])[0] == 'delivered'
        assert len(seen) == 1
        assert route.pending_for_session(server, session) == []
        server._notif_dispatch_event('ui-owner', session, event, 'late shared copy')
        assert len(seen) == 1
    finally:
        if token is not None:
            db.close()
            reset_hermes_home_override(token)
