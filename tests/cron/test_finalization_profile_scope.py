"""A detached cron worker must finalize in its owning multiplexed profile."""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from agent.secret_scope import reset_multiplex_context, set_multiplex_context
from cron import scheduler
from gateway import session_cron
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB


class RecordingProvider(MemoryProvider):
    name = 'fixture-memory'

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.home = Path(kwargs['hermes_home'])

    def get_tool_schemas(self):
        return []

    def on_session_end(self, messages):
        from hermes_constants import get_hermes_home
        assert get_hermes_home() == self.home
        (self.home / 'memory-receipt').write_text('ended', encoding='utf-8')


def _manager(home):
    manager = MemoryManager()
    manager.add_provider(RecordingProvider())
    manager.initialize_all('fixture-session', hermes_home=str(home))
    return manager


@pytest.fixture
def multiplexed(monkeypatch):
    # A host gateway's guard also applies after a worker exits its copied turn context.
    monkeypatch.setattr('agent.secret_scope._MULTIPLEX_ACTIVE', True)
    token = set_multiplex_context(True)
    try:
        yield
    finally:
        reset_multiplex_context(token)


@pytest.mark.parametrize('abort', [False, True])
def test_run_job_finalizes_in_owner_profile(tmp_path, monkeypatch, multiplexed, abort):
    owner, other = tmp_path / 'owner', tmp_path / 'other'
    owner.mkdir()
    other.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(other))
    monkeypatch.setenv('HERMES_RUNTIME_DIR', str(tmp_path / 'runtime'))
    manager = _manager(owner)
    release, closed, cancel = threading.Event(), threading.Event(), threading.Event()
    db = SessionDB(db_path=owner / 'state.db')
    db.create_session('fixture-session', source='cron')
    db.append_message('fixture-session', 'user', 'fixture')
    if not abort:
        db.append_message('fixture-session', 'assistant', 'done')

    class Agent:
        session_id = 'fixture-session'

        def run_conversation(self, *args, **kwargs):
            if abort:
                cancel.set()
                assert release.wait(10)
            return {'final_response': 'done'}

        def interrupt(self, *_args):
            pass

        def close(self):
            manager.on_session_end([])
            manager.shutdown_all()
            closed.set()

    agent = Agent()
    job = {'id': 'fixture-job', 'name': 'fixture', 'prompt': 'fixture',
           'schedule': {'kind': 'once'}}
    monkeypatch.setattr(scheduler, '_prepare_job_prompt', lambda *a: (None, 'fixture'))
    monkeypatch.setattr(scheduler, '_reload_dotenv_and_publish_delivery_target', lambda *a: None)
    monkeypatch.setattr(scheduler, '_load_cron_job_config', lambda *a: SimpleNamespace(cfg={}, model='fixture'))
    monkeypatch.setattr(scheduler, '_resolve_cron_agent_setup', lambda *a: SimpleNamespace(
        blocked=None, model='fixture', fallback_notice=None))
    monkeypatch.setattr(scheduler, '_open_cron_session_db', lambda *a: db)
    monkeypatch.setattr(scheduler, '_construct_cron_agent', lambda *a, **kw: agent)
    monkeypatch.setattr(scheduler, '_FireAudit', lambda *a: SimpleNamespace(write=lambda *a: None))
    # Event synchronization controls the worker; shorten only the monitor's polling in this fixture.
    real_wait = scheduler.concurrent.futures.wait
    monkeypatch.setattr(scheduler.concurrent.futures, 'wait', lambda fs, timeout: real_wait(fs, timeout=0.01))
    home_token = set_hermes_home_override(str(owner))
    execution_token = session_cron._execution.set((None, 'fixture-session', job['id'], None))
    try:
        result = scheduler.run_job(job, cancel_event=cancel)
        assert result[0] is (not abort)
        if abort:
            assert 'ownership' in result[3]
            assert not closed.is_set()
    finally:
        session_cron._execution.reset(execution_token)
        reset_hermes_home_override(home_token)
        # Complete after the owning scope exited: Future callbacks run outside ctx.run().
        release.set()
    assert closed.wait(10)
    assert (owner / 'memory-receipt').read_text(encoding='utf-8') == 'ended'
    assert not (other / 'memory-receipt').exists()
    check = SessionDB(db_path=owner / 'state.db')
    try:
        row = check.get_session('fixture-session')
        assert row['end_reason'] == ('cron_incomplete_no_output' if abort else 'cron_complete')
    finally:
        check.close()


def test_memory_refuses_missing_and_foreign_profile_then_accepts_owner(tmp_path, multiplexed, caplog):
    owner, other = tmp_path / 'owner', tmp_path / 'other'
    owner.mkdir()
    other.mkdir()
    manager = _manager(owner)
    for home in (None, str(other)):
        token = set_hermes_home_override(home)
        try:
            manager.on_session_end([])
        finally:
            reset_hermes_home_override(token)
    assert not (owner / 'memory-receipt').exists()
    assert 'memory_caller_profile_unresolved' in caplog.text
    token = set_hermes_home_override(str(owner))
    try:
        manager.on_session_end([])
        manager.shutdown_all()
    finally:
        reset_hermes_home_override(token)
    assert (owner / 'memory-receipt').is_file()
    assert not (other / 'memory-receipt').exists()
