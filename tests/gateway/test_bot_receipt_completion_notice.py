"""The session_bot mailbox scan shares ``runtime/bot_live_delivery`` with no-agent cron completion
notices (``deliver_completion_notice``). It must recognise a well-formed notice and skip it without a
warning (session_bot never consumes one), still flag a corrupt notice, and keep judging every other
receipt shape exactly as before. Real writer, real dir, real locked scan."""
import json
import logging

from hermes_state import SessionDB

KEY = 'c' * 32


def _bot_chat(home):
    db = SessionDB(db_path=home / 'state.db')
    try:
        db.create_session('bot', 'cli')
        db.set_session_title('bot', 'Bot Chat')
    finally:
        db.close()


def _scan(home, caplog):
    from gateway.session_bot_mailbox import _scan_receipts
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger='gateway.session_bot'):
        root, records = _scan_receipts(home)
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING and 'Skipping malformed Bot mailbox' in r.getMessage()]
    return root, records, warnings


def _put(root, key, record):
    (root / f'{key}.json').write_text(json.dumps(record))


def test_valid_completion_notice_is_skipped_without_a_warning(tmp_path, caplog):
    from tools.bot_live_delivery import deliver_completion_notice
    _bot_chat(tmp_path)
    deliver_completion_notice(tmp_path, 'Cronjob Response: scout completed', delivery_id=KEY)

    _, records, warnings = _scan(tmp_path, caplog)
    assert warnings == []
    assert records == []  # recognised, but never handed to session_bot's recovery


def test_corrupt_completion_notice_is_still_flagged(tmp_path, caplog):
    from tools.bot_live_delivery import deliver_completion_notice
    _bot_chat(tmp_path)
    notice = deliver_completion_notice(tmp_path, 'Cronjob Response: scout completed', delivery_id=KEY)
    root, _, _ = _scan(tmp_path, caplog)
    _put(root, KEY, {**notice, 'target': {'profile_home': str(tmp_path)}})

    _, records, warnings = _scan(tmp_path, caplog)
    assert records == []
    assert len(warnings) == 1 and 'completion-notice target is incomplete' in warnings[0]


def test_other_receipt_shapes_are_judged_as_before(tmp_path, caplog):
    from tools.bot_live_delivery import deliver_completion_notice
    _bot_chat(tmp_path)
    deliver_completion_notice(tmp_path, 'Cronjob Response: scout completed', delivery_id=KEY)
    root, _, _ = _scan(tmp_path, caplog)
    home = str(tmp_path.resolve())
    canonical = {'delivery_id': 'a' * 32, 'admission_id': 'adm', 'profile_home': home, 'session_id': 'bot',
                 'principal_id': 'p', 'message': 'hi', 'status': 'settled'}
    legacy = {'delivery_id': 'b' * 32, 'id': 'b' * 32, 'status': 'queued', 'created_at': 1, 'sequence': 1,
              'owner': {'profile_home': home, 'session_id': 'bot', 'lease_id': 'l', 'live_session_id': 'v'}}
    _put(root, 'a' * 32, canonical)
    _put(root, 'b' * 32, legacy)
    _put(root, 'd' * 32, {'delivery_id': 'd' * 32, 'message': 'no schema'})
    _put(root, 'e' * 32, {**legacy, 'delivery_id': 'e' * 32, 'id': 'e' * 32, 'owner': {}})

    _, records, warnings = _scan(tmp_path, caplog)
    assert sorted(p.stem for p, _ in records) == ['a' * 32, 'b' * 32]
    assert sorted(warnings) == sorted([
        f"Skipping malformed Bot mailbox receipt {root / ('d' * 32 + '.json')} (unknown receipt schema)",
        f"Skipping malformed Bot mailbox receipt {root / ('e' * 32 + '.json')} (owner pin is incomplete)",
    ])
