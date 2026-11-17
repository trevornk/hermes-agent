"""Tests for gateway/mirror.py — session mirroring."""

import importlib
import json
import logging

import pytest
from unittest.mock import patch, MagicMock

import gateway.mirror as mirror_mod
from gateway.mirror import (
    mirror_to_session,
    _find_session_id,
)


def _setup_sessions(tmp_path, sessions_data):
    """Helper to write a fake sessions.json and patch module-level paths."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    index_file = sessions_dir / "sessions.json"
    index_file.write_text(json.dumps(sessions_data), encoding="utf-8")
    return sessions_dir, index_file


class TestFindSessionId:
    def test_finds_matching_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "agent:main:telegram:dm": {
                "session_id": "sess_abc",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-01-01T00:00:00",
            }
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "12345")

        assert result == "sess_abc"

    def test_returns_most_recent(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "old": {
                "session_id": "sess_old",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "new": {
                "session_id": "sess_new",
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "12345")

        assert result == "sess_new"

    def test_thread_id_disambiguates_same_chat(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "topic_a": {
                "session_id": "sess_topic_a",
                "origin": {"platform": "telegram", "chat_id": "-1001", "thread_id": "10"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "topic_b": {
                "session_id": "sess_topic_b",
                "origin": {"platform": "telegram", "chat_id": "-1001", "thread_id": "11"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = _find_session_id("telegram", "-1001", thread_id="10")

        assert result == "sess_topic_a"


class TestMirrorToSession:


    def test_successful_mirror_uses_user_id_for_group_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "alice": {
                "session_id": "sess_alice",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "alice"},
                "updated_at": "2026-01-01T00:00:00",
            },
            "bob": {
                "session_id": "sess_bob",
                "origin": {"platform": "telegram", "chat_id": "-1001", "user_id": "bob"},
                "updated_at": "2026-02-01T00:00:00",
            },
        })

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("gateway.mirror._append_to_sqlite") as mock_sqlite:
            result = mirror_to_session(
                "telegram",
                "-1001",
                "Hello group!",
                source_label="cli",
                user_id="alice",
            )

        assert result is True
        mock_sqlite.assert_called_once()
        assert mock_sqlite.call_args[0][0] == "sess_alice"

    def test_no_matching_session(self, tmp_path):
        sessions_dir, index_file = _setup_sessions(tmp_path, {})

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file):
            result = mirror_to_session("telegram", "99999", "Hello!")

        assert result is False


    def test_failed_sqlite_write_reports_false(self, tmp_path):
        """A mirror whose transcript write raises must not report success (#10130)."""
        sessions_dir, index_file = _setup_sessions(tmp_path, {
            "dm": {
                "session_id": "sess_dm",
                "origin": {"platform": "telegram", "chat_id": "123"},
                "updated_at": "2026-01-01T00:00:00",
            },
        })
        broken_db = MagicMock()
        broken_db.find_session_by_origin.return_value = None  # resolve via sessions.json
        broken_db.append_message.side_effect = OSError("disk full")

        with patch.object(mirror_mod, "_SESSIONS_DIR", sessions_dir), \
             patch.object(mirror_mod, "_SESSIONS_INDEX", index_file), \
             patch("hermes_state_registry.acquire", return_value=broken_db), \
             patch("hermes_state_registry.release_or_close"):
            result = mirror_to_session("telegram", "123", "Hello!")

        assert result is False
        broken_db.append_message.assert_called_once()


class TestAppendToSqlite:
    def test_connection_is_released_after_use(self, tmp_path):
        """Verify _append_to_sqlite returns the shared SessionDB reference."""
        from gateway.mirror import _append_to_sqlite
        mock_db = MagicMock()
        released = []

        with patch("hermes_state_registry.acquire", return_value=mock_db), \
             patch(
                 "hermes_state_registry.release_or_close",
                 side_effect=lambda db: released.append(db),
             ):
            _append_to_sqlite("sess_1", {"role": "assistant", "content": "hello"})

        mock_db.append_message.assert_called_once()
        assert released == [mock_db], (
            "the shared handle must be released exactly once after use"
        )


class TestSessionsIndexProfileScoping:
    """#112844: the fallback index must follow the active profile, not the launch one."""

    @staticmethod
    def _write_index(home, session_id):
        d = home / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        (d / "sessions.json").write_text(json.dumps({
            "agent:main:telegram:dm": {
                "session_id": session_id,
                "origin": {"platform": "telegram", "chat_id": "12345"},
                "updated_at": "2026-01-01T00:00:00",
            }
        }), encoding="utf-8")

    def test_fallback_follows_active_profile_home(self, tmp_path, monkeypatch):
        """A profile switched in after import must be read, not the launch profile's index.

        The module captures ``sessions.json`` under the home that was live at import. Under the
        multiplexed gateway one process serves every profile, so a lookup for another profile
        must not resolve against the launch profile's index and return its session id.
        """
        launch_home, active_home = tmp_path / "launch", tmp_path / "active"
        self._write_index(launch_home, "sess_launch")
        self._write_index(active_home, "sess_active")

        # Re-import the module with the launch home live: this is the import-time capture.
        monkeypatch.setenv("HERMES_HOME", str(launch_home))
        importlib.reload(mirror_mod)
        try:
            # A request for a different profile is now served by the same process.
            monkeypatch.setenv("HERMES_HOME", str(active_home))
            assert mirror_mod._find_session_id("telegram", "12345") == "sess_active"
        finally:
            monkeypatch.undo()
            importlib.reload(mirror_mod)

    def test_patched_constant_still_wins(self, tmp_path, monkeypatch):
        """Tests that patch ``_SESSIONS_INDEX`` keep overriding the live home."""
        patched = tmp_path / "patched"
        self._write_index(patched, "sess_patched")

        monkeypatch.setattr(mirror_mod, "_SESSIONS_INDEX", patched / "sessions" / "sessions.json")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))

        assert mirror_mod._find_session_id("telegram", "12345") == "sess_patched"


