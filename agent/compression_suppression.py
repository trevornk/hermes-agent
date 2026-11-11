"""Host-side, engine-agnostic suppression of automatic blocking compression.

carry-marker: compression-suppression-primitive

Why this exists: the engine cooldown (``record_timeout_failure`` / ``_consecutive_timeout_failures``) is an
engine capability, and the LCM engine has neither, so a timed-out compaction never armed anything and every API
call of the turn re-blocked on a fresh one. This module is the ONE place the host remembers "do not start another
blocking compression right now", keyed by a named *arming reason* so independent breakers can share it:

* ``"fits_window_timeout"`` (turn-scoped): a blocking compaction of a request that fits the model window hit the
  host wait cap. Armed by ``compression_facade``; lapses by itself when the turn id changes.
* ``"no_real_progress"`` (reserved for the real-token no-progress circuit breaker): arm it with
  ``arm(agent, "no_real_progress", scope="session")`` and clear it with ``clear(agent, "no_real_progress")``.

Contract (enforced in :func:`suppresses_request`, the only query the compression funnel uses):

* A suppressed call returns the transcript unchanged (a transient defer: callers refund the attempt and send).
* Suppression NEVER applies to a request above the model window or of unknown size/window (it cannot be sent
  uncompressed), to manual ``/compress`` (``force=True``), or to provider-proven overflow recovery
  (``bypass_cooldown=True``). New reasons inherit these exemptions.
* ``scope="turn"`` needs ``agent._current_turn_id`` (without one, arming is a no-op); entries are tied to ``agent._current_turn_id`` at arm time and are ignored (and dropped) once
  the turn id differs; ``scope="session"`` entries last until :func:`clear`.
* Arming logs at WARNING and every skip at INFO, so a silent "why did compression stop" is always greppable.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

REASON_FITS_WINDOW_TIMEOUT = "fits_window_timeout"
REASON_NO_REAL_PROGRESS = "no_real_progress"

_STATE_ATTR = "_compression_suppression"
_lock = threading.Lock()


def _turn_id(agent: Any) -> str:
    return str(getattr(agent, "_current_turn_id", "") or "")


def _state(agent: Any) -> dict[str, tuple[str, str]]:
    return vars(agent).setdefault(_STATE_ATTR, {})


def arm(agent: Any, reason: str, *, scope: str = "turn") -> None:
    """Suppress automatic blocking compression for ``reason`` (``scope``: ``"turn"`` or ``"session"``)."""
    if scope not in ("turn", "session"):
        raise ValueError(f"unknown suppression scope: {scope!r}")
    if scope == "turn" and not _turn_id(agent):
        # No turn context (out-of-turn caller): "the rest of this turn" is undefined and would never lapse.
        logger.debug("Not arming turn-scoped compression suppression %r: no current turn id", reason)
        return
    with _lock:
        _state(agent)[reason] = (scope, _turn_id(agent))
    logger.warning(
        "Automatic blocking compression suppressed (reason=%s, scope=%s, session=%s)",
        reason, scope, getattr(agent, "session_id", None) or "none",
    )


def is_suppressed(agent: Any) -> Optional[str]:
    """The armed reason (turn-scoped entries from an earlier turn are dropped), else ``None``."""
    state = vars(agent).get(_STATE_ATTR)
    if not state:
        return None
    current = _turn_id(agent)
    with _lock:
        for reason, (scope, armed_turn) in list(state.items()):
            if scope == "turn" and armed_turn != current:
                del state[reason]
        return next(iter(state), None)


def clear(agent: Any, reason: Optional[str] = None) -> None:
    """Lift one reason, or every reason when ``reason`` is ``None``."""
    with _lock:
        state = vars(agent).get(_STATE_ATTR)
        if state:
            state.clear() if reason is None else state.pop(reason, None)


def suppresses_request(
    agent: Any, request_tokens: Any, *, force: bool = False, bypass_cooldown: bool = False
) -> Optional[str]:
    """The reason this automatic compression request must be skipped, else ``None`` (see the module contract)."""
    if force or bypass_cooldown:
        return None
    reason = is_suppressed(agent)
    if reason is None:
        return None
    from agent.conversation_compression import request_exceeds_model_window

    if request_exceeds_model_window(agent, request_tokens) is not False:
        return None
    logger.info(
        "Skipping blocking compression: suppressed (%s) and the request (~%s tokens) fits the model window "
        "(session=%s)", reason, request_tokens, getattr(agent, "session_id", None) or "none",
    )
    return reason
