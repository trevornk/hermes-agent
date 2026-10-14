"""Tests for tui_gateway.loop_noise — the WS peer-hangup teardown filter (#50005)."""

from __future__ import annotations

import asyncio

import pytest

from tui_gateway.loop_noise import (
    _is_benign_teardown,
    install_loop_noise_filter,
)


class _FakeConnectionLostCallback:
    """Stand-in whose repr matches asyncio's ``_call_connection_lost`` flood."""

    def __repr__(self) -> str:
        return "<Handle _ProactorBasePipeTransport._call_connection_lost(None)>"


def test_benign_teardown_matches_reset_in_connection_lost():
    ctx = {
        "exception": ConnectionResetError(10054, "forcibly closed"),
        "handle": _FakeConnectionLostCallback(),
    }
    assert _is_benign_teardown(ctx) is True


def test_benign_teardown_matches_aborted_and_broken_pipe():
    for exc in (
        ConnectionAbortedError(10053, "aborted"),
        BrokenPipeError("epipe"),
    ):
        ctx = {"exception": exc, "callback": _FakeConnectionLostCallback()}
        assert _is_benign_teardown(ctx) is True


def test_reset_outside_connection_lost_is_not_suppressed():
    # Same error type, but NOT from the connection-lost teardown path — must
    # fall through to the default handler.
    ctx = {
        "exception": ConnectionResetError("reset in a real handler"),
        "handle": "<Handle some_other_handler()>",
    }
    assert _is_benign_teardown(ctx) is False


def test_unrelated_exception_is_not_suppressed():
    ctx = {
        "exception": ValueError("boom"),
        "handle": _FakeConnectionLostCallback(),
    }
    assert _is_benign_teardown(ctx) is False


def test_no_exception_is_not_suppressed():
    assert _is_benign_teardown({"message": "loop warning, no exc"}) is False


def _shielded_ctx(exc: BaseException) -> dict:
    # Shape of the context asyncio's ``_log_on_exception`` hands the loop handler on
    # Python 3.14.0-3.14.8 (python/cpython#156321).
    return {"message": f"{type(exc).__name__} exception in shielded future", "exception": exc}


def _keepalive_timeout():
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close, CloseCode

    return ConnectionClosedError(None, Close(CloseCode.INTERNAL_ERROR, "keepalive ping timeout"), None)


def test_closed_websocket_in_shielded_future_is_benign():
    from websockets.exceptions import ConnectionClosedOK

    assert _is_benign_teardown(_shielded_ctx(_keepalive_timeout())) is True
    assert _is_benign_teardown(_shielded_ctx(ConnectionClosedOK(None, None, None))) is True


def test_other_errors_in_shielded_future_are_not_suppressed():
    assert _is_benign_teardown(_shielded_ctx(RuntimeError("real bug"))) is False
    # A peer-hangup OS error is only benign inside ``_call_connection_lost``, not in a shield.
    assert _is_benign_teardown(_shielded_ctx(ConnectionResetError("reset"))) is False


def test_closed_websocket_outside_shield_is_not_suppressed():
    ctx = {"message": "Task exception was never retrieved", "exception": _keepalive_timeout()}
    assert _is_benign_teardown(ctx) is False


def _shield_then_cancel_then_fail(loop: asyncio.AbstractEventLoop, exc: BaseException) -> None:
    """The websockets-legacy keepalive shape: the waiter is shielded, the awaiting side is
    cancelled by a timeout, and the connection teardown fails the inner future afterwards."""

    async def scenario() -> None:
        inner = loop.create_future()
        outer = asyncio.shield(inner)
        outer.cancel()
        await asyncio.sleep(0)
        inner.set_exception(exc)
        await asyncio.sleep(0)  # let the done-callbacks run
        inner.exception()  # the owner retrieves it, as websockets/uvicorn do

    loop.run_until_complete(scenario())


def test_real_shield_does_not_forward_closed_websocket():
    # End-to-end through real ``asyncio.shield()``: on 3.14.0-3.14.8 it reports the failure to
    # the loop handler even though the owner retrieves it; elsewhere nothing is reported and
    # this passes trivially. Either way a closed websocket must not reach the previous handler.
    loop = asyncio.new_event_loop()
    try:
        forwarded: list[dict] = []
        loop.set_exception_handler(lambda _loop, ctx: forwarded.append(ctx))
        install_loop_noise_filter(loop)

        _shield_then_cancel_then_fail(loop, _keepalive_timeout())

        assert forwarded == []
    finally:
        loop.close()


@pytest.mark.skipif(
    not hasattr(asyncio.tasks, "_log_on_exception"),
    reason="needs the eager shield() reporting (CPython 3.14.0-3.14.8, gh-156321)",
)
def test_real_shield_still_forwards_unrelated_errors():
    loop = asyncio.new_event_loop()
    try:
        forwarded: list[dict] = []
        loop.set_exception_handler(lambda _loop, ctx: forwarded.append(ctx))
        install_loop_noise_filter(loop)

        _shield_then_cancel_then_fail(loop, RuntimeError("genuine bug in a shielded future"))

        assert [str(c["exception"]) for c in forwarded] == ["genuine bug in a shielded future"]
        assert forwarded[0]["message"] == "RuntimeError exception in shielded future"
    finally:
        loop.close()


def test_install_suppresses_flood_and_forwards_real_errors():
    loop = asyncio.new_event_loop()
    try:
        forwarded: list[dict] = []
        loop.set_exception_handler(lambda _loop, ctx: forwarded.append(ctx))

        install_loop_noise_filter(loop)

        # Benign teardown flood → swallowed, not forwarded.
        loop.call_exception_handler(
            {
                "exception": ConnectionResetError(10054, "forcibly closed"),
                "handle": _FakeConnectionLostCallback(),
            }
        )
        assert forwarded == []

        # Genuine loop error → forwarded to the previous handler unchanged.
        real_ctx = {"exception": RuntimeError("genuine loop bug")}
        loop.call_exception_handler(real_ctx)
        assert len(forwarded) == 1
        assert forwarded[0] is real_ctx
    finally:
        loop.close()




