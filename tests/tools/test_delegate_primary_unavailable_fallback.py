"""A pinned ``delegation.provider`` whose credentials are unavailable (credential pool cooled down for the model, quota
exhausted, login expired) starts the child on ``delegation.fallback_providers`` instead of refusing the whole call.

Real ``delegate_task``, real route/credential resolution; only the runtime provider resolver (the credential layer)
and the child's construction/run are stubbed. Misconfiguration and routes without a chain must behave as before.
"""

import json
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.auth_constants import AuthError

SONNET = "claude-sonnet-5-5"
COOLDOWN = AuthError(f"Anthropic credentials are rate-limited for {SONNET}; "
                     "other Claude models remain available (see `hermes auth list`).")
OVERFLOW = {"provider": "anthropic-overflow", "model": SONNET}
CODEX = {"provider": "openai-codex", "model": "gpt-6-astra"}
PRIMARY_CFG = {"provider": "anthropic", "model": SONNET}


def _runtime(provider, base_url, api_mode, key, *, requested=None):
    return {"provider": provider, "requested_provider": requested or provider, "base_url": base_url,
            "api_key": key, "api_mode": api_mode, "request_overrides": {}, "model": None}


RUNTIMES = {
    "anthropic-overflow": _runtime("custom", "https://api.anthropic.com", "anthropic_messages", "overflow-key",
                                   requested="anthropic-overflow"),
    "openai-codex": _runtime("openai-codex", "https://chatgpt.com/backend-api/codex", "codex_responses", "codex-key"),
    "anthropic": _runtime("anthropic", "https://api.anthropic.com", "anthropic_messages", "max-key"),
}


def _parent():
    parent = MagicMock()
    parent.base_url, parent.api_key, parent.provider = "https://openrouter.ai/api/v1", "parent-key", "openrouter"
    parent.api_mode, parent.model, parent.platform = "chat_completions", "anthropic/claude-sonnet-4", "cli"
    parent.request_overrides = {}
    parent.reasoning_config = {"enabled": True, "effort": "medium"}
    parent.providers_allowed = parent.providers_ignored = parent.providers_order = parent.provider_sort = None
    parent._session_db, parent._delegate_depth, parent._active_children = None, 0, []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = parent.tool_progress_callback = parent.thinking_callback = None
    parent._fallback_chain = [{"provider": "openrouter", "model": "parent-chain-model"}]
    return parent


def _spawn(cfg, outcomes):
    """``(tool result, [AIAgent kwargs], [providers the resolver was asked for])`` for one task.

    ``outcomes`` maps a requested provider to an exception (raised) or a runtime dict; anything unlisted resolves
    from RUNTIMES.
    """
    asked = []

    def resolver(requested=None, target_model=None, **_):
        asked.append(requested)
        outcome = outcomes.get(requested, RUNTIMES.get(requested))
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise AuthError(f"Unknown provider '{requested}'.", code="invalid_provider")
        return dict(outcome)

    with (
        patch("tools.delegate_tool._load_config", return_value=cfg),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolver),
        patch("run_agent.AIAgent", side_effect=lambda **kw: MagicMock(provider=kw.get("provider"),
                                                                     model=kw.get("model"))) as agent,
        patch("tools.delegate_tool._run_batch", return_value=json.dumps({"status": "built"})),
    ):
        from tools.delegate_tool import delegate_task
        result = json.loads(delegate_task(goal="do the thing", parent_agent=_parent()))
    return result, [call.kwargs for call in agent.call_args_list], asked


def test_cooled_down_primary_starts_the_child_on_the_first_fallback_entry(caplog):
    cfg = {**PRIMARY_CFG, "fallback_providers": [OVERFLOW, CODEX]}
    with caplog.at_level(logging.INFO, logger="tools.delegate_tool"):
        result, (child,), asked = _spawn(cfg, {"anthropic": COOLDOWN})
    assert result == {"status": "built"}
    assert asked == ["anthropic", "anthropic-overflow"]
    assert (child["provider"], child["model"]) == ("anthropic-overflow", SONNET)
    assert (child["base_url"], child["api_key"]) == ("https://api.anthropic.com", "overflow-key")
    assert child["api_mode"] == "anthropic_messages"
    # The entries after the one in use stay the child's own chain; the parent's chain is never borrowed.
    assert child["fallback_model"] == [CODEX]
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("anthropic-overflow/" + SONNET in m and "fallback_providers[0]" in m for m in logged)


