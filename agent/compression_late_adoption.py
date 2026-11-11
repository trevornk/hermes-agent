"""Adopt a compaction that finished after the host stopped waiting (sibling of ``compression_facade``).

carry-marker: compression-late-adoption

What the worker has already done by the time it finishes: ``compress_context`` runs the whole pipeline on the
pooled thread. Once ``fence.begin_commit()`` admits it, it has ALREADY mutated the durable session (in-place
``archive_and_compact`` at the start watermark; rows appended after the watermark survive as tail) and the agent
(system prompt, flush cursors, compressor counters). The only thing left undone is the host's in-memory
transcript, which still holds the pre-compaction messages. "Adoption" is therefore NOT a second commit: it makes
the live list follow the committed state — ``compacted prefix + everything appended since the snapshot``.

Safety rules (each one is a test):

* Only a worker the host detached from at the fits-window cap is tracked, and only for in-place compaction with a
  watermark-fenced commit (``conversation_compression._await_worker_within_budget`` enforces both).
* The commit fence carries an admission check (:meth:`LateCompression.admission_ok`): the worker may commit only
  while the live prefix it compacted is unchanged and the session id is the same. A changed prefix is refused
  BEFORE any durable mutation, so the "discard" case never leaves the database ahead of the transcript.
* A semantic no-op result (same list, or no real shrink by ``compression_made_progress``) is never adopted.
* Adoption happens at most once, inside ``_compress_context`` — the funnel every API-call boundary already uses.
* While a tracked worker is in flight no second worker starts: a fitting request is skipped like a lock-skip, an
  over-window / unknown-size request waits for it (bounded by the idle budget) and adopts the result.
* The prefix check is re-run at adoption time; a mismatch (changed after the commit) discards with a warning.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ATTR = "_late_compression"


def _digest(message: Any) -> str:
    if isinstance(message, dict):
        # "_"-prefixed keys are persistence bookkeeping (_db_persisted, _row_id, ...), not conversation content.
        message = {k: v for k, v in message.items() if not (isinstance(k, str) and k.startswith("_"))}
    return hashlib.blake2b(
        json.dumps(message, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8", "replace"),
        digest_size=16,
    ).hexdigest()


def fingerprint(messages: list) -> list[str]:
    """Per-message content digests (bookkeeping keys excluded); equal lists mean an identical prefix."""
    return [_digest(m) for m in messages]


class LateCompression:
    """One detached compaction attempt: what it compacted, and its parked result once it finishes."""

    def __init__(self, agent: Any, live_messages: list) -> None:
        self.agent = agent
        self.session_id = getattr(agent, "session_id", None)
        self.live_ref = live_messages
        self.source_fp: Optional[list[str]] = None
        self.source_len = 0
        self.source_tokens = 0
        self.state = "running"  # running -> ready | noop | failed
        self.result: Optional[tuple[list, str]] = None
        self.future: Any = None
        self.fence: Any = None
        self._lock = threading.Lock()

    def record_source(self, snapshot: list) -> None:
        """Worker thread, right after the deep snapshot: what this attempt is about to compact."""
        from agent.model_metadata import estimate_messages_tokens_rough

        self.source_len = len(snapshot)
        self.source_tokens = estimate_messages_tokens_rough(snapshot)
        self.source_fp = fingerprint(snapshot)

    def _prefix_unchanged(self, live: list) -> bool:
        return (
            self.source_fp is not None and len(live) >= self.source_len
            and fingerprint(live[: self.source_len]) == self.source_fp
        )

    def admission_ok(self) -> bool:
        """Commit-fence admission check (worker thread, under the fence lock)."""
        ok = getattr(self.agent, "session_id", None) == self.session_id and self._prefix_unchanged(self.live_ref)
        if not ok:
            logger.warning(
                "Late compaction refused at the commit fence: the transcript it compacted changed (session=%s)",
                self.session_id or "none",
            )
        return ok

    def on_done(self, future: Any) -> None:
        """Park the finished attempt's result (idempotent)."""
        with self._lock:
            if self.state != "running":
                return
            try:
                result = future.result()
            except BaseException:
                logger.info("Detached compression worker ended without a result (session=%s)", self.session_id or "none")
                self.state = "failed"
                return
            self.state = self._classify(result)
            self.result = result if self.state == "ready" else None

    def _classify(self, result: Any) -> str:
        from agent.model_metadata import estimate_messages_tokens_rough
        from agent.turn_context import compression_made_progress

        if not (isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], list)):
            return "failed"
        compressed = result[0]
        if compressed is self.live_ref or not compressed:
            return "noop"
        # Unanchored like-for-like estimates (same rough estimator both sides); kept a thin seam so the
        # real-token progress check can replace it.
        if not compression_made_progress(
            self.source_len, len(compressed), self.source_tokens, estimate_messages_tokens_rough(compressed)
        ):
            logger.info("Late compaction made no real progress (session=%s); not adopting", self.session_id or "none")
            return "noop"
        return "ready"


