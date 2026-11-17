"""Transactional local lineage projections and one-time writer migration."""
import json
from unittest.mock import patch

import pytest

from hermes_state import SessionDB
from hermes_state_local import (
    LINEAGE_BACKFILL_DONE, LINEAGE_INDEX_PREFIX, POLICY_PREFIX, backfill_lineage_index,
    local_lineage_index, release_receipts, retire_local_receipts, sync_lineage_index,
)
from hermes_state_local_lineage import _commit_local_target
from tests.hermes_state.local_receipts import store_receipt


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / 'state.db')
    yield database
    database.close()


def _index(conn, owner):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (LINEAGE_INDEX_PREFIX + owner,)).fetchone()
    return json.loads(row[0]) if row else None


def test_real_reset_creates_projection(db, tmp_path):
    from hermes_state_runtime import begin_runtime_epoch
    from tests.hermes_state.test_target_advance_fence import _local_session, _mutate

    epoch = begin_runtime_epoch(db, instance_id='owner')
    owner = _local_session(db, epoch, tmp_path)
    assert _index(db._conn, owner) is None
    result = _mutate(db, epoch, owner, 'reset')
    assert _index(db._conn, owner) == [owner, result['target_session_id']]


@pytest.mark.parametrize('lineage', [None, ['copy'], ['copy', 'tip']])
def test_creation_projects_only_multi_segment_receipts(db, tmp_path, lineage):
    from hermes_state_local import commit_local_session
    from hermes_state_runtime import begin_runtime_epoch
    from tests.hermes_state.test_target_advance_fence import _local_session

    epoch = begin_runtime_epoch(db, instance_id='owner')
    original = _local_session(db, epoch, tmp_path)
    receipt = json.loads(db._conn.execute('SELECT value FROM state_meta WHERE key=?',
                                         (POLICY_PREFIX + original,)).fetchone()[0])
    receipt.update(session_id='copy', route='copy-route', request_id='copy')
    receipt['entry'] = dict(receipt['entry'], session_id='copy')
    if lineage is not None:
        receipt['lineage'] = lineage
    commit_local_session(db, epoch=epoch, receipt=receipt)
    assert _index(db._conn, 'copy') == (lineage if lineage and len(lineage) > 1 else None)


def test_target_commit_and_projection_rewrite_share_transaction(db):
    conn = db._conn
    db.create_session('owner', source='cli')
    receipt = store_receipt(conn, 'owner', ['owner'], route='route', entry={'session_id': 'owner'})
    conn.commit()
    assert _index(conn, 'owner') is None
    conn.execute('BEGIN')
    receipt['lineage'].append('head')
    receipt['entry']['session_id'] = 'head'
    _commit_local_target(conn, receipt, receipt['entry'])
    assert _index(conn, 'owner') == ['owner', 'head']
    conn.rollback()
    assert _index(conn, 'owner') is None
    _commit_local_target(conn, receipt, receipt['entry'])
    conn.commit()
    assert _index(conn, 'owner') == ['owner', 'head']
    sync_lineage_index(conn, 'owner', ['owner'])
    assert _index(conn, 'owner') is None


@pytest.mark.parametrize('remove', ['retire', 'release'])
def test_receipt_removal_removes_projection_in_same_transaction(db, remove):
    conn = db._conn
    store_receipt(conn, 'owner', ['owner', 'head'], entry={'session_id': 'head'})
    conn.commit()
    conn.execute('BEGIN')
    if remove == 'retire':
        retire_local_receipts(conn, ['head'])
    else:
        release_receipts(conn, ['owner'])
    assert _index(conn, 'owner') is None
    conn.rollback()
    assert _index(conn, 'owner') == ['owner', 'head']


def test_stale_and_malformed_projections_are_ignored(db):
    conn = db._conn
    sync_lineage_index(conn, 'missing', ['missing', 'head'])
    for owner, raw in [('broken', '{'), ('object', '{}'), ('short', '["short"]'), ('typed', '["typed",1]')]:
        store_receipt(conn, owner, [owner])
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (LINEAGE_INDEX_PREFIX + owner, raw))
    assert local_lineage_index(conn) == (set(), {})


