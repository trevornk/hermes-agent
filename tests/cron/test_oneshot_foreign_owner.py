"""Finite dispatch budgets must not retire another process's active one-shot."""

import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest

from cron import jobs


_OWNER = """
import json, sys
from cron import jobs, executions
job_id, mode, ledger = sys.argv[1:]
if mode == 'scheduled':
    assert [j['id'] for j in jobs.get_due_jobs()] == [job_id]
job = jobs.claim_job_for_fire(job_id, manual=mode == 'manual', return_job=True)
assert job
assert jobs.claim_dispatch(job_id)
row = executions.create_execution(job_id, source=mode) if ledger == 'yes' else None
if row:
    executions.mark_execution_running(row['id'])
print(json.dumps({'owner': job['fire_claim']['by']}), flush=True)
command = sys.stdin.readline().strip()
if command == 'finish':
    assert jobs.mark_job_run(job_id, True, expected_fire_owner=job['fire_claim']['by'])
    if row:
        executions.finish_execution(row['id'], success=True)
"""

_SCAN = """
from unittest.mock import patch
from cron import jobs, scheduler
assert jobs.get_due_jobs() == []
with patch.object(scheduler, '_maybe_run_worktree_maintenance'), \
     patch.object(scheduler, '_sweep_mcp_orphans'), \
     patch.object(scheduler, '_should_yield_tick_to_fresh_gateway', return_value=None), \
     patch.object(scheduler, '_process_due_job', side_effect=AssertionError('duplicate dispatch')):
    assert scheduler.tick(verbose=False, headless=True) == 0
    assert scheduler.tick(verbose=False, headless=True) == 0
"""


def _due_job():
    instant = (jobs._hermes_now() - timedelta(seconds=5)).isoformat()
    job = {
        'id': 'fixture-oneshot', 'name': 'fixture', 'prompt': 'fixture',
        'schedule': {'kind': 'once', 'run_at': instant},
        'next_run_at': instant, 'enabled': True, 'state': 'scheduled',
        'repeat': {'times': 1, 'completed': 0}, 'deliver': 'local',
    }
    jobs.save_jobs([job])
    return job['id']


def test_duplicate_dispatch_preserves_active_fire(tmp_path):
    with jobs.use_cron_store(tmp_path):
        job_id = _due_job()
        claimed = jobs.claim_job_for_fire(job_id, manual=True, return_job=True)
        assert claimed
        assert jobs.claim_dispatch(job_id)
        assert jobs.claim_dispatch(job_id) is False
        stored = jobs.get_job(job_id)
        assert stored is not None, 'duplicate dispatch deleted the live fire owner'
        assert stored['repeat']['completed'] == 1
        assert jobs.mark_job_run(job_id, True, expected_fire_owner=claimed['fire_claim']['by'])


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('mode', ['manual', 'scheduled'])
@pytest.mark.parametrize('ownership', ['fire', 'ledger', 'both', 'dead'])
def test_separate_owner_and_scheduler_processes(tmp_path, monkeypatch, mode, ownership):
    home = tmp_path / 'owner-home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_RUNTIME_DIR', str(tmp_path / 'runtime'))
    with jobs.use_cron_store(home):
        job_id = _due_job()
    env = os.environ.copy()
    env['PYTHONPATH'] = os.getcwd()
    process = subprocess.Popen(
        [sys.executable, '-u', '-c', _OWNER, job_id, mode,
         'no' if ownership == 'fire' else 'yes'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    try:
        # communicate() remains bounded even if the child fails before its ready receipt.
        from tests.waiting import wait_until
        import selectors
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert wait_until(lambda: bool(selector.select(timeout=0)), timeout=15)
        ready = json.loads(process.stdout.readline())
        if ownership == 'dead':
            process.stdin.write('exit\n')
            process.stdin.flush()
            assert process.wait(timeout=10) == 0
        with jobs.use_cron_store(home):
            records = jobs.load_jobs()
            # Reach the limit guard even for a scheduled run whose dispatch run_claim expired.
            records[0]['run_claim'] = None
            if ownership == 'ledger':
                records[0]['fire_claim']['at'] = (
                    jobs._hermes_now() - timedelta(seconds=jobs.FIRE_CLAIM_TTL_SECONDS + 1)
                ).isoformat()
            jobs.save_jobs(records)

        # A genuinely separate scheduler process, twice: no shared in-memory running set.
        result = subprocess.run([sys.executable, '-c', _SCAN], env=env,
                                capture_output=True, text=True, timeout=20, check=False)
        assert result.returncode == 0, result.stderr
        with jobs.use_cron_store(home):
            stored = jobs.get_job(job_id)
            if ownership == 'dead':
                assert stored is None
                return
            assert stored is not None, 'scheduler retired an active foreign run'
            assert stored['fire_claim']['by'] == ready['owner']
            assert stored['repeat']['completed'] == 1
            # A duplicate direct dispatch must refuse without deleting the live owner's record.
            assert jobs.claim_dispatch(job_id) is False
            assert jobs.get_job(job_id) is not None
        process.stdin.write('finish\n')
        process.stdin.flush()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, (stdout, stderr)
        with jobs.use_cron_store(home):
            completed = jobs.get_job(job_id)
            assert completed['state'] == 'completed'
            assert completed['last_status'] == 'ok'
            assert completed['repeat']['completed'] == 1
            assert jobs.get_due_jobs() == []
    finally:
        if process.poll() is None:
            process.communicate(input='exit\n', timeout=10)
        else:
            process.communicate(timeout=10)