@pytest.mark.parametrize("optional", [False, True])
def test_absent_transcript_log_level(tmp_path, monkeypatch, caplog, optional):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with caplog.at_level(logging.DEBUG, logger="gateway.mirror"):
        assert mirror_mod.mirror_to_session(
            "telegram", "absent", "hello", **({"optional": True} if optional else {}),
        ) is False
    misses = [r for r in caplog.records if "no session found" in r.message]
    assert len(misses) == 1
    assert misses[0].levelno == (logging.DEBUG if optional else logging.WARNING)
    assert not optional or not any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("user_id", [None, "unknown"])
@pytest.mark.parametrize("backend", ["json", "sqlite"])
def test_optional_ambiguous_participants_never_write(tmp_path, monkeypatch, caplog, user_id, backend):
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "test.db")
    entries = {
        user: {"session_id": user, "origin": {"platform": "telegram", "chat_id": "group", "user_id": user}}
        for user in ("alice", "bob")
    }
    if backend == "sqlite":
        for user in entries:
            db.create_session(user, "telegram", user_id=user, chat_id="group", session_key=f"group:{user}")
        # A stale, apparently unique JSON entry must not override the DB's refusal.
        entries.pop("bob")
        assert db.find_session_by_origin(platform="telegram", chat_id="group", user_id=user_id) is None
    _, index = _setup_sessions(tmp_path, entries)
    monkeypatch.setattr(mirror_mod, "_SESSIONS_INDEX", index)
    try:
        with patch("hermes_state_registry.acquire", return_value=db), \
             patch("hermes_state_registry.release_or_close"), \
             patch.object(mirror_mod, "_append_to_sqlite") as append:
            assert mirror_mod._find_session_id("telegram", "group", user_id=user_id) is None
            assert mirror_mod.mirror_to_session("telegram", "group", "hello", user_id=user_id, optional=True) is False
        append.assert_not_called()
        assert any(r.levelno == logging.WARNING and "ambiguous" in r.message.lower() for r in caplog.records)
    finally:
        db.close()


def test_optional_lookup_failure_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = MagicMock()
    db.find_session_by_origin.side_effect = OSError("lookup broken")
    with patch("hermes_state_registry.acquire", return_value=db), \
         patch("hermes_state_registry.release_or_close"):
        assert mirror_mod.mirror_to_session("telegram", "absent", "hello", optional=True) is False
    assert any(r.levelno == logging.WARNING and "lookup broken" in r.message for r in caplog.records)


@pytest.mark.parametrize("failure", ["corrupt", "unreadable"])
def test_optional_bad_sessions_index_warns(tmp_path, monkeypatch, caplog, failure):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _, index = _setup_sessions(tmp_path, {})
    monkeypatch.setattr(mirror_mod, "_SESSIONS_INDEX", index)
    if failure == "corrupt":
        index.write_text("{bad json", encoding="utf-8")
    else:
        index.unlink()
        index.mkdir()
    assert mirror_mod.mirror_to_session("telegram", "absent", "hello", optional=True) is False
    assert any(r.levelno == logging.WARNING and "sessions.json" in r.message for r in caplog.records)


@pytest.mark.parametrize("explicit", [False, True])
def test_optional_existing_transcript_mirrors(tmp_path, monkeypatch, explicit):
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "test.db")
    db.create_session("existing", "telegram", chat_id="123", session_key="dm:123")
    try:
        with patch("hermes_state_registry.acquire", return_value=db), \
             patch("hermes_state_registry.release_or_close"):
            assert mirror_mod.mirror_to_session(
                "telegram", "123", "hello", optional=True, session_id="existing" if explicit else None,
            ) is True
        assert [m["content"] for m in db.get_messages("existing")] == ["hello"]
    finally:
        db.close()


@pytest.mark.parametrize("session_id", [None, "explicit"])
def test_optional_sqlite_write_failure_warns(tmp_path, monkeypatch, caplog, session_id):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = MagicMock()
    db.find_session_by_origin.return_value = "existing"
    db.append_message.side_effect = OSError("disk full")
    with patch("hermes_state_registry.acquire", return_value=db), \
         patch("hermes_state_registry.release_or_close"):
        assert mirror_mod.mirror_to_session(
            "telegram", "123", "hello", optional=True, session_id=session_id,
        ) is False
    assert any(r.levelno == logging.WARNING and "disk full" in r.message for r in caplog.records)
