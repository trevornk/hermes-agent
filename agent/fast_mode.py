"""Bounded fast-mode windows (``/fast auto`` and ``/fast cold``).

``agent.service_tier``: ``None`` (normal), ``"priority"`` / ``"ultrafast"`` (static tiers,
pinned into ``agent.request_overrides`` at build time), ``"auto"`` (every user turn opens a
window of ``agent.fast_auto_seconds``) or ``"cold"`` (only a session's first turn,
no prior history, opens it). The provider's fast override is layered onto request
kwargs only while the window is open; only per-request params (``service_tier`` /
``speed``) vary, so the request body stays byte-identical. Anthropic keeps a separate
prompt cache per speed, so each Anthropic window boundary re-writes the prefix at the
new speed.
"""

from __future__ import annotations

import time
from typing import Any

BOUNDED_MODES = frozenset({"auto", "cold"})
DEFAULT_WINDOW_SECONDS = 60
# Documented fast-mode rate-limit headers; a limit of 0 means the organization has no fast
# capacity for the model (https://platform.claude.com/docs/en/build-with-claude/fast-mode).
_FAST_LIMIT_HEADERS = ("anthropic-fast-input-tokens-limit", "anthropic-fast-output-tokens-limit")
#: Tiers sent on every request of the session (OpenAI ``service_tier`` values; ``priority`` also
#: selects Anthropic/xAI fast mode). Ultrafast is OpenAI-only and gated per model.
STATIC_TIERS = frozenset({"priority", "ultrafast"})
# Codex app-server names for wire tiers it accepts (turn/start.serviceTier); a tier missing here is not sent.
CODEX_TIER_WORDS: dict[str, str] = {"priority": "fast"}
NORMAL_TIER_WORDS = frozenset({"", "normal", "default", "standard", "off", "none"})
# User/config word -> agent.service_tier. The single table every surface (config loaders, /fast
# on CLI / gateway / TUI) parses through, so a new tier is one edit.
SERVICE_TIER_WORDS: dict[str, str] = {
    "fast": "priority", "priority": "priority", "on": "priority",
    "ultrafast": "ultrafast", "ultra": "ultrafast", "auto": "auto", "cold": "cold",
}
# Typed /fast shorthands -> canonical /fast word (pickers always send the canonical value).
FAST_ARG_ALIASES: dict[str, str] = {"ultra": "ultrafast"}


def parse_service_tier(raw: Any) -> str | None:
    """``agent.service_tier`` for a user/config word; None for normal and for unknown words."""
    value = str(raw or "").strip().lower()
    return None if value in NORMAL_TIER_WORDS else SERVICE_TIER_WORDS.get(value)


def parse_exact_service_tier(raw: Any) -> str:
    """Strict :func:`parse_service_tier` for an explicit client pick: ``""`` pins normal, an unknown
    word raises ``ValueError`` instead of silently reading as normal."""
    value = str(raw or "").strip().lower()
    if value not in NORMAL_TIER_WORDS and value not in SERVICE_TIER_WORDS:
        raise ValueError(f"unknown service tier: {value}")
    return parse_service_tier(value) or ""


def service_tier_word(tier: Any) -> str:
    """The user-facing word for a stored tier (``priority`` -> ``fast``, None/"" -> ``normal``)."""
    return {"priority": "fast", None: "normal", "": "normal"}.get(tier, tier)


def begin_turn(agent: Any, conversation_history: Any) -> None:
    """Open (or refuse) the fast window at a user-turn boundary."""
    mode = getattr(agent, "service_tier", None)
    agent._fast_until = 0.0
    if mode not in BOUNDED_MODES:
        return
    if mode == "cold" and any(
        isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")
        for m in (conversation_history or ())
    ):
        return
    try:
        window = float(getattr(agent, "fast_auto_seconds", DEFAULT_WINDOW_SECONDS))
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW_SECONDS
    agent._fast_until = time.monotonic() + max(window, 0.0)


def effective_request_overrides(agent: Any) -> dict[str, Any]:
    """``agent.request_overrides`` plus the fast override while the window is open, minus
    ``speed`` for a model this session learned has no fast capacity."""
    overrides = dict(getattr(agent, "request_overrides", None) or {})
    if getattr(agent, "service_tier", None) in BOUNDED_MODES and time.monotonic() < getattr(agent, "_fast_until", 0.0):
        from hermes_cli.models import resolve_fast_mode_overrides
        base_url = getattr(agent, "base_url", None)
        if getattr(agent, "api_mode", None) == "anthropic_messages":
            base_url = getattr(agent, "_anthropic_base_url", None) or base_url
        overrides.update(
            resolve_fast_mode_overrides(getattr(agent, "model", None), provider=getattr(agent, "provider", None), base_url=base_url) or {}
        )
    if "speed" in overrides and getattr(agent, "model", None) in (getattr(agent, "_fast_mode_unavailable_models", None) or ()):
        overrides.pop("speed", None)
    from agent.codex_service_tiers import negotiate_request_tier
    negotiate_request_tier(agent, overrides)
    return overrides


