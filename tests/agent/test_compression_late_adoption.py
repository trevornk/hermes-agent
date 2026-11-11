"""A compaction that finishes after the host stopped waiting is adopted, not discarded.

A fitting request stops waiting after ``compression.fits_window_blocking_seconds`` but leaves the worker (and its
commit fence) alive. If the worker later commits, the durable transcript is the compacted one while the live list
is still the full one, so the next API-call boundary (``_compress_context``) swaps the compacted prefix in and
keeps everything appended since. A changed prefix is refused at the commit fence (nothing is persisted), a
no-op result is never adopted, and no second worker starts while one is in flight.
"""

import copy
import threading
import time
from types import SimpleNamespace

import pytest

from agent import compression_late_adoption as late
from agent.conversation_compression import CompressionCommitFence
from agent.model_metadata import estimate_messages_tokens_rough
from agent.turn_context import compression_made_progress

# The fits-window cap only applies once the worker holds its fence; a cold first import/estimate would race it.
estimate_messages_tokens_rough([{"role": "user", "content": "warm"}])
assert callable(compression_made_progress)


def _msgs(n):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(n)]


def _wait_until(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while not pred() and time.monotonic() < end:
        threading.Event().wait(0.01)
    return pred()


class _Env:
    def __init__(self, monkeypatch, *, result):
        from agent.compression_facade import CompressionFacadeMixin

        class _Agent(CompressionFacadeMixin):
            def _conversation_root_id(self):
                return None

        self.agent = _Agent()
        self.agent.context_compressor = SimpleNamespace(context_length=10_000_000)
        self.agent._cached_system_prompt = "sys"
        self.agent._current_turn_id = "turn-1"
        self.agent.session_id = "s"
        self.agent.compression_in_place = True
        self.gate = threading.Event()
        self.calls = []
        self.committed = []
        self.result = result
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"compression": {"fits_window_blocking_seconds": 0.2, "context_timeout_seconds": 60,
                                     "context_total_ceiling_seconds": 120}},
        )
        monkeypatch.setattr("agent.conversation_compression.compress_context", self._compress_context)

    def _compress_context(self, agent, messages, system_message, **kw):
        self.calls.append(list(messages))
        if len(self.calls) > 1:
            return messages, system_message  # any later attempt is a harmless no-op here
        fence = kw["commit_fence"]
        fence.mark_commit_watermark_fenced()
        self.gate.wait(10)
        if not fence.begin_commit():
            return messages, system_message
        try:
            self.committed.append(True)
            compressed = self.result(messages)
            return compressed, "new-prompt"
        finally:
            fence.finish_commit()

    def ticket_done(self):
        t = late.current(self.agent)
        return t is not None and t.state != "running"


def _compacted(messages):
    return [{"role": "user", "content": "SUMMARY"}, *messages[-2:]]


@pytest.fixture
def env(monkeypatch):
    e = _Env(monkeypatch, result=_compacted)
    yield e
    e.gate.set()


def test_late_success_with_unchanged_prefix_is_adopted_once_with_the_appended_tail(env):
    live = _msgs(10)
    assert env.agent._compress_context(live, "sys", approx_tokens=100)[0] is live  # cap hit: sent uncompressed
    live.extend([{"role": "user", "content": "n1"}, {"role": "assistant", "content": "n2"}])
    env.gate.set()
    assert _wait_until(env.ticket_done)

    adopted, prompt = env.agent._compress_context(live, "sys", approx_tokens=100)
    assert [m["content"] for m in adopted] == ["SUMMARY", "m8", "m9", "n1", "n2"]
    assert prompt == "new-prompt"
    assert len(env.calls) == 1  # no second worker was needed
    assert env.committed == [True]
    assert late.current(env.agent) is None

    again, _ = env.agent._compress_context(adopted, "sys", approx_tokens=100)
    assert again is adopted  # exactly once: the next call is an ordinary (here no-op) compression
    assert [m["content"] for m in adopted][:1] == ["SUMMARY"]


