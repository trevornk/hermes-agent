"""Authenticated Desktop cron origins using the existing durable completion ledger.

No caller-supplied session/env identity can commission this route. The active
Desktop runtime record supplies its profile, authenticated principal and lineage.
Native worker execution remains the scheduler/supervisor's responsibility.
"""
from __future__ import annotations

from contextlib import closing

import hashlib
import json
import logging
import os
import sqlite3
import sys
import time

from hermes_constants import get_hermes_home

PLATFORM = 'desktop-owner'
_ENDED_ORIGIN_DROP_GRACE_S = 7 * 86400
_ENDED_ORIGIN_ALERT_GRACE_S = 3600
logger = logging.getLogger(__name__)


class _OriginGone(ValueError):
    def __init__(self, message, reason, ended_at=None):
        super().__init__(message)
        self.reason = reason
        self.ended_at = ended_at


def capture_origin():
    # Do not import a new UI runtime in a CLI/cron subprocess. This ContextVar is
    # bound only around an admitted Desktop turn, never exported as environment.
    server = sys.modules.get('tui_gateway.server')
    context = getattr(server, '_current_runtime_session_record', None)
    session = context.get() if context is not None else None
    if not session or session.get('_finalized'):
        return None
    principal = server._session_auth_user_id(session)
    sid = str(getattr(session.get('agent'), 'session_id', '') or session.get('session_key') or '')
    home = server._session_home(session).resolve()
    if not principal or not sid or home != get_hermes_home().resolve():
        return None
    from hermes_state import SessionDB
    db = SessionDB(home / 'state.db')
    try:
        row = db.get_session(sid)
        if row is None or row.get('user_id') != principal or row.get('source') not in ('desktop', 'tui', 'gui'):
            return None
    finally:
        db.close()
    return {'platform': PLATFORM, 'chat_id': sid, 'profile_home': str(home), 'principal_id': principal}


def creation_origin(supplied):
    captured = capture_origin()
    if isinstance(supplied, dict) and supplied.get('platform') == PLATFORM and supplied != captured:
        raise ValueError('Desktop origin requires an authenticated originating turn')
    return captured if captured is not None else supplied


def validate_origin_update(previous, supplied):
    if isinstance(previous, dict) and previous.get('platform') == PLATFORM and supplied != previous:
        raise ValueError('Desktop originating owner cannot be changed')
    if isinstance(supplied, dict) and supplied.get('platform') == PLATFORM and supplied != previous:
        raise ValueError('Desktop origin may only be captured at creation')


def _validate_origin(db, origin):
    if (not isinstance(origin, dict) or set(origin) != {'platform', 'chat_id', 'profile_home', 'principal_id'}
            or origin['platform'] != PLATFORM
            or origin['profile_home'] != str(get_hermes_home().resolve())
            or not all(isinstance(v, str) and v for v in origin.values())):
        raise ValueError('Desktop origin profile/binding mismatch')
    if db is None:
        raise ValueError('Desktop origin database unavailable')
    root = db.get_session(origin['chat_id'])
    tip = db.get_compression_tip(origin['chat_id']) or origin['chat_id']
    row = db.get_session(tip)
    if any(r is None or r.get('user_id') != origin['principal_id']
           or r.get('source') not in ('desktop', 'tui', 'gui') for r in (root, row)):
        raise _OriginGone('Desktop origin identity/lineage no longer exists', 'origin_identity_gone')
    if row.get('expiry_finalized') or (row.get('ended_at') and row.get('end_reason') != 'compression'):
        raise _OriginGone('Desktop origin has ended', 'origin_ended', row.get('ended_at'))
    return tip


def idempotent_queue_target(job, for_failure=False):
    """Only the internal, idempotent publication may recover a claimed handoff."""
    from cron.scheduler_delivery import _resolve_delivery_targets
    targets = _resolve_delivery_targets(job, for_failure=for_failure)
    return bool(targets) and all(t.get('platform') == PLATFORM for t in targets)


