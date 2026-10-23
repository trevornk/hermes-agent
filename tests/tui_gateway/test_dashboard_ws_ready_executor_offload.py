"""Regression coverage for the dedicated `_READY_EXECUTOR` in
tui_gateway/ws.py (dashboard_ws_handshake_offload.patch).

The gateway.ready handshake's resolve_skin() call must dispatch through a
small dedicated thread pool (`_READY_EXECUTOR`), not the shared default
`asyncio.to_thread` pool -- so a reconnect storm saturating the shared pool
(e.g. from concurrent session-message reads) cannot stall the ready frame.
"""

import asyncio

import pytest

from tui_gateway import server, ws as ws_mod


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def accept(self):
        pass

    async def send_text(self, line):
        self.sent.append(line)

    async def receive_text(self):
        raise ws_mod._WebSocketDisconnect()

    async def close(self):
        pass


def test_ready_frame_uses_dedicated_executor_not_default_pool(monkeypatch):
    """The gateway.ready handshake must dispatch resolve_skin() through
    ws_mod._READY_EXECUTOR specifically -- not asyncio.to_thread's shared
    default executor -- so a saturated default pool (from concurrent
    session-message reads elsewhere in the dashboard) cannot stall the
    ready frame. This is the discriminating regression: it fails if the
    handshake is changed back to a bare asyncio.to_thread call."""
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)

    calls = []
    loop = asyncio.new_event_loop()
    real_run_in_executor = type(loop).run_in_executor
    loop.close()

    def _spy_run_in_executor(self, executor, func, *args):
        calls.append(executor)
        return real_run_in_executor(self, executor, func, *args)

    monkeypatch.setattr(type(loop), "run_in_executor", _spy_run_in_executor)
    monkeypatch.setattr(server, "resolve_skin", lambda: {"name": "default"})

    ws = _FakeWS()
    asyncio.run(ws_mod.handle_ws(ws))

    assert calls, "resolve_skin was never dispatched via run_in_executor at all"
    assert calls[0] is ws_mod._READY_EXECUTOR, (
        f"gateway.ready's resolve_skin() call used executor {calls[0]!r}, "
        f"expected the dedicated ws_mod._READY_EXECUTOR -- the shared-pool "
        f"stall this patch fixes would reappear if this used the default "
        f"executor (None) again"
    )

    ready_frames = [
        line for line in ws.sent if '"gateway.ready"' in line
    ]
    assert ready_frames, "no gateway.ready frame was sent"


def test_ready_executor_has_two_dedicated_worker_threads():
    """Sizing regression: the executor must stay a small, dedicated pool
    (2 workers, ws-ready prefix) -- not accidentally widened/collapsed back
    into sharing threads with unrelated work."""
    assert ws_mod._READY_EXECUTOR._max_workers == 2
    assert ws_mod._READY_EXECUTOR._thread_name_prefix == "ws-ready"