def test_backfill_is_once_and_fresh_store_is_marked(db):
    conn = db._conn
    assert conn.execute('SELECT 1 FROM state_meta WHERE key=?', (LINEAGE_BACKFILL_DONE,)).fetchone()
    conn.execute('DELETE FROM state_meta WHERE key=?', (LINEAGE_BACKFILL_DONE,))
    for owner, raw in [('owner', json.dumps({'lineage': ['owner', 'head']})),
                       ('single', json.dumps({'lineage': ['single']})), ('broken', '{'),
                       ('typed', json.dumps({'lineage': ['typed', 1]}))]:
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (POLICY_PREFIX + owner, raw))
    conn.commit()
    backfill_lineage_index(conn)
    assert _index(conn, 'owner') == ['owner', 'head']
    assert _index(conn, 'single') is None and _index(conn, 'typed') is None
    assert conn.execute('SELECT 1 FROM state_meta WHERE key=?', (LINEAGE_BACKFILL_DONE,)).fetchone()
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        backfill_lineage_index(conn)
    finally:
        conn.set_trace_callback(None)
    assert len(statements) == 1


def test_backfill_open_failure_retries_and_read_only_never_runs_it(db):
    path = db.db_path
    conn = db._conn
    conn.execute('DELETE FROM state_meta WHERE key=?', (LINEAGE_BACKFILL_DONE,))
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                 (POLICY_PREFIX + 'owner', json.dumps({'lineage': ['owner', 'head']})))
    conn.commit()
    with patch('hermes_state_schema.backfill_lineage_index', side_effect=RuntimeError('busy')) as backfill:
        reader = SessionDB(path, read_only=True)
        reader.close()
        backfill.assert_not_called()
        writer = SessionDB(path)
        writer.close()
        backfill.assert_called_once()
    writer = SessionDB(path)
    try:
        assert _index(writer._conn, 'owner') == ['owner', 'head']
    finally:
        writer.close()


def test_failed_backfill_rolls_back_marker_and_entries(db):
    conn = db._conn
    conn.execute('DELETE FROM state_meta WHERE key=?', (LINEAGE_BACKFILL_DONE,))
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                 (POLICY_PREFIX + 'owner', json.dumps({'lineage': ['owner', 'head']})))
    conn.commit()
    with patch('hermes_state_local.sync_lineage_index', side_effect=RuntimeError('failed')):
        with pytest.raises(RuntimeError, match='failed'):
            backfill_lineage_index(conn)
    assert not conn.in_transaction
    assert _index(conn, 'owner') is None
    assert conn.execute('SELECT 1 FROM state_meta WHERE key=?', (LINEAGE_BACKFILL_DONE,)).fetchone() is None


def _old_scan(conn):
    superseded, heads = set(), {}
    rows = conn.execute("SELECT value FROM state_meta WHERE key GLOB ? AND json_valid(value) "
                        "AND json_array_length(value, '$.lineage') > 1", (POLICY_PREFIX + '*',)).fetchall()
    for (raw,) in rows:
        receipt = json.loads(raw)
        owner, lineage = receipt.get('session_id'), receipt.get('lineage')
        if not isinstance(owner, str) or not all(isinstance(sid, str) for sid in lineage):
            continue
        marks = ','.join('?' * len(lineage))
        ended = dict(conn.execute(f'SELECT id,end_reason FROM sessions WHERE id IN ({marks})', lineage).fetchall())
        starts = [sid for i, sid in enumerate(lineage) if i == 0 or ended.get(lineage[i - 1]) == 'session_reset']
        superseded.update(starts[:-1])
        heads[starts[-1]] = (owner, lineage)
    return superseded, heads


def test_projection_matches_old_scan_and_listing_without_policy_value_scan(db):
    conn = db._conn
    for owner, reasons in [('reset', ['session_reset', None]),
                           ('compress', ['compression', None]),
                           ('mixed', ['compression', 'session_reset', 'compression', None])]:
        lineage = [owner + str(i) for i in range(len(reasons))]
        for sid, reason in zip(lineage, reasons):
            db.create_session(sid, source='cli')
            conn.execute('UPDATE sessions SET end_reason=? WHERE id=?', (reason, sid))
        store_receipt(conn, owner, lineage)
    conn.commit()
    expected = _old_scan(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        assert local_lineage_index(conn) == expected
    finally:
        conn.set_trace_callback(None)
    assert not any('GLOB' in sql and POLICY_PREFIX + '*' in sql for sql in statements)
    with patch('hermes_state_local.local_lineage_index', _old_scan):
        old_rows = db.list_sessions_rich()
    # Force a fresh projection after switching implementations.
    db._local_lineage_cache[0].clear()
    assert db.list_sessions_rich() == old_rows
    assert any(row.get('_lineage_ids') for row in old_rows)
