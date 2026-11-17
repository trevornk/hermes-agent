"""Strict raw tools/list schemas captured from CuaDriver 0.38.0, without capability tokens."""
import json
from pathlib import Path

import jsonschema
import pytest

from tools.computer_use.cua_backend import CuaDriverBackend


SCHEMAS = json.loads((Path(__file__).parents[1] / "fixtures/cua_driver_0_38_input_schemas.json").read_text())


@pytest.fixture
def backend():
    b = CuaDriverBackend()
    b._active_pid, b._active_window_id = 42, 84
    b._snapshot_tokens = {7: "s00000001:7"}
    b._session._tool_schemas = SCHEMAS.copy()
    b._session._capabilities = {name: set() for name in SCHEMAS}
    calls = []

    def call(name, args, **kwargs):
        jsonschema.validate(args, b._session._tool_schemas[name])
        calls.append((name, args.copy()))
        return {"data": {}, "structuredContent": {}, "isError": False}

    b._session.call_tool = call
    return b, calls


def test_cua038_requests_structured_elements(backend):
    b, _ = backend
    assert b._gws_args()["tree_format"] == "elements"
    jsonschema.validate(b._gws_args(), SCHEMAS["get_window_state"])
    b._session._tool_schemas = {}
    assert "tree_format" not in b._gws_args()


@pytest.mark.parametrize("action,kwargs", [
    ("click", {"element": 7}),
    ("click", {"element": 7, "click_count": 2}),
    ("scroll", {"element": 7, "direction": "down"}),
    ("set_value", {"element": 7, "value": "canary"}),
])
def test_token_only_actions_drop_removed_index(backend, action, kwargs):
    b, calls = backend
    assert getattr(b, action)(**kwargs).ok
    assert calls[-1][1]["element_token"] == "s00000001:7"
    assert "element_index" not in calls[-1][1]


@pytest.mark.parametrize("action,kwargs", [
    ("click", {"element": 8}),
    ("scroll", {"element": 8, "direction": "down"}),
    ("set_value", {"element": 8, "value": "canary"}),
])
def test_missing_token_refuses_before_transport(backend, action, kwargs):
    b, calls = backend
    assert not getattr(b, action)(**kwargs).ok
    assert not calls


@pytest.mark.parametrize("kwargs", [{"button": "right"}, {"modifiers": ["shift"]}])
def test_element_double_click_cannot_degrade_to_single_activation(backend, kwargs):
    b, calls = backend
    assert not b.click(element=7, click_count=2, **kwargs).ok
    assert not calls


def test_pixel_double_click_preserves_window_origin_button_and_modifier(backend):
    b, calls = backend
    assert b.click(x=13, y=17, click_count=2, button="right", modifiers=["shift"], delivery_mode="foreground").ok
    name, args = calls[-1]
    assert name == "click"
    assert (args["count"], args["button"], args["modifier"], args["window_id"], args["x"], args["y"]) == (2, "right", ["shift"], 84, 13, 17)


@pytest.mark.parametrize("count", [0, 3, -1])
def test_invalid_click_count_refused(backend, count):
    b, calls = backend
    assert not b.click(element=7, click_count=count).ok
    assert not calls


def test_element_drag_refused_when_only_pixels_supported(backend):
    b, calls = backend
    assert not b.drag(from_element=7, to_element=8).ok
    assert not calls


def test_pixel_drag_preserves_button_and_modifiers(backend):
    b, calls = backend
    assert b.drag(from_xy=(3, 4), to_xy=(5, 6), button="right", modifiers=["shift"]).ok
    assert calls[-1][1]["button"] == "right"
    assert calls[-1][1]["modifier"] == ["shift"]


def test_raw_schema_scroll_coordinates_not_capability_tokens(backend):
    b, calls = backend
    assert b.scroll(direction="down", x=12, y=14).ok
    assert (calls[-1][1]["x"], calls[-1][1]["y"]) == (12, 14)


def test_unsupported_scroll_coordinates_refused_not_window_wide(backend):
    b, calls = backend
    b._session._tool_schemas = {**SCHEMAS, "scroll": {"properties": {"direction": {}, "amount": {}, "pid": {}, "session": {}}}}
    assert not b.scroll(direction="down", x=12, y=14).ok
    assert not calls


def test_element_scroll_without_window_refuses(backend):
    b, calls = backend
    b._active_window_id = None
    assert not b.scroll(direction="down", element=7).ok
    assert not calls


def test_scroll_modifiers_refused_not_dropped(backend):
    b, calls = backend
    assert not b.scroll(direction="down", element=7, modifiers=["shift"]).ok
    assert not calls


@pytest.mark.parametrize("action,kwargs", [("type_text", {"text": "canary"}), ("key", {"keys": "a"}), ("key", {"keys": "cmd+t"})])
def test_keyboard_siblings_match_raw_schemas(backend, action, kwargs):
    b, calls = backend
    assert getattr(b, action)(**kwargs).ok
    assert len(calls) == 1


def test_legacy_index_and_token_preserved(backend):
    b, calls = backend
    old = {**SCHEMAS["click"], "properties": {**SCHEMAS["click"]["properties"], "element_index": {"type": "integer"}}}
    b._session._tool_schemas = {**SCHEMAS, "click": old}
    assert b.click(element=7).ok
    assert calls[-1][1]["element_index"] == 7
    assert calls[-1][1]["element_token"] == "s00000001:7"


def test_capture_replaces_tokens_with_real_frames(backend):
    b, _ = backend
    b._session.call_tool = lambda *a, **k: {"data": None, "isError": False, "structuredContent": {"elements": [{"element_index": 9, "role": "AXButton", "label": "New", "element_token": "s00000002:9", "frame": {"x": 10, "y": 20, "w": 30, "h": 40}}]}}
    _, _, elements, _ = b._capture_window_state()
    assert b._snapshot_tokens == {9: "s00000002:9"}
    assert elements[0].bounds != (0, 0, 0, 0)