def test_unresolvable_first_entry_is_skipped_for_the_second():
    cfg = {**PRIMARY_CFG, "fallback_providers": [OVERFLOW, CODEX]}
    outcomes = {"anthropic": COOLDOWN, "anthropic-overflow": AuthError("no key", code="missing_api_key")}
    result, (child,), asked = _spawn(cfg, outcomes)
    assert result == {"status": "built"}
    assert asked == ["anthropic", "anthropic-overflow", "openai-codex"]
    assert (child["provider"], child["model"]) == ("openai-codex", "gpt-6-astra")
    assert child["fallback_model"] is None  # nothing left after the last entry, and the parent chain is not borrowed


def test_every_entry_unresolvable_surfaces_the_primary_error():
    cfg = {**PRIMARY_CFG, "fallback_providers": [OVERFLOW, CODEX]}
    broken = AuthError("no key", code="missing_api_key")
    result, children, _ = _spawn(cfg, {"anthropic": COOLDOWN, "anthropic-overflow": broken, "openai-codex": broken})
    assert children == []
    assert "Cannot resolve delegation provider 'anthropic'" in result["error"]
    assert f"rate-limited for {SONNET}" in result["error"]


@pytest.mark.parametrize("chain", [None, []], ids=["no chain", "explicit empty chain"])
def test_no_chain_keeps_the_existing_refusal(chain):
    cfg = dict(PRIMARY_CFG) if chain is None else {**PRIMARY_CFG, "fallback_providers": chain}
    result, children, asked = _spawn(cfg, {"anthropic": COOLDOWN})
    assert children == [] and asked == ["anthropic"]
    assert result["error"].startswith("Cannot resolve delegation provider 'anthropic': "
                                      f"Anthropic credentials are rate-limited for {SONNET}")


def test_healthy_primary_is_used_and_keeps_its_chain():
    cfg = {**PRIMARY_CFG, "fallback_providers": [OVERFLOW, CODEX]}
    _, (child,), asked = _spawn(cfg, {})
    assert asked == ["anthropic"]
    assert (child["provider"], child["api_key"]) == ("anthropic", "max-key")
    assert child["fallback_model"] == [OVERFLOW, CODEX]


@pytest.mark.parametrize("error", [
    AuthError("Unknown provider 'anthropc'.", code="invalid_provider"),
    ValueError("bad delegation config"),
    RuntimeError("boom"),
], ids=["unknown provider", "value error", "unexpected error"])
def test_non_credential_errors_still_surface_even_with_a_chain(error):
    cfg = {**PRIMARY_CFG, "fallback_providers": [OVERFLOW, CODEX]}
    result, children, asked = _spawn(cfg, {"anthropic": error})
    assert children == [] and asked == ["anthropic"]  # no fallback entry was even tried
    assert result["error"].startswith("Cannot resolve delegation provider 'anthropic'")


def test_primary_without_an_api_key_falls_back_too():
    cfg = {**PRIMARY_CFG, "fallback_providers": [CODEX]}
    keyless = {**RUNTIMES["anthropic"], "api_key": ""}
    _, (child,), _ = _spawn(cfg, {"anthropic": keyless})
    assert (child["provider"], child["model"]) == ("openai-codex", "gpt-6-astra")


def test_endpoint_entry_without_its_own_key_is_skipped_not_given_the_parent_key():
    keyless = {"provider": "acme", "model": "acme-1", "base_url": "https://llm.acme.example/v1"}
    keyed = {"provider": "acme", "model": "acme-2", "base_url": "https://llm.acme.example/v1", "api_key": "acme-key"}
    cfg = {**PRIMARY_CFG, "fallback_providers": [keyless, keyed]}
    _, (child,), _ = _spawn(cfg, {"anthropic": COOLDOWN})
    assert (child["model"], child["base_url"], child["api_key"]) == (
        "acme-2", "https://llm.acme.example/v1", "acme-key")


def test_primary_endpoint_tuning_does_not_follow_the_child_to_the_fallback():
    cfg = {**PRIMARY_CFG, "fallback_providers": [OVERFLOW],
           "request_overrides": {"extra_headers": {"x-primary-only": "1"}}}
    _, (child,), _ = _spawn(cfg, {"anthropic": COOLDOWN})
    assert child["provider"] == "anthropic-overflow"
    assert "x-primary-only" not in json.dumps(child.get("request_overrides") or {})
