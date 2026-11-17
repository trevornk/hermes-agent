"""Headless Kanban workers must not leave cron origin delivery to home fallback."""

import json

import pytest

from cron.jobs import get_job
from tools.cronjob_tools import registry


@pytest.fixture(autouse=True)
def cron_worker_context(tmp_path, monkeypatch):
    # Same isolated job-store setup as test_cronjob_tools.
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    monkeypatch.setattr("tools.cronjob_tools._gateway_liveness_notice", lambda: {"gateway_running": True})
    from gateway.session_context import reset_session_vars, set_session_vars

    tokens = set_session_vars(cron_session="")
    yield
    reset_session_vars()


def create_job(**kwargs):
    result = json.loads(registry.dispatch("cronjob_manage", {
        "action": "create", "prompt": "Report status", "schedule": "every 1h", **kwargs,
    }))
    assert result["success"] is True
    return result, get_job(result["job_id"])


def assert_local_resolution(result, field):
    note = result["delivery_resolved"]
    assert field in note
    assert "local" in note
    assert "saved" in note
    assert "not pushed anywhere" in note
    assert "platform:chat_id" in note
    assert "local-only cron job" not in result.get("message", "")


@pytest.mark.parametrize("deliver,expected", [
    (None, "local"),
    ("origin", "local"),
    ("OrIgIn", "local"),
    ("origin,matrix:X", "local,matrix:X"),
    ("matrix:X,origin,local,origin,matrix:X", "matrix:X,local"),
])
def test_worker_create_resolves_origin(deliver, expected):
    result, stored = create_job(deliver=deliver)
    assert stored["deliver"] == expected
    assert stored["origin"] is None
    assert result["deliver"] == expected
    assert_local_resolution(result, "deliver")


def test_worker_create_explicit_target_unchanged():
    result, stored = create_job(deliver="matrix:X")
    assert stored["deliver"] == "matrix:X"
    assert stored.get("failure_deliver") is None
    assert "delivery_resolved" not in result


def test_worker_create_failure_origin_resolves_local():
    result, stored = create_job(deliver="matrix:X", failure_deliver="origin")
    assert stored["deliver"] == "matrix:X"
    assert stored["failure_deliver"] == "local"
    assert_local_resolution(result, "failure_deliver")


@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("deliver", [None, "origin"])
def test_push_origin_unchanged(monkeypatch, worker, deliver):
    from gateway.session_context import reset_session_vars, set_session_vars

    if worker:
        # The normal worker capability rejects async delivery even with chat IDs.
        # Model the exceptional worker with a real push-capable origin.
        monkeypatch.setattr("gateway.session_context.async_delivery_supported", lambda: True)
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK")
    tokens = set_session_vars(platform="matrix", chat_id="desktop-owner", cron_session="")
    try:
        result, stored = create_job(deliver=deliver, failure_deliver="origin")
    finally:
        reset_session_vars()
    assert stored["deliver"] == "origin"
    assert stored["failure_deliver"] == "origin"
    assert stored["origin"]["platform"] == "matrix"
    assert stored["origin"]["chat_id"] == "desktop-owner"
    assert "delivery_resolved" not in result


@pytest.mark.parametrize("deliver,expected", [(None, "local"), ("origin", "origin")])
def test_non_kanban_headless_unchanged(monkeypatch, deliver, expected):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    result, stored = create_job(deliver=deliver, failure_deliver="origin")
    assert stored["deliver"] == expected
    assert stored["failure_deliver"] == "origin"
    assert stored["origin"] is None
    assert "delivery_resolved" not in result


@pytest.mark.parametrize("target", ["matrix", ""])
@pytest.mark.parametrize("deliver", [None, "origin"])
def test_cron_session_keeps_creator_delivery(target, deliver):
    from gateway.session_context import reset_session_vars, set_session_vars, _VAR_MAP

    tokens = set_session_vars(cron_session="1")
    platform_token = _VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"].set(target)
    chat_token = _VAR_MAP["HERMES_CRON_AUTO_DELIVER_CHAT_ID"].set("alerts")
    thread_token = _VAR_MAP["HERMES_CRON_AUTO_DELIVER_THREAD_ID"].set("")
    try:
        _, stored = create_job(deliver=deliver)
    finally:
        _VAR_MAP["HERMES_CRON_AUTO_DELIVER_THREAD_ID"].reset(thread_token)
        _VAR_MAP["HERMES_CRON_AUTO_DELIVER_CHAT_ID"].reset(chat_token)
        _VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"].reset(platform_token)
        reset_session_vars()
    expected = "matrix:alerts" if target else "local"
    assert stored["deliver"] == expected
    assert stored["failure_deliver"] == expected


@pytest.mark.parametrize("field", ["deliver", "failure_deliver"])
@pytest.mark.parametrize("value,expected", [
    ("ORIGIN", "local"),
    ("origin,matrix:X,local", "local,matrix:X"),
    ("matrix:X", "matrix:X"),
])
def test_worker_update_delivery(field, value, expected):
    created, _ = create_job(deliver="matrix:original", failure_deliver="matrix:failures")
    result = json.loads(registry.dispatch("cronjob_manage", {
        "action": "update", "job_id": created["job_id"], field: value,
    }))
    assert result["success"] is True
    stored = get_job(created["job_id"])
    assert stored[field] == expected
    other = "failure_deliver" if field == "deliver" else "deliver"
    assert stored[other] == ("matrix:failures" if other == "failure_deliver" else "matrix:original")
    if "origin" in value.lower():
        assert_local_resolution(result, field)
    else:
        assert "delivery_resolved" not in result


def test_worker_update_omitted_deliver_and_clear_failure():
    created, _ = create_job(deliver="matrix:X", failure_deliver="matrix:failures")
    result = json.loads(registry.dispatch("cronjob_manage", {
        "action": "update", "job_id": created["job_id"], "deliver": None,
        "failure_deliver": "",
    }))
    assert result["success"] is True
    stored = get_job(created["job_id"])
    assert stored["deliver"] == "matrix:X"
    assert stored.get("failure_deliver") is None
    assert "delivery_resolved" not in result


def test_worker_non_push_session_resolves_local():
    from gateway.session_context import reset_session_vars, set_session_vars

    tokens = set_session_vars(platform="api_server", chat_id="request", async_delivery=False)
    try:
        result, stored = create_job(deliver="origin", failure_deliver="origin")
    finally:
        reset_session_vars()
    assert stored["origin"] is None
    assert stored["deliver"] == stored["failure_deliver"] == "local"
    assert_local_resolution(result, "deliver, failure_deliver")


def test_worker_update_omitted_deliver_preserves_existing_origin(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    created, _ = create_job(deliver="origin")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    result = json.loads(registry.dispatch("cronjob_manage", {
        "action": "update", "job_id": created["job_id"], "name": "Renamed",
    }))
    assert result["success"] is True
    assert get_job(created["job_id"])["deliver"] == "origin"
    assert "delivery_resolved" not in result