def test_changed_prefix_is_refused_at_the_commit_fence_and_nothing_is_adopted(env):
    live = _msgs(10)
    env.agent._compress_context(live, "sys", approx_tokens=100)
    live[3]["content"] = "edited"  # the prefix the worker compacted no longer matches
    env.gate.set()
    assert _wait_until(env.ticket_done)

    assert env.committed == []  # begin_commit refused: no durable mutation
    out, _ = env.agent._compress_context(live, "sys", approx_tokens=100)
    assert out[0]["content"] == "m0"  # live transcript untouched, not replaced by the stale summary
    assert late.current(env.agent) is None


def test_a_different_session_is_never_adopted(env):
    live = _msgs(10)
    env.agent._compress_context(live, "sys", approx_tokens=100)
    env.agent.session_id = "other"
    env.gate.set()
    assert _wait_until(env.ticket_done)
    out, _ = env.agent._compress_context(live, "sys", approx_tokens=100)
    assert out[0]["content"] == "m0"
    assert env.committed == []


@pytest.mark.parametrize("result", [lambda m: m, lambda m: copy.deepcopy(m)], ids=["identity", "equal_copy"])
def test_semantic_no_op_result_is_not_adopted(monkeypatch, result):
    env = _Env(monkeypatch, result=result)
    try:
        live = _msgs(10)
        env.agent._compress_context(live, "sys", approx_tokens=100)
        env.gate.set()
        assert _wait_until(env.ticket_done)
        out, prompt = env.agent._compress_context(live, "sys", approx_tokens=100)
        assert out is live and prompt != "new-prompt"
    finally:
        env.gate.set()


def test_no_second_worker_while_one_is_in_flight(env):
    live = _msgs(10)
    env.agent._compress_context(live, "sys", approx_tokens=100)
    assert late.current(env.agent).state == "running"
    started = time.monotonic()
    out, _ = env.agent._compress_context(live, "sys", approx_tokens=100, trigger="pre_api")
    assert out is live and time.monotonic() - started < 2
    assert len(env.calls) == 1
    from agent.conversation_compression import compression_skipped_due_to_lock
    assert compression_skipped_due_to_lock(env.agent)  # callers refund and proceed, like a lock skip


def test_over_window_request_waits_for_the_in_flight_worker_and_adopts_it(env, monkeypatch):
    live = _msgs(10)
    env.agent._compress_context(live, "sys", approx_tokens=100)
    threading.Timer(0.3, env.gate.set).start()
    out, prompt = env.agent._compress_context(live, "sys", approx_tokens=20_000_000)  # over the window
    assert [m["content"] for m in out] == ["SUMMARY", "m8", "m9"]
    assert prompt == "new-prompt" and len(env.calls) == 1


def test_a_dead_in_flight_ticket_does_not_block_new_attempts(env):
    live = _msgs(10)
    env.agent._compress_context(live, "sys", approx_tokens=100)
    ticket = late.current(env.agent)
    ticket.fence.revoke_commit_admission()  # worker can no longer commit: the ticket is stale
    assert late.in_flight(env.agent) is None


def test_fence_admission_check_refuses_the_commit():
    fence = CompressionCommitFence()
    fence.set_admission_check(lambda: False)
    assert fence.begin_commit() is False and fence.is_cancelled
    ok = CompressionCommitFence()
    ok.set_admission_check(lambda: True)
    assert ok.begin_commit() is True
    ok.finish_commit()
    boom = CompressionCommitFence()
    boom.set_admission_check(lambda: 1 / 0)
    assert boom.begin_commit() is False  # a failing check fails closed


def test_fingerprint_ignores_private_bookkeeping_keys_but_not_content():
    a = _msgs(3)
    b = copy.deepcopy(a)
    b[1]["_db_persisted"] = True
    b[1]["_row_id"] = 7
    assert late.fingerprint(a) == late.fingerprint(b)
    b[1]["content"] = "changed"
    assert late.fingerprint(a) != late.fingerprint(b)