def fast_mode_unprovisioned(api_error: Any, api_kwargs: Any) -> bool:
    """True for a 429 on a ``speed: "fast"`` request whose fast-mode limit header is 0. The
    organization has no fast capacity for the model, so waiting or rotating keys cannot help."""
    if getattr(api_error, "status_code", None) != 429 or not isinstance(api_kwargs, dict):
        return False
    if (api_kwargs.get("extra_body") or {}).get("speed") != "fast":
        return False
    headers = getattr(getattr(api_error, "response", None), "headers", None)
    if headers is None:
        return False
    return any(str(headers.get(name, "")).strip() == "0" for name in _FAST_LIMIT_HEADERS)


def mark_fast_mode_unavailable(agent: Any) -> bool:
    """Stop sending ``speed`` for the current model for the rest of the session. False when the
    model was already marked, so the caller retries at most once per model."""
    model = getattr(agent, "model", None)
    unavailable = getattr(agent, "_fast_mode_unavailable_models", None)
    if not isinstance(unavailable, set):
        unavailable = agent._fast_mode_unavailable_models = set()
    if not model or model in unavailable:
        return False
    unavailable.add(model)
    return True


#: The exact synthetic values ``resolve_fast_mode_overrides`` produces for the
#: static ``/fast`` toggle. A key present with a DIFFERENT value (e.g. an
#: explicit ``service_tier: "flex"``) is a genuine user setting, not the /fast
#: marker, and must never be treated as stale or clobbered by re-derivation.
_FAST_MODE_SENTINELS: dict[str, Any] = {"speed": "fast", "service_tier": "priority"}


def rescope_route_fast_override(agent: Any) -> None:
    """Re-derive the STATIC fast-mode override baked into ``agent.request_overrides``
    (``agent.service_tier == "priority"``: Anthropic Fast Mode's ``speed`` or
    OpenAI/xAI Priority Processing's ``service_tier``) for the route ``agent`` now
    points at.

    A static override is pinned into ``request_overrides`` ONCE, at agent-init /
    turn-route-resolution time, for the route active THEN. A subsequent provider
    fallback or in-place ``/model`` switch changes ``agent.model``/``provider``/
    ``base_url`` but never touches this pinned value — so a provider-specific
    param (Anthropic-only ``speed``, OpenAI/xAI-only ``service_tier``) rides
    along onto a route that never asked for it and, worse, may hard-reject the
    unknown field (the Codex Responses transport 400s on any key outside its
    allow-list — see ``agent.codex_responses_adapter._preflight_codex_api_kwargs``).
    Bounded ``auto``/``cold`` windows are unaffected: ``effective_request_overrides()``
    re-derives them from the CURRENT route on every request already.

    Call this once a swap/switch has finished mutating ``agent.model``/
    ``provider``/``base_url``/``api_mode``. Only a key whose CURRENT value equals
    the synthetic /fast marker (see ``_FAST_MODE_SENTINELS``) is ever touched — an
    explicit, unrelated value under the same key (e.g. a genuine
    ``service_tier: "flex"``) is left alone in both directions: never dropped as
    "stale" and never overwritten by a freshly re-derived synthetic value. The
    strip is committed to ``agent.request_overrides`` BEFORE the resolver runs, so
    a resolver exception can never leave a stale, route-incompatible marker key on
    the agent even if the caller only logs and swallows it. A fresh marker is
    re-derived whenever the static toggle is on, independent of whether a stale
    marker was present — a route that legitimately supports fast mode gets it
    re-enabled even after an intermediate unsupported hop already cleared it.
    """
    # Value equality with a sentinel is NOT provenance: an explicit caller-set
    # request_overrides["service_tier"] == "priority" happens to collide with the
    # synthetic /fast marker's value. Only the static preference OWNER
    # (agent.service_tier == "priority") may strip/re-derive these keys; when the
    # static toggle is off, an explicit override under the same key is a genuine
    # user setting and must be left untouched in both directions.
    if getattr(agent, "service_tier", None) != "priority":
        return

    overrides = dict(getattr(agent, "request_overrides", None) or {})
    for key, sentinel in _FAST_MODE_SENTINELS.items():
        if overrides.get(key) == sentinel:
            overrides.pop(key)
    # Commit the strip now — before any resolver call that may raise — so a
    # stale, route-incompatible marker can never survive an exception below.
    agent.request_overrides = overrides

    from hermes_cli.models import resolve_fast_mode_overrides
    base_url = getattr(agent, "base_url", None)
    if getattr(agent, "api_mode", None) == "anthropic_messages":
        base_url = getattr(agent, "_anthropic_base_url", None) or base_url
    fresh = resolve_fast_mode_overrides(
        getattr(agent, "model", None), provider=getattr(agent, "provider", None), base_url=base_url,
    ) or {}
    if not fresh:
        return
    changed = False
    for key, value in fresh.items():
        if key in overrides:
            continue  # explicit non-marker value present (markers already stripped above) — don't clobber
        overrides[key] = value
        changed = True
    if changed:
        agent.request_overrides = overrides
