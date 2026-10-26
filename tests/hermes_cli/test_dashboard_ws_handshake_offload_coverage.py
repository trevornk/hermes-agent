"""Regression coverage for the in-flight message-read coalescing in
hermes_cli/web_routers/sessions.py (dashboard_ws_handshake_offload.patch).

A reconnect storm can stack many identical /api/sessions/{id}/messages
requests; these must collapse into one DB read (`_coalesced_to_thread`),
while requests that differ in any key parameter (e.g. session_id) must
never share a result -- and a client disconnecting must not cancel a read
another concurrent caller is still waiting on.
"""

import asyncio
import threading

import pytest

from hermes_cli.web_routers import sessions as sessions_router


class _FakeDB:
    """Minimal SessionDB stand-in for the coalescing tests below."""

    def __init__(self, content="hi"):
        self._content = content

    def resolve_session_id(self, sid):
        return sid

    def resolve_resume_session_id(self, sid):
        return sid

    def get_messages(self, sid, **kw):
        return [{"role": "user", "content": self._content}]


@pytest.mark.asyncio
async def test_concurrent_identical_message_reads_share_one_db_call(monkeypatch):
    """The exact defect the commit message describes: a reconnect loop that
    stacks N identical /api/sessions/{id}/messages requests must not open N
    separate DB reads. Two concurrent calls with identical
    (profile, session_id, limit, offset, order, include_compacted) must
    collapse into exactly one call to `_with_db`.

    `_coalesced_to_thread` registers the shared future synchronously (before
    its first await), so the first task's `_with_db` call is guaranteed to
    be registered in `_inflight_message_reads` before the second task's
    coroutine body runs past its own lookup -- this relies on asyncio's
    cooperative-scheduling guarantee (a task only yields at an `await`
    point, and `_coalesced_to_thread` is a plain synchronous function whose
    entire body -- including the dict write -- runs to completion before its
    result is awaited), not on real-thread execution speed.
    """
    sessions_router._inflight_message_reads.clear()

    calls = []

    def _fake_with_db(profile, fn, *, read_only):
        calls.append(profile)
        return fn(_FakeDB())

    monkeypatch.setattr(sessions_router, "_with_db", _fake_with_db)

    async def _fire():
        return await sessions_router.get_session_messages(
            "20260830_180820_744f05", None, None, 0, None, False
        )

    t1 = asyncio.create_task(_fire())
    t2 = asyncio.create_task(_fire())
    r1, r2 = await asyncio.gather(t1, t2)

    assert r1["session_id"] == "20260830_180820_744f05"
    assert r2 == r1
    assert len(calls) == 1, (
        f"expected exactly 1 DB read for 2 identical concurrent requests, "
        f"got {len(calls)} -- the coalescing behavior added by "
        f"dashboard_ws_handshake_offload.patch is not working"
    )

    sessions_router._inflight_message_reads.clear()


@pytest.mark.asyncio
async def test_differing_params_are_not_incorrectly_coalesced(monkeypatch):
    """Coalescing must key on the full parameter tuple: two requests for
    different session_ids that are GENUINELY IN FLIGHT AT THE SAME TIME must
    not share a result (that would leak one session's transcript into
    another's response).

    A purely sequential ``await``, ``await`` pair cannot prove this --
    `_inflight_message_reads` would always be empty by the time the second
    call starts, so it can never exercise the "two different keys both
    present in the dict simultaneously" branch. This test forces genuine
    overlap deterministically (no reliance on thread/executor speed):
    `_coalesced_to_thread` runs the DB read via `asyncio.to_thread`, i.e. a
    real OS worker thread. `_with_db`'s own `profile` argument is the
    request's `profile` query param (always `None` for both calls here, not
    the session id), so the fake identifies "the first call in flight" by
    call order, not by inspecting `profile`: the "session-a" read is issued
    first and, deterministically, is the one still blocked (on a plain
    `threading.Event`) when "session-b" is issued and resolves -- proving
    both keys are in-flight in `_inflight_message_reads` at once, not that
    one merely happened to finish before the other started.
    """
    sessions_router._inflight_message_reads.clear()

    calls = []
    first_call_entered = threading.Event()
    release_first_call = threading.Event()

    def _fake_with_db(profile, fn, *, read_only):
        is_first_call = len(calls) == 0
        calls.append(profile)
        if is_first_call:
            first_call_entered.set()
            # Block the real worker thread (asyncio.to_thread) until the
            # test proves the second request ("session-b") was issued and
            # resolved while the first ("session-a") was still in-flight --
            # deterministic overlap, not a speed assumption.
            assert release_first_call.wait(timeout=5), "test deadlocked waiting for release"
        return fn(_FakeDB())

    monkeypatch.setattr(sessions_router, "_with_db", _fake_with_db)

    async def _fire(session_id):
        return await sessions_router.get_session_messages(
            session_id, None, None, 0, None, False
        )

    t_a = asyncio.create_task(_fire("session-a"))
    await asyncio.get_event_loop().run_in_executor(None, first_call_entered.wait, 5)
    assert ("messages", None, "session-a", None, 0, None, False) in (
        sessions_router._inflight_message_reads
    ), "session-a must be in flight before session-b starts, to prove real overlap"

    t_b = asyncio.create_task(_fire("session-b"))
    r_b = await t_b  # "b" does not block, so it resolves independently of "a"

    assert r_b["session_id"] == "session-b"

    release_first_call.set()
    r_a = await t_a

    assert r_a["session_id"] == "session-a"
    assert len(calls) == 2, "distinct session_ids must not be coalesced into one DB read"
    assert r_a != r_b, "results for two different session_ids must not be identical/leaked"

    sessions_router._inflight_message_reads.clear()


@pytest.mark.asyncio
async def test_one_client_disconnecting_does_not_cancel_the_shared_read():
    """`_coalesced_to_thread` explicitly shields the in-flight future so a
    cancelled caller (client disconnect) cannot cancel a read another
    concurrent caller is still waiting on. Regression for silently losing
    the second reader's response mid-reconnect-storm."""
    sessions_router._inflight_message_reads.clear()

    async def _slow_read():
        await asyncio.sleep(0.05)
        return "12345", 500, [{"role": "user", "content": "hi"}]

    key = ("messages", None, "sid-x", None, 0, None, False)

    # Manually seed the in-flight future the way _coalesced_to_thread would,
    # using a real coroutine so we can cancel one waiter without killing it.
    fut = asyncio.ensure_future(_slow_read())
    sessions_router._inflight_message_reads[key] = fut

    async def _second_waiter():
        return await sessions_router._coalesced_to_thread(key, lambda: None)

    waiter_task = asyncio.create_task(_second_waiter())
    await asyncio.sleep(0)  # let it attach to the shielded future
    waiter_task.cancel()
    try:
        await waiter_task
    except asyncio.CancelledError:
        pass

    # The original future must still complete successfully -- cancelling one
    # shielded waiter must not propagate a CancelledError into `fut`.
    result = await fut
    assert result == ("12345", 500, [{"role": "user", "content": "hi"}])
    assert not fut.cancelled()

    sessions_router._inflight_message_reads.clear()
