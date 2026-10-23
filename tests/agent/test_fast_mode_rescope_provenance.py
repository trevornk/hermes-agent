"""Direct-helper regression tests for ``agent.fast_mode.rescope_route_fast_override``.

Covers the behavior contract for re-deriving a statically-pinned fast-mode
override (``speed`` / ``service_tier``) after a route change (provider
fallback or ``/model`` switch mutates ``agent.model``/``provider``/``base_url``
in place without touching a previously-pinned request_overrides value):

1. **Sequence/provenance** — an early-return keyed on "does an override key
   already exist" would mean that once an intermediate unsupported route
   cleared the key, a LATER route that legitimately supports fast mode never
   gets it re-derived. The fix must re-derive fresh on every call.
2. **Provenance conflation** — ANY ``service_tier``/``speed`` key must not be
   treated as "the static fast-mode marker" and unconditionally cleared on
   every rescope call; only a value that IS the synthetic marker may be
   stripped/re-derived. An explicit non-marker value is left untouched, and
   never clobbered by a freshly resolved synthetic value either.
3. **Fail-unsafe exception ordering** — the stale-key strip must be committed
   to ``agent.request_overrides`` BEFORE the resolver call, so a resolver
   exception (network/config error) can never leave a stale,
   route-incompatible key riding on the outbound request even if the caller
   only logs and swallows.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.fast_mode import rescope_route_fast_override


def _agent(**kw):
    base = dict(
        service_tier=None,
        model=None,
        provider=None,
        base_url=None,
        api_mode="chat_completions",
        request_overrides={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── 1. Sequence/provenance: priority route -> unsupported hop -> eligible ──


def test_rescope_reapplies_after_intermediate_unsupported_hop():
    """Anthropic (speed=fast) -> openrouter (unsupported, clears) -> Anthropic
    again (supported) must re-derive speed=fast on the THIRD rescope, not stay
    stuck at {} because the key was already absent."""
    agent = _agent(
        service_tier="priority",
        request_overrides={"speed": "fast"},
        provider="anthropic", model="claude-opus-5", base_url="https://api.anthropic.com",
        api_mode="anthropic_messages",
    )

    # Hop 1: route becomes an unsupported provider -> override cleared.
    agent.provider, agent.model, agent.base_url = "openrouter", "z-ai/glm-4.7", "https://openrouter.ai/api/v1"
    agent.api_mode = "chat_completions"
    rescope_route_fast_override(agent)
    assert agent.request_overrides == {}

    # Hop 2: route becomes eligible again -> must be RE-DERIVED, not left at {}.
    agent.provider, agent.model, agent.base_url = "anthropic", "claude-opus-5", "https://api.anthropic.com"
    agent.api_mode = "anthropic_messages"
    rescope_route_fast_override(agent)
    assert agent.request_overrides == {"speed": "fast"}, (
        f"expected speed=fast re-derived for the eligible route, got {agent.request_overrides!r}"
    )


# ── 2. Provenance: explicit non-marker values must survive rescope ─────────


def test_rescope_preserves_explicit_flex_when_fast_mode_disabled():
    """An explicit service_tier=flex (unrelated to /fast) must survive a
    same-provider rescope when the static /fast toggle is off."""
    agent = _agent(
        service_tier=None,
        request_overrides={"service_tier": "flex"},
        provider="openai", model="gpt-5.5", base_url="https://api.openai.com/v1",
    )
    rescope_route_fast_override(agent)
    assert agent.request_overrides == {"service_tier": "flex"}, (
        f"explicit service_tier=flex must not be cleared by rescope: {agent.request_overrides!r}"
    )


def test_rescope_preserves_explicit_priority_when_fast_mode_disabled():
    """Value equality with a sentinel is NOT provenance. An explicit,
    caller-set request_overrides['service_tier'] == 'priority' must survive
    rescope when the static /fast toggle is OFF (agent.service_tier is None)
    -- the string 'priority' matching the sentinel is a coincidence of value,
    not proof the synthetic /fast marker put it there. Only the static
    preference owner (agent.service_tier == 'priority') may trigger the
    marker strip/re-derive."""
    agent = _agent(
        service_tier=None,
        request_overrides={"service_tier": "priority"},
        provider="openai", model="gpt-5.5", base_url="https://api.openai.com/v1",
        api_mode="chat_completions",
    )
    rescope_route_fast_override(agent)
    assert agent.request_overrides == {"service_tier": "priority"}, (
        f"explicit service_tier=priority (fast mode off) must not be cleared by "
        f"rescope: {agent.request_overrides!r}"
    )


def test_rescope_does_not_clobber_explicit_flex_with_synthetic_priority():
    """Static /fast ON must never overwrite an explicit service_tier value
    with the synthetic 'priority' marker (no accidental paid-tier
    enablement)."""
    agent = _agent(
        service_tier="priority",
        request_overrides={"service_tier": "flex"},
        provider="openai", model="gpt-5.5", base_url="https://api.openai.com/v1",
    )
    with patch(
        "hermes_cli.models.resolve_fast_mode_overrides",
        return_value={"service_tier": "priority"},
    ):
        rescope_route_fast_override(agent)
    assert agent.request_overrides == {"service_tier": "flex"}, (
        f"explicit service_tier=flex must not be overwritten by the synthetic "
        f"priority value: {agent.request_overrides!r}"
    )


# ── 3. Fail-safe exception handling ─────────────────────────────────────────


def test_rescope_strips_stale_key_even_when_resolver_raises():
    """The stale-key strip must be committed to agent.request_overrides
    BEFORE the resolver call, so a resolver exception can never leave a
    route-incompatible key on the agent."""
    agent = _agent(
        service_tier="priority",
        request_overrides={"speed": "fast"},
        provider="openai-codex", model="gpt-6-astra", base_url="https://chatgpt.com/backend-api/codex",
    )
    with patch(
        "hermes_cli.models.resolve_fast_mode_overrides",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError):
            rescope_route_fast_override(agent)

    assert "speed" not in agent.request_overrides, (
        "stale 'speed' key must be stripped before the resolver call, even "
        f"though the resolver raised: {agent.request_overrides!r}"
    )


def test_rescope_no_stale_keys_no_static_fast_is_a_noop():
    """No marker keys present and fast mode off -> untouched, no resolver
    call."""
    agent = _agent(
        service_tier=None,
        request_overrides={"temperature": 0.2},
        provider="openai", model="gpt-5.5", base_url="https://api.openai.com/v1",
    )
    with patch("hermes_cli.models.resolve_fast_mode_overrides") as mock_resolve:
        rescope_route_fast_override(agent)
        mock_resolve.assert_not_called()
    assert agent.request_overrides == {"temperature": 0.2}