def payload_digest(event):
    return hashlib.sha256(json.dumps({k: event.get(k) for k in
        ('cron_origin', 'summary', 'goal', 'status')}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def compact_completion_history(conn, cutoff, keep=1000):
    """Bound terminal payload history while keeping compact identity tombstones."""
    rows = conn.execute("""SELECT delegation_id,event_json,result_json,updated_at FROM async_delegations
        WHERE delegation_id LIKE 'cron-owner-%' AND delivery_state IN ('delivered','dropped')
        AND json_extract(event_json,'$.cron_origin') IS NOT NULL ORDER BY updated_at DESC""").fetchall()
    for index, (key, event_raw, result_raw, updated) in enumerate(rows):
        if index < keep and updated >= cutoff:
            continue
        event, result = json.loads(event_raw), json.loads(result_raw)
        conn.execute("UPDATE async_delegations SET event_json=?,result_json=?,task_json='{}' WHERE delegation_id=?",
            (json.dumps({'payload_sha256': payload_digest(event)}),
             json.dumps({'owner_followthrough': result.get('owner_followthrough', {})}), key))


def _queue_ended_origin_alert(event):
    """Use the scheduler's durable explicit-target handoff, never a new transport."""
    from cron.delivery_queue import enqueue
    key, goal = event['delegation_id'], event['goal']
    alert_id = key + '-origin-ended-alert'
    try:
        receipt = enqueue(alert_id, {
            'id': alert_id, 'name': goal,
            'deliver': 'matrix:!ZudlWNSxyHvqGwIkdv:sppun.com',
        }, f"Desktop owner unavailable for {key} ({goal}):\n{event['summary']}", for_failure=True)
    except Exception:
        logger.warning("Desktop completion %s (%s) origin unavailable; failure alert unavailable", key, goal, exc_info=True)
        return 'unavailable'
    # Queued is not a delivery acknowledgment; the scheduler records the send outcome.
    return receipt['status']


def _ended_origin_action(db, event, result, now):
    """A current end is resumable; only identity loss or an aged end can expire a receipt."""
    try:
        _validate_origin(db, event.get('cron_origin'))
    except _OriginGone as exc:
        if exc.reason == 'origin_identity_gone':
            return exc.reason, True
        age = now - exc.ended_at if exc.ended_at is not None else 0
        if age > _ENDED_ORIGIN_DROP_GRACE_S:
            return exc.reason, True
        if (age > _ENDED_ORIGIN_ALERT_GRACE_S and event.get('status') == 'error'
                and 'alert' not in result.get('owner_followthrough', {})):
            return exc.reason, False
    except ValueError:
        pass  # Invalid profile/binding is not proof that a lineage ended.
    return None


def _settle_ended_origin_candidate(db, key, now):
    from tools import async_delegation as ledger
    with ledger._DB_LOCK, ledger._transaction() as conn:
        # Re-read both the claim and lineage after serializing with session/claim writes.
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("""SELECT event_json,result_json,delivery_claim,delivery_claimed_at
            FROM async_delegations WHERE delegation_id=? AND delivery_state=?
            AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (key, 'pending', now - ledger._CLAIM_LEASE_S)).fetchone()
        if row is None:
            return 0
        event, result = json.loads(row[0]), json.loads(row[1])
        action = _ended_origin_action(db, event, result, now)
        if action is None:
            return 0
        reason, drop = action
        followthrough = result['owner_followthrough']
        if event.get('status') == 'error' and 'alert' not in followthrough:
            # Commit the idempotent queue id first: a ledger rollback cannot duplicate a send.
            followthrough['alert'] = _queue_ended_origin_alert(event)
        if drop:
            followthrough.update(status='dropped', reason=reason, finished_at=now)
            logger.info("Desktop completion %s (%s) dropped: %s", key, event['goal'], reason)
        conn.execute("""UPDATE async_delegations SET delivery_state=?,result_json=?,
            updated_at=?,delivery_claim=?,delivery_claimed_at=? WHERE delegation_id=?""",
            ('dropped' if drop else 'pending', json.dumps(result), now,
             None if drop else row[2], None if drop else row[3], key))
    return 1


def sweep_ended_origin_completions():
    """Alert aged failures and expire old ended origins without requiring a loaded UI."""
    from hermes_state import SessionDB
    from tools import async_delegation as ledger
    path = ledger._db_path()
    if not path.exists():
        return 0
    now = time.time()
    # The usual empty tick must not initialize SessionDB or take state.db's writer lock.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        rows = conn.execute("""SELECT delegation_id,event_json,result_json FROM async_delegations
            WHERE delegation_id LIKE ? AND delivery_state=?
            AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            ('cron-owner-%', 'pending', now - ledger._CLAIM_LEASE_S)).fetchall()
    if not rows:
        return 0
    db = SessionDB(path)
    changed = 0
    try:
        for key, event_raw, result_raw in rows:
            if _ended_origin_action(db, json.loads(event_raw), json.loads(result_raw), now) is not None:
                changed += _settle_ended_origin_candidate(db, key, now)
    finally:
        db.close()
    return changed


def publish_completion(job, content, *, for_failure=False):
    """Idempotent terminal publication, not an owner-consumption acknowledgment."""
    from hermes_state import SessionDB
    from tools import async_delegation as ledger
    from cron.scheduler_delivery import _redact_cron_payload
    execution = job.get('execution_id')
    if not isinstance(execution, str) or not execution:
        raise ValueError('Desktop completion requires the durable execution id')
    origin = job.get('origin')
    db = SessionDB(get_hermes_home() / 'state.db')
    try:
        try:
            _validate_origin(db, origin)
        except _OriginGone:
            pass  # Persist for reopen; periodic housekeeping applies the alert/drop graces.
    finally:
        db.close()
    key = 'cron-owner-' + hashlib.sha256(json.dumps(
        [str(get_hermes_home().resolve()), job['id'], execution, bool(for_failure)],
        separators=(',', ':')).encode()).hexdigest()
    text = _redact_cron_payload(content, 'Desktop owner completion')
    goal = _redact_cron_payload(str(job.get('name') or job['id']), 'Desktop job name')
    status = 'error' if for_failure else 'completed'
    now = time.time()
    event = {'type': 'async_delegation', 'delegation_id': key,
             'session_key': origin['chat_id'], 'parent_session_id': origin['chat_id'],
             'origin_session_id': '', 'origin_ui_session_id': '', 'cron_origin': origin,
             'goal': goal, 'summary': text, 'status': status,
             'dispatched_at': now, 'completed_at': now}
    result = {'status': status, 'summary': text, 'owner_followthrough': {'status': 'pending'}}
    ledger._prune_durable_records()
    with ledger._DB_LOCK, ledger._transaction() as conn:
        prior = conn.execute('SELECT event_json,delivery_state FROM async_delegations WHERE delegation_id=?', (key,)).fetchone()
        if prior is not None:
            old = json.loads(prior[0])
            if old.get('payload_sha256', payload_digest(old)) != payload_digest(event):
                raise ValueError('Desktop completion identity already has a different payload')
            return {'delivery_id': key, 'status': prior[1]}
        conn.execute('''INSERT INTO async_delegations
            (delegation_id,origin_session,origin_ui_session_id,parent_session_id,state,
             dispatched_at,completed_at,updated_at,event_json,result_json,task_json,
             origin_session_id,delivery_state,delivery_attempts)
            VALUES(?,?,'',?,?,?,?,?,?,?,?,?,'pending',0)''',
            (key, origin['chat_id'], origin['chat_id'], status, now, now, now,
             json.dumps(event), json.dumps(result), json.dumps({'goal': goal}), ''))
    # Existing per-profile replay/poller observes this committed ledger, including
    # when publisher and Desktop are different processes or no viewer is attached.
    from tools.process_registry import process_registry
    process_registry.completion_queue.put(event)
    return {'delivery_id': key, 'status': 'pending'}


def belongs_to_session(server, session, event):
    origin = event.get('cron_origin')
    if (not isinstance(origin, dict) or session.get('_finalized')
            or origin.get('profile_home') != str(server._session_home(session).resolve())
            or origin.get('principal_id') != server._session_auth_user_id(session)):
        return False
    with server._session_db(session) as db:
        try:
            tip = _validate_origin(db, origin)
        except ValueError:
            return False
        current = str(getattr(session.get('agent'), 'session_id', '') or session.get('session_key') or '')
        return current == tip


def begin_followthrough(event, claim):
    """Fence model execution durably before starting. Ambiguous starts never replay."""
    from tools import async_delegation as ledger
    from gateway.status import get_process_start_time
    now = time.time()
    with ledger._DB_LOCK, ledger._transaction() as conn:
        row = conn.execute('SELECT result_json FROM async_delegations WHERE delegation_id=? AND delivery_state=\'pending\' AND delivery_claim=?',
                           (event['delegation_id'], claim)).fetchone()
        if row is None:
            return False
        result = json.loads(row[0])
        result['owner_followthrough'] = {'status': 'started', 'pid': os.getpid(),
                                         'birth': get_process_start_time(os.getpid()), 'started_at': now}
        conn.execute("UPDATE async_delegations SET delivery_state='started',result_json=?,updated_at=? WHERE delegation_id=? AND delivery_claim=?",
                     (json.dumps(result), now, event['delegation_id'], claim))
    return True


def reconcile_start_failure(event, claim):
    """Read the committed fence after a failed start write; never infer admission."""
    from tools import async_delegation as ledger
    with ledger._DB_LOCK, ledger._transaction() as conn:
        row = conn.execute('SELECT delivery_state FROM async_delegations WHERE delegation_id=? AND delivery_claim=?',
                           (event['delegation_id'], claim)).fetchone()
        if row is None:
            return None
        state = row[0]
        if state == 'pending':
            conn.execute("UPDATE async_delegations SET delivery_claim=NULL,delivery_claimed_at=NULL,delivery_attempts=MAX(0,delivery_attempts-1),updated_at=? WHERE delegation_id=? AND delivery_state='pending' AND delivery_claim=?",
                         (time.time(), event['delegation_id'], claim))
    if state == 'pending':
        ledger.return_completion_offer(event)
    return state


def settle_followthrough(event, claim, outcome):
    from tools import async_delegation as ledger
    with ledger._DB_LOCK, ledger._transaction() as conn:
        row = conn.execute("SELECT result_json FROM async_delegations WHERE delegation_id=? AND delivery_state='started' AND delivery_claim=?",
                           (event['delegation_id'], claim)).fetchone()
        if row is None:
            return
        result = json.loads(row[0])
        result['owner_followthrough'].update(status=outcome['status'], finished_at=time.time(),
            response_sha256=hashlib.sha256(str(outcome.get('text') or '').encode()).hexdigest())
        # Keep model failure distinct from worker failure and from queued/started.
        state = 'delivered' if outcome['status'] == 'settled' else 'dropped'
        conn.execute('UPDATE async_delegations SET delivery_state=?,result_json=?,delivered_at=?,updated_at=?,delivery_claim=NULL,delivery_claimed_at=NULL WHERE delegation_id=? AND delivery_claim=?',
                     (state, json.dumps(result), time.time(), time.time(), event['delegation_id'], claim))


def refund_unstarted(event, claim):
    from tools import async_delegation as ledger
    with ledger._DB_LOCK, ledger._transaction() as conn:
        row = conn.execute("SELECT result_json FROM async_delegations WHERE delegation_id=? AND delivery_state='started' AND delivery_claim=?",
                           (event['delegation_id'], claim)).fetchone()
        if row is None:
            return
        result = json.loads(row[0])
        result['owner_followthrough'] = {'status': 'pending'}
        conn.execute("UPDATE async_delegations SET delivery_state='pending',result_json=?,delivery_claim=NULL,delivery_claimed_at=NULL,delivery_attempts=MAX(0,delivery_attempts-1),updated_at=? WHERE delegation_id=? AND delivery_state='started' AND delivery_claim=?",
                     (json.dumps(result), time.time(), event['delegation_id'], claim))
    ledger.return_completion_offer(event)


def report_unknown(server, sid, session, event, claim):
    """Persist the escalation before clearing its recoverable obligation."""
    if not belongs_to_session(server, session, event):
        return
    key = event['delegation_id']
    notice = ('Native job completed, but its owner follow-through was interrupted and its outcome is unknown. '
              'It will not be replayed automatically. Review completion ' + key + '.')
    with server._session_db(session) as db:
        db.append_completion_notice(session['session_key'], notice, key + '-unknown')
    settle_followthrough(event, claim, {'status': 'unknown'})
    server._emit("cron.completion", sid, {'text': notice, 'id': key + '-unknown'})



def pending_for_session(server, session):
    """Read this owner's pending receipts independently of the process-wide offer queue.

    Another profile's consumer can take and return every shared-queue offer before
    this poller wakes. Reading the durable row here provides bounded progress; the
    existing delivery claim remains the sole admission/deduplication authority.
    """
    if session.get('running') or session.get('_finalized'):
        return []
    from tools import async_delegation as ledger
    # Never let an unbound caller read a different home's completion payloads.
    if server._session_home(session).resolve() != get_hermes_home().resolve():
        return []
    with ledger._DB_LOCK, ledger._transaction() as conn:
        rows = conn.execute("""WITH RECURSIVE lineage(id) AS (SELECT ? UNION
            SELECT s.parent_session_id FROM sessions s JOIN lineage l ON s.id=l.id
            JOIN sessions p ON p.id=s.parent_session_id WHERE p.end_reason='compression')
            SELECT event_json FROM async_delegations
            WHERE delivery_state='pending' AND delegation_id LIKE 'cron-owner-%'
            AND origin_session IN (SELECT id FROM lineage)
            AND (delivery_claim IS NULL OR delivery_claimed_at < ?)
            ORDER BY updated_at,delegation_id LIMIT 20""",
            (str(getattr(session.get('agent'), 'session_id', '') or session.get('session_key') or ''),
             time.time() - ledger._CLAIM_LEASE_S)).fetchall()
    events = [json.loads(raw) for (raw,) in rows]
    return [event for event in events if belongs_to_session(server, session, event)]


def recover_interrupted(server, session, sid=None):
    """Bounded existing-poller recovery: report uncertain model starts, never rerun."""
    if session.get('running'):
        return
    from tools import async_delegation as ledger
    with ledger._DB_LOCK, ledger._transaction() as conn:
        rows = conn.execute("""WITH RECURSIVE lineage(id) AS (SELECT ? UNION
            SELECT s.parent_session_id FROM sessions s JOIN lineage l ON s.id=l.id
            JOIN sessions p ON p.id=s.parent_session_id WHERE p.end_reason='compression')
            SELECT delegation_id,event_json,result_json,delivery_claim FROM async_delegations
            WHERE delivery_state='started' AND delegation_id LIKE 'cron-owner-%'
            AND origin_session IN (SELECT id FROM lineage) ORDER BY updated_at LIMIT 20""",
            (str(getattr(session.get('agent'), 'session_id', '') or session.get('session_key') or ''),)).fetchall()
    for _key, raw, result_raw, claim in rows:
        event, result = json.loads(raw), json.loads(result_raw)
        if not belongs_to_session(server, session, event):
            continue
        start = result['owner_followthrough']
        # Use the same birth-aware liveness predicate as the completion ledger.
        alive = ledger._owner_liveness()
        if alive is None:
            continue
        if alive(start['pid'], start['birth']) and time.time() - start['started_at'] < 300:
            continue  # bounded grace for a settled turn whose terminal write failed
        report_unknown(server, sid, session, event, claim)
