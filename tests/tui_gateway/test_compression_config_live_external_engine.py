"""Regression: live compression config hot-reload must not crash on an external
(non-built-in) ``context.engine`` plugin such as hermes-lcm.

``_apply_live_compression_config`` unconditionally called
``cc._coerce_threshold_tokens_cap(...)`` — a ``ContextCompressor``-only private
helper — on whatever object is bound to ``agent.context_compressor``. Per the
host contract documented in ``agent/agent_init.py`` (`_build_context_engine`,
see also fix 60391d0eef1), an external context engine OWNS its own compaction
policy end-to-end and the host's ``compression.*`` tuning knobs never reach it.
Calling a ``ContextCompressor``-private method on a plugin engine that doesn't
implement it raised ``AttributeError`` on every live-config sync (e.g. after
``hermes config set compression.threshold_tokens ...`` on a running Desktop/TUI
session with ``context.engine: lcm``), observed 3x post-update in the field.

The fix mirrors the SAME isinstance(cc, ContextCompressor) guard already used
at the two other call sites that touch ContextCompressor-private state
(``tools/delegate_tool.py::_apply_child_compression_cap``,
``agent/conversation_compression.py::_capture_authoritative_cooldown_under_lease``)
rather than a bare ``hasattr`` patch on the one crashing line — every attribute
this function pokes (``tail_mode``, ``proactive_prune_*``, ``model_thresholds``,
``_config_threshold_percent``, ``threshold_tokens_cap``, ...) is
ContextCompressor-specific tuning that must not touch a plugin engine's state.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from agent.context_compressor import ContextCompressor
from agent.context_engine import ContextEngine
from tui_gateway import server


class _FakeExternalEngine(ContextEngine):
    """Minimal stand-in for a plugin engine (e.g. hermes-lcm's LCMEngine).

    Deliberately does NOT implement ``_coerce_threshold_tokens_cap`` or any
    other ContextCompressor-private helper — plugin engines only implement the
    public ``ContextEngine`` ABC contract.
    """

    def __init__(self) -> None:
        self.threshold_tokens = 50_000
        self.context_length = 200_000
        self.threshold_percent = 0.75
        self.tail_mode = "external-native"  # engine-owned; host must not overwrite

    @property
    def name(self) -> str:
        return "fake-external"

    def update_from_response(self, usage: dict[str, Any]) -> None:
        pass

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        return False

    def compress(
        self, messages: list[dict[str, Any]], current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None, force: bool = False, memory_context: str = "",
    ) -> list[dict[str, Any]]:
        return messages


def _external_engine_session():
    engine = _FakeExternalEngine()
    agent = SimpleNamespace(
        model="external-model",
        provider="",
        context_compressor=engine,
        compression_enabled=True,
        compression_idle_compact_after_seconds=0,
        codex_responses_native_compaction=False,
        codex_responses_compact_threshold=200_000,
    )
    return {"agent": agent, "session_key": "session-external"}, engine


def test_live_compression_config_sync_does_not_crash_on_external_engine(monkeypatch):
    """RED (pre-fix): raised AttributeError: '_FakeExternalEngine' object has no
    attribute '_coerce_threshold_tokens_cap' — the exact crash observed with LCM."""
    session, engine = _external_engine_session()
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "model": {"context_length": 200_000},
            "compression": {"threshold_tokens": 100_000, "tail_mode": "legacy"},
        },
    )

    # Must not raise.
    server._apply_live_compression_config(session["agent"], server._load_cfg())

    # The engine instance survives untouched as an engine (sanity: no crash mid-mutation).
    assert session["agent"].context_compressor is engine


def test_external_engine_agent_level_settings_still_apply(monkeypatch):
    """Settings that live on ``agent`` (not the compressor) are host-owned even for
    external engines and must keep applying live — only ContextCompressor-private
    tuning is skipped."""
    session, _ = _external_engine_session()
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "compression": {
                "enabled": False,
                "codex_responses_native": True,
                "codex_responses_compact_threshold": 120_000,
                "idle_compact_after_seconds": 900,
            }
        },
    )

    server._sync_agent_compression_with_config("sid-external", session)

    agent = session["agent"]
    assert agent.compression_enabled is False
    assert agent.codex_responses_native_compaction is True
    assert agent.codex_responses_compact_threshold == 120_000
    assert agent.compression_idle_compact_after_seconds == 900


def test_external_engine_own_tuning_is_not_overwritten_by_host_config(monkeypatch):
    """The external engine owns its own compaction policy end-to-end (per the
    host contract in agent_init.py's _build_context_engine): host compression.*
    tuning must not clobber engine-owned state like ``tail_mode`` or
    ``threshold_tokens``."""
    session, engine = _external_engine_session()
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "model": {"context_length": 999_999},
            "compression": {
                "threshold_tokens": 1,
                "tail_mode": "legacy",
                "target_ratio": 0.99,
                "protect_last_n": 1,
            },
        },
    )

    server._sync_agent_compression_with_config("sid-external", session)

    # Untouched: the plugin engine manages these itself.
    assert engine.tail_mode == "external-native"
    assert engine.threshold_tokens == 50_000
    assert engine.context_length == 200_000


def test_native_compressor_threshold_tokens_cap_still_applies(monkeypatch):
    """GREEN control: the built-in ContextCompressor keeps full live-tuning behavior."""
    compressor = ContextCompressor(
        model="gpt-5.6-sol", threshold_percent=0.85, config_context_length=272_000, quiet_mode=True,
    )
    agent = SimpleNamespace(
        model="gpt-5.6-sol", provider="openai-codex", context_compressor=compressor,
        compression_enabled=True, compression_idle_compact_after_seconds=0,
        codex_responses_native_compaction=False, codex_responses_compact_threshold=200_000,
    )
    session = {"agent": agent, "session_key": "session-native"}
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {
            "model": {"context_length": 272_000},
            "compression": {"threshold_tokens": 100_000},
        },
    )

    server._sync_agent_compression_with_config("sid-native", session)

    assert compressor.threshold_tokens == 100_000
    assert compressor.threshold_tokens_cap == 100_000
