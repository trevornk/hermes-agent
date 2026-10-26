"""``status_callback(kind, text, category=...)`` typed-category passthrough.

Context-pressure warnings ride the legacy ``warn`` status kind so every
existing consumer (chat gateways posting the text, older TUI clients)
keeps working unchanged, but they also carry a typed ``category`` so a
renderer can key the alert without sniffing message text. This must reach
the emitted ``status.update`` payload end-to-end: ``AIAgent`` calling
``status_callback`` -> ``_agent_cbs``'s per-session lambda -> ``server.py``'s
``_status_update`` -> the payload handed to ``_emit``.

A category-less call must keep the existing payload shape exactly (no
``category`` key at all), so old clients that assert payload equality do
not regress.
"""

from __future__ import annotations

import importlib

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def server():
    # Mocks are scoped to the initial import only (see
    # tests/tui_gateway/test_protocol.py for the rationale).
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test_status_category")
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        mod = importlib.import_module("tui_gateway.server")
    yield mod


def _capture(server, monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(
        server, "_emit", lambda event, sid, payload=None: events.append(payload or {})
    )
    return events


def test_status_update_with_category_adds_category_key(server, monkeypatch):
    events = _capture(server, monkeypatch)
    server._status_update("sid", "warn", "\u26a0 pressure", category="context_pressure")

    assert events == [{"kind": "warn", "text": "\u26a0 pressure", "category": "context_pressure"}]


def test_status_update_without_category_omits_the_key(server, monkeypatch):
    """No regression for existing clients: a plain call must not gain a
    ``category`` key at all (not even ``None``)."""
    events = _capture(server, monkeypatch)
    server._status_update("sid", "warn", "\u26a0 unrelated failure")

    assert events == [{"kind": "warn", "text": "\u26a0 unrelated failure"}]
    assert "category" not in events[0]


def test_agent_status_update_forwards_category(server, monkeypatch):
    events = _capture(server, monkeypatch)
    monkeypatch.setattr(server, "_agent_presentation_enabled", lambda *a, **k: True)

    server._agent_status_update("sid", "warn", "\u26a0 pressure", category="context_pressure")

    assert events == [{"kind": "warn", "text": "\u26a0 pressure", "category": "context_pressure"}]


def test_status_callback_lambda_in_agent_cbs_accepts_category(server, monkeypatch):
    """The lambda AIAgent actually calls (registered under 'status_callback' in
    _agent_cbs) must accept a positional/keyword category and thread it through
    — this is the seam a typed-only StatusOutputMixin consumer probes with
    _accepts_keyword_argument before passing category=..."""
    events = _capture(server, monkeypatch)
    monkeypatch.setattr(server, "_agent_presentation_enabled", lambda *a, **k: True)
    monkeypatch.setattr(
        server, "_sessions", {"sid": {"agent": None}}, raising=False
    )

    cbs = server._agent_cbs("sid")
    status_cb = cbs["status_callback"]

    # Signature must accept category as keyword without raising.
    status_cb("warn", "\u26a0 pressure", category="context_pressure")

    assert events == [{"kind": "warn", "text": "\u26a0 pressure", "category": "context_pressure"}]

    # And a legacy two-positional-arg call (no category) must still work.
    events.clear()
    status_cb("lifecycle", "some other status")
    assert events == [{"kind": "lifecycle", "text": "some other status"}]
