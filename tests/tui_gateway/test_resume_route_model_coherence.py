"""Resume must never fuse a model from one route with a provider from another.

``sessions.model`` and ``model_config`` are written by different paths and can diverge.
``_stored_session_runtime_overrides`` took the model from the column and the provider from the
JSON, so a divergent row resumed as a model/endpoint pair that was never configured — observed
in the field as ``gpt-6-astra`` sent to ``https://api.anthropic.com``, 404-ing every turn until
provider fallback re-routed it (#110279).
"""
import json

import pytest

from tui_gateway import server


def _row(model, cfg, **extra):
    row = {"id": "s-mix", "model": model, "model_config": json.dumps(cfg)}
    row.update(extra)
    return row


ANTHROPIC = {"model": "claude-opus-5", "provider": "anthropic",
             "base_url": "https://api.anthropic.com", "api_mode": "anthropic_messages"}
CODEX = {"model": "claude-opus-5", "provider": "openai-codex",
         "base_url": "https://chatgpt.com/backend-api/codex", "api_mode": "codex_responses"}


def test_divergent_model_does_not_inherit_the_other_models_provider():
    """The exact field shape that 404'd: codex-only model, anthropic route persisted."""
    ov = server._stored_session_runtime_overrides(_row("gpt-6-astra", ANTHROPIC))
    assert ov["model_override"]["model"] == "gpt-6-astra"
    # Never pair the new model with the old model's endpoint.
    assert ov["model_override"]["provider"] != "anthropic"
    assert ov["model_override"]["base_url"] != "https://api.anthropic.com"
    assert ov["model_override"]["api_mode"] != "anthropic_messages"


def test_divergent_model_does_not_inherit_a_codex_route_either():
    """Mirror direction: an Anthropic model must not be pinned to the Codex endpoint."""
    ov = server._stored_session_runtime_overrides(_row("claude-opus-5", dict(CODEX, model="gpt-5.6-sol")))
    assert ov["model_override"]["model"] == "claude-opus-5"
    assert ov["model_override"]["provider"] != "openai-codex"
    assert ov["model_override"]["base_url"] != "https://chatgpt.com/backend-api/codex"


def test_billing_provider_supplies_the_route_when_the_stale_one_is_dropped():
    """billing_provider is written by a separate path and corroborated every field-observed row."""
    ov = server._stored_session_runtime_overrides(
        _row("gpt-6-astra", ANTHROPIC, billing_provider="openai-codex"))
    assert ov["model_override"]["model"] == "gpt-6-astra"
    assert ov["model_override"]["provider"] == "openai-codex"
    assert ov.get("provider_override") == "openai-codex"


def test_matching_model_keeps_its_full_route():
    """The non-divergent path must be untouched: same model => restore provider/base_url/api_mode."""
    ov = server._stored_session_runtime_overrides(_row("claude-opus-5", ANTHROPIC))
    assert ov["model_override"] == {
        "model": "claude-opus-5", "provider": "anthropic",
        "base_url": "https://api.anthropic.com", "api_mode": "anthropic_messages"}
    assert ov["provider_override"] == "anthropic"


def test_config_only_model_still_restores_its_route():
    """Empty model column falls back to model_config's model — that is not a divergence."""
    ov = server._stored_session_runtime_overrides(_row("", ANTHROPIC))
    assert ov["model_override"]["model"] == "claude-opus-5"
    assert ov["model_override"]["provider"] == "anthropic"


def test_divergence_preserves_non_route_runtime_fields():
    """Only provider/base_url/api_mode are route fields; reasoning/tier must survive a drop."""
    cfg = dict(ANTHROPIC, reasoning_config={"enabled": True, "effort": "high"}, service_tier="normal")
    ov = server._stored_session_runtime_overrides(_row("gpt-6-astra", cfg))
    assert ov["reasoning_config_override"] == {"enabled": True, "effort": "high"}
    assert ov["service_tier_override"] == ""  # "normal" means an explicit no-priority-tier override


@pytest.mark.parametrize("model", ["council"])
def test_pseudo_models_are_not_treated_as_divergent(model):
    """`/moa` sessions persist model=council with a moa:// route; same model both sides, so keep it."""
    cfg = {"model": model, "provider": "moa", "base_url": "moa://local"}
    ov = server._stored_session_runtime_overrides(_row(model, cfg))
    assert ov["model_override"]["provider"] == "moa"
    assert ov["model_override"]["base_url"] == "moa://local"
