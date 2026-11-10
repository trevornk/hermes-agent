"""Regressions: a model-scoped cooldown must not hide a pool credential from other models, and the
vision auto-fallback must not forward the unavailable provider's model id to another provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import patch

from agent import auxiliary_client as aux


class _ScopedPool:
    """Pool whose only entry is benched for one model (mirrors model_cooldown_until semantics)."""

    def __init__(self) -> None:
        self.entry = SimpleNamespace(id="e1")
        self.calls: list[Optional[str]] = []

    def select(self, *, model: Optional[str] = None) -> Any:
        self.calls.append(model)
        return None if model in (None, "benched-model") else self.entry


def test_select_pool_entry_forwards_model() -> None:
    pool = _ScopedPool()
    with patch.object(aux, "_load_pool_with_credentials", return_value=pool):
        assert aux._select_pool_entry("openai-codex") == (True, None)
        assert aux._select_pool_entry("openai-codex", "gpt-6-astra") == (True, pool.entry)
    assert pool.calls == [None, "gpt-6-astra"]


def test_codex_credential_resolution_is_model_scoped() -> None:
    pool = _ScopedPool()
    with patch.object(aux, "_load_pool_with_credentials", return_value=pool), \
            patch.object(aux, "_codex_base_url_override", return_value="https://codex.example/v1"), \
            patch.object(aux, "_pool_runtime_api_key", side_effect=lambda e: "tok" if e else None), \
            patch.object(aux, "_read_codex_singleton_token", return_value=None):
        # Another model's bench must not make this model look credential-less...
        assert aux._resolve_codex_credential_and_base("gpt-6-astra") == ("tok", "https://codex.example/v1")
        # ...while the benched model itself still honors its cooldown.
        assert aux._resolve_codex_credential_and_base("benched-model")[0] is None
    assert pool.calls == ["gpt-6-astra", "benched-model"]


def test_vision_auto_fallback_drops_explicit_model() -> None:
    seen: list[dict[str, Any]] = []
    fallback_client = object()

    def fake_resolve(**kwargs: Any) -> Any:
        seen.append(kwargs)
        if kwargs["provider"] == "auto":
            return "anthropic", fallback_client, "claude-default"
        return "openai-codex", None, None

    with patch.object(aux, "resolve_vision_provider_client", side_effect=fake_resolve):
        route = aux._resolve_call_client(
            "vision", provider=None, model=None, base_url=None, api_key=None,
            resolved_provider="openai-codex", resolved_model="gpt-6-astra",
            resolved_base_url=None, resolved_api_key=None, resolved_api_mode=None,
            main_runtime=None, async_mode=False,
        )
    assert route.client is fallback_client
    assert route.final_model == "claude-default"
    assert seen[0]["model"] == "gpt-6-astra"
    assert seen[1]["provider"] == "auto" and seen[1]["model"] is None