def current(agent: Any) -> Optional[LateCompression]:
    return vars(agent).get(_ATTR)


def register(agent: Any, ticket: LateCompression, future: Any, fence: Any) -> None:
    """The host detached from ``future``: track it so its result can be adopted and no second worker starts."""
    ticket.future, ticket.fence = future, fence
    vars(agent)[_ATTR] = ticket
    future.add_done_callback(ticket.on_done)


def _clear(agent: Any, ticket: LateCompression) -> None:
    if vars(agent).get(_ATTR) is ticket:
        del vars(agent)[_ATTR]


def in_flight(agent: Any) -> Optional[LateCompression]:
    """The tracked attempt if its worker is still able to commit, else ``None``."""
    ticket = current(agent)
    if ticket is None or ticket.state != "running":
        return None
    future = ticket.future
    if future is None or future.done() or getattr(ticket.fence, "is_cancelled", False):
        return None  # finished, or past its deadline / revoked: it can no longer commit
    return ticket


def _wait(ticket: LateCompression, seconds: float) -> None:
    try:
        ticket.future.result(timeout=seconds)
    except concurrent.futures.TimeoutError:
        pass
    except BaseException:
        pass  # a failed worker is classified by on_done


def adopt(agent: Any, messages: list) -> Optional[tuple[list, str]]:
    """``(adopted messages, system prompt)`` for a finished, still-valid attempt, once; else ``None``."""
    ticket = current(agent)
    if ticket is None or ticket.future is None:
        return None
    if ticket.state == "running" and ticket.future.done():
        ticket.on_done(ticket.future)
    if ticket.state == "running":
        return None
    _clear(agent, ticket)
    if ticket.state != "ready" or ticket.result is None:
        logger.info("Late compaction not adopted: %s (session=%s)", ticket.state, ticket.session_id or "none")
        return None
    if getattr(agent, "session_id", None) != ticket.session_id or not ticket._prefix_unchanged(messages):
        logger.warning(
            "Late compaction discarded: the transcript changed since it was compacted (session=%s)",
            ticket.session_id or "none",
        )
        return None
    compressed, prompt = ticket.result
    adopted = [*compressed, *messages[ticket.source_len:]]
    agent._last_compaction_in_place = True
    logger.info(
        "Adopted late compaction: %d -> %d messages (+%d appended since) (session=%s)",
        ticket.source_len, len(compressed), len(messages) - ticket.source_len, ticket.session_id or "none",
    )
    return adopted, prompt


def intercept(
    agent: Any, messages: list, system_message: str, *, approx_tokens: Any, force: bool, partial: bool
) -> Optional[tuple[list, str]]:
    """Called first by ``_compress_context``: a result to return instead of starting a worker, or ``None``.
    ``partial`` (``/compress here N``: ``messages`` is only the head) never adopts."""
    ticket = current(agent)
    if ticket is None:
        return None
    if in_flight(agent) is not None:
        from agent.conversation_compression import (
            _existing_system_prompt, request_exceeds_model_window, resolve_context_compression_timeouts,
        )

        if not force and request_exceeds_model_window(agent, approx_tokens) is not False:
            # Cannot be sent as is: wait for the one attempt instead of starting another.
            _wait(ticket, resolve_context_compression_timeouts()[0])
        if in_flight(agent) is not None:
            logger.info("Skipping compression: a background compaction is still running (session=%s)", ticket.session_id)
            agent._compression_skipped_due_to_lock = "background compaction in flight"
            return messages, _existing_system_prompt(agent, system_message)
    if partial:
        return None
    return adopt(agent, messages)
