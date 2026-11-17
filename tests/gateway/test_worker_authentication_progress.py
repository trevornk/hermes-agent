"""Worker authentication must not hold the owner loop behind a fallback store reader."""
import asyncio
from contextlib import contextmanager
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from gateway.session_worker import _claim, worker_request
from hermes_state import SessionDB
from hermes_state_runtime import (
    RuntimeStoreError, begin_runtime_epoch, register_worker_execution,
)
from hermes_cli.process_identity import worker_birth
import psutil


@pytest.fixture
def worker(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        db.create_session('owned', 'cli')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        authority = SimpleNamespace(db=db, epoch=epoch, profile_id=str(tmp_path), sessions={},
            _require_admission_open=lambda: None, logical_owner=lambda sid: sid, runner=None)
        scope = dict(profile_id=str(tmp_path), session_id='owned', execution_id='worker',
                     generation=0, pid=process.pid, birth=worker_birth(psutil.Process(process.pid)),
                     secret='synthetic-worker-secret')
        actor = SimpleNamespace(subject='human', profile_id=str(tmp_path), capabilities={'worker:adopt'})
        connection = SimpleNamespace(authority=authority, actor=actor)
        register_worker_execution(db, epoch=epoch, execution_id='worker', session_id='owned',
                                  generation=0, kind='compute', adoption_secret=_claim(connection, None, scope))
        scope['epoch'] = epoch
        yield SimpleNamespace(authority=authority, actor=actor), scope
    finally:
        process.stdin.close()
        process.wait(timeout=5)
        db.close()


CASES = [
    ('session.prompt', {'system_prompt': 'saved'}),
    ('delegation.read', {'delegation_id': 'missing'}),
    ('child.reserve', {'secret': 'synthetic-child-secret'}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,payload', CASES)
@pytest.mark.parametrize('read_number', [1, 2])
async def test_fallback_authentication_read_allows_owner_loop_progress(worker, monkeypatch, operation, payload, read_number):
    connection, scope = worker
    db = connection.authority.db
    identity = {k: v for k, v in scope.items() if k != 'epoch'}
    await worker_request(connection, None, identity, operation='adopt')
    # Exercise the real fallback connection and writer lock, not a mocked database response.
    monkeypatch.setattr(db, '_checkout_read_conn', lambda: None)
    original = db._read_ctx
    loop = asyncio.get_running_loop()
    release, held, loop_progress = threading.Event(), threading.Event(), threading.Event()
    blocked_read_progress = []
    reads = 0

    def holder():
        with db._lock:
            held.set()
            release.wait(5)

    @contextmanager
    def read(**kwargs):
        nonlocal reads
        reads += 1
        gated = reads == read_number
        if gated:
            thread.start()
            assert held.wait(5)
            # The callback can release the lock only if authentication yields the owner loop.
            loop.call_soon_threadsafe(progress)
        with original(**kwargs) as conn:
            if gated:
                blocked_read_progress.append(loop_progress.is_set())
            yield conn

    def progress():
        loop_progress.set()
        release.set()

    thread = threading.Thread(target=holder)
    monkeypatch.setattr(db, '_read_ctx', read)
    try:
        params = scope | {'sequence': 1, 'operation': operation, 'payload': payload}
        if operation == 'delegation.read':
            with pytest.raises(RuntimeStoreError, match='not_found'):
                await worker_request(connection, SimpleNamespace(session_id='owned'), params, operation='persist')
        else:
            result = await worker_request(connection, None, params, operation='persist')
            assert isinstance(result, dict)
        assert blocked_read_progress == [True]
    finally:
        release.set()
        if thread.ident is not None:
            await asyncio.to_thread(thread.join, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,payload', CASES)
async def test_wrong_worker_secret_never_writes_a_receipt(worker, operation, payload):
    connection, scope = worker
    with pytest.raises(RuntimeStoreError, match='permission_denied'):
        await worker_request(connection, None, scope | {'secret': 'wrong-synthetic-secret',
            'sequence': 1, 'operation': operation, 'payload': payload}, operation='persist')
    assert connection.authority.db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 0


def test_lost_persistence_receipt_is_unknown_sticky_and_exactly_replayable(tmp_path):
    from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError, WorkerReceiptTimeout
    from hermes_cli.turn_exit import owner_transport_failed, turn_exit_code
    from hermes_state_runtime import mutate_worker_execution, register_worker_execution

    db = SessionDB(tmp_path / 'state.db')
    db.create_session('owned', 'cli')
    epoch = begin_runtime_epoch(db, instance_id='owner')
    register_worker_execution(db, epoch=epoch, execution_id='worker', session_id='owned',
                              generation=0, kind='compute', adoption_secret='synthetic')
    scope = dict(epoch=epoch, execution_id='worker', session_id='owned', generation=0)
    calls = []

    def rpc(method, **params):
        calls.append(params)
        result = mutate_worker_execution(db, **params)
        if len(calls) == 1:
            raise WorkerReceiptTimeout(method, 20)
        return result

    store = RuntimeSessionStore(rpc, scope, tmp_path / 'outbox')
    try:
        with pytest.raises(WorkerReceiptTimeout) as timed_out:
            store._apply('transcript.append', {'messages': [{'role': 'user', 'content': 'inert'}]})
        assert db._read_one('SELECT COUNT(*) FROM messages')[0] == 1
        assert len(store.journal['pending']) == 1
        assert turn_exit_code({'completed': False}, kanban_worker=True,
                              owner_unavailable=owner_transport_failed(exc=timed_out.value)) == 69
        with pytest.raises(WorkerPersistenceError, match='worker_receipt_timeout'):
            store._apply('session.prompt', {'system_prompt': 'must not overtake pending write'})
        assert len(calls) == 1
        assert store.retry_pending()[0]['count'] == 1
        assert calls[0] == calls[1]
        assert db._read_one('SELECT COUNT(*) FROM messages')[0] == 1
        assert db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 1
        assert store.failure is None and store.journal['pending'] == []
        assert turn_exit_code({'completed': True}, kanban_worker=True) == 0
    finally:
        store.close()
        db.close()


@pytest.mark.asyncio
async def test_cancelled_owner_waiter_does_not_cancel_inflight_sqlite_commit(worker, monkeypatch):
    import gateway.session_worker as owner
    connection, scope = worker
    original = owner.mutate_worker_execution
    entered, release, done = threading.Event(), threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(5)
            return original(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(owner, 'mutate_worker_execution', delayed)
    params = scope | {'sequence': 1, 'operation': 'session.prompt', 'payload': {'system_prompt': 'late commit'}}
    task = asyncio.create_task(worker_request(connection, None, params, operation='persist'))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(done.wait, 5)
    db = connection.authority.db
    assert db.get_session('owned')['system_prompt'] == 'late commit'
    result = await worker_request(connection, None, params, operation='persist')
    assert result == {'value': None}
    assert db._read_one('SELECT COUNT(*) FROM worker_receipts')[0] == 1
