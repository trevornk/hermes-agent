"""Regression tests: switch_model() must re-derive the static fast-mode
override (speed / service_tier) for the switched-to route.

Sibling of the provider-fallback leak (test_provider_fallback.py::
TestFastModeParamsDoNotLeakAcrossFallback): switch_model() is the OTHER path
that mutates agent.model/provider/base_url in place without touching a
statically-pinned agent.request_overrides["speed"|"service_tier"]. A user on
static /fast (agent.service_tier == "priority") who runs `/model` onto a
provider that rejects the stale param hits a hard failure — e.g. an
Anthropic-only ``speed`` param or an OpenAI-only ``service_tier`` riding onto
openai-codex, whose Responses API 400s on any unlisted top-level key (see
agent.codex_responses_adapter._preflight_codex_api_kwargs).
"""

from unittest.mock import MagicMock, patch

from agent.context_compressor import ContextCompressor
from run_agent import AIAgent


def _make_agent(*, provider, base_url, model, service_tier, request_overrides) -> AIAgent:
    """Minimal AIAgent with a context_compressor, skipping __init__ (mirrors
    tests/agent/test_switch_model_reapplies_headers.py's bare-agent pattern)."""
    agent = AIAgent.__new__(AIAgent)

    agent.model = model
    agent.provider = provider
    agent.base_url = base_url
    agent.api_key = "sk-primary"
    agent.api_mode = "chat_completions"
    agent.service_tier = service_tier
    agent.request_overrides = dict(request_overrides)
    agent.client = MagicMock()
    agent.quiet_mode = True
    agent._config_context_length = None
    agent._client_kwargs = {"api_key": "sk-primary", "base_url": base_url}

    compressor = ContextCompressor(
        model=agent.model, threshold_percent=0.50, base_url=base_url, api_key="sk-primary",
        provider=provider, quiet_mode=True, config_context_length=None,
    )
    agent.context_compressor = compressor
    agent._primary_runtime = {}

    return agent


@patch("agent.model_metadata.get_model_context_length", return_value=131_072)
def test_switch_from_anthropic_speed_to_codex_drops_speed(mock_ctx_len):
    """/model switch from Anthropic (static speed=fast) to openai-codex must
    drop the Anthropic-only 'speed' key — Codex's Responses API rejects it."""
    agent = _make_agent(
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-opus-5",
        service_tier="priority", request_overrides={"speed": "fast"},
    )

    agent.switch_model(
        "gpt-6-astra", "openai-codex",
        api_key="sk-codex", base_url="https://chatgpt.com/backend-api/codex",
    )

    assert "speed" not in agent.request_overrides, (
        "Anthropic-only 'speed' override leaked into request_overrides after "
        f"/model switch to a Codex provider: {agent.request_overrides!r}"
    )


@patch("agent.model_metadata.get_model_context_length", return_value=131_072)
def test_switch_from_openai_service_tier_to_unsupported_route_drops_it(mock_ctx_len):
    """/model switch from OpenAI (static service_tier=priority) to a route that
    doesn't support Priority Processing must drop the stale key."""
    agent = _make_agent(
        provider="openai", base_url="https://api.openai.com/v1", model="gpt-5.5",
        service_tier="priority", request_overrides={"service_tier": "priority"},
    )

    agent.switch_model(
        "z-ai/glm-4.7", "openrouter",
        api_key="sk-or", base_url="https://openrouter.ai/api/v1",
    )

    assert "service_tier" not in agent.request_overrides, (
        "OpenAI-only 'service_tier' override leaked into request_overrides after "
        f"/model switch to a route that doesn't support it: {agent.request_overrides!r}"
    )


@patch("agent.model_metadata.get_model_context_length", return_value=131_072)
def test_switch_between_openai_fast_routes_keeps_service_tier(mock_ctx_len):
    """A switch to ANOTHER route that legitimately supports Priority Processing
    (openai -> openai-codex) must keep service_tier=priority, not just survive
    but be correctly re-derived for the destination."""
    agent = _make_agent(
        provider="openai", base_url="https://api.openai.com/v1", model="gpt-5.5",
        service_tier="priority", request_overrides={"service_tier": "priority"},
    )

    agent.switch_model(
        "gpt-6-astra", "openai-codex",
        api_key="sk-codex", base_url="https://chatgpt.com/backend-api/codex",
    )

    assert agent.request_overrides.get("service_tier") == "priority"


@patch("agent.model_metadata.get_model_context_length", return_value=131_072)
def test_switch_with_fast_mode_off_is_unaffected(mock_ctx_len):
    """No static fast override present -> switch_model must not fabricate one."""
    agent = _make_agent(
        provider="openai", base_url="https://api.openai.com/v1", model="gpt-5.5",
        service_tier=None, request_overrides={},
    )

    agent.switch_model(
        "gpt-6-astra", "openai-codex",
        api_key="sk-codex", base_url="https://chatgpt.com/backend-api/codex",
    )

    assert "service_tier" not in agent.request_overrides
    assert "speed" not in agent.request_overrides


@patch("agent.model_metadata.get_model_context_length", return_value=131_072)
def test_switch_then_restore_primary_does_not_resurrect_stale_fast_param(mock_ctx_len):
    """switch_model()'s _primary_runtime snapshot must capture the RESCOPED
    request_overrides, not the pre-switch value — otherwise a later fallback
    activation followed by restore_primary_runtime() resurrects the stale
    provider-specific fast param via the snapshot (the same bug class as the
    extra_body salvage; speed/service_tier need the same ordering).
    """
    agent = _make_agent(
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-opus-5",
        service_tier="priority", request_overrides={"speed": "fast"},
    )

    agent.switch_model(
        "gpt-6-astra", "openai-codex",
        api_key="sk-codex", base_url="https://chatgpt.com/backend-api/codex",
    )

    assert "speed" not in agent._primary_runtime.get("request_overrides", {}), (
        "switch_model's _primary_runtime snapshot captured the stale pre-rescope "
        f"request_overrides: {agent._primary_runtime.get('request_overrides')!r}"
    )
