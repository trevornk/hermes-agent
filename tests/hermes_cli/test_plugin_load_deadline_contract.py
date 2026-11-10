"""Cooperative import/register deadlines without importing the Hermes runtime.

Execute the production function bodies with controlled dependencies. AST selection
is only an isolation boundary; assertions exercise calls, results and worker state,
never the spelling or structure of the source.
"""

import ast
import contextvars
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def loader():
    source = Path(__file__).resolve().parents[2] / "hermes_cli" / "plugins_loader.py"
    names = {"run_with_load_deadline", "_reserve_abandoned_loader_slot", "PluginLoadTimeout"}
    nodes = [node for node in ast.parse(source.read_text()).body if getattr(node, "name", None) in names]
    namespace = {
        "contextvars": contextvars,
        "threading": threading,
        "time": SimpleNamespace(monotonic=lambda: 100.0),
        "_resolve_plugin_load_timeout": lambda: 10.0,
        "_IN_PLUGIN_LOAD": contextvars.ContextVar("test_load_scope", default=None),
        "_ABANDONED_LOADERS": [],
        "_ABANDONED_LOADERS_LOCK": threading.Lock(),
        "_MAX_ABANDONED_LOADERS": 1,
        "_LOADER_THREAD_PREFIX": "test-plugin-load:",
    }
    future = ast.parse("from __future__ import annotations").body
    module = ast.Module(body=[*future, *nodes], type_ignores=[])
    exec(compile(module, str(source), "exec"), namespace)
    return namespace


class Context:
    def __init__(self):
        self.abandoned = False

    def _abandon_load(self):
        self.abandoned = True


def test_import_and_register_share_published_deadline_and_nested_budget(loader):
    outer, nested, unrelated = Context(), Context(), Context()
    unrelated._load_deadline = 777.0
    profile_scope = contextvars.ContextVar("test_profile", default=None)
    token = profile_scope.set("fixture-profile")
    caller = threading.current_thread()
    observations = []

    def register():
        observations.append((nested._load_deadline, profile_scope.get(), threading.current_thread()))
        return "registered"

    def import_and_register():
        observations.append((outer._load_deadline, profile_scope.get(), threading.current_thread()))
        # Import has consumed time. A nested load must neither resolve a new
        # timeout nor extend the absolute deadline from a later clock reading.
        loader["time"].monotonic = lambda: 900.0

        def unexpected_timeout_resolution():
            raise AssertionError("nested load resolved a fresh budget")

        loader["_resolve_plugin_load_timeout"] = unexpected_timeout_resolution
        result = loader["run_with_load_deadline"]("nested", nested, register)
        assert loader["_IN_PLUGIN_LOAD"].get() == [outer]
        return result

    try:
        assert loader["run_with_load_deadline"]("outer", outer, import_and_register) == "registered"
    finally:
        profile_scope.reset(token)
    assert [row[:2] for row in observations] == [(110.0, "fixture-profile")] * 2
    assert observations[0][2] is observations[1][2] and observations[0][2] is not caller
    assert unrelated._load_deadline == 777.0
    assert loader["_IN_PLUGIN_LOAD"].get() is None
    assert not outer.abandoned and not nested.abandoned


@pytest.mark.parametrize("inherited", [None, 42.0])
def test_reentrant_load_inherits_outer_deadline_without_own_budget(loader, inherited):
    outer, nested = Context(), Context()
    if inherited is not None:
        outer._load_deadline = inherited
    nested._load_deadline = 999.0
    scope = [outer]
    token = loader["_IN_PLUGIN_LOAD"].set(scope)
    try:
        assert loader["run_with_load_deadline"]("nested", nested, lambda: nested._load_deadline) == inherited
        assert scope == [outer]
    finally:
        loader["_IN_PLUGIN_LOAD"].reset(token)


def test_timeout_zero_publishes_none_and_runs_inline(loader):
    loader["_resolve_plugin_load_timeout"] = lambda: 0.0
    ctx = Context()
    ctx._load_deadline = 999.0
    caller = threading.current_thread()
    result = loader["run_with_load_deadline"]("disabled", ctx, lambda: (ctx._load_deadline, threading.current_thread()))
    assert result == (None, caller)
    assert not ctx.abandoned and not loader["_ABANDONED_LOADERS"]


def test_worker_propagates_same_base_exception(loader):
    failure = KeyboardInterrupt("fixture cancellation")

    def fail():
        raise failure

    ctx = Context()
    with pytest.raises(KeyboardInterrupt) as raised:
        loader["run_with_load_deadline"]("failure", ctx, fail)
    assert raised.value is failure
    assert not ctx.abandoned and not loader["_ABANDONED_LOADERS"]


def test_abandoned_worker_cap_refuses_before_work(loader):
    loader["_ABANDONED_LOADERS"].append(SimpleNamespace(is_alive=lambda: True))

    def forbidden_work():
        pytest.fail("worker cap must not fall back to inline work")

    with pytest.raises(loader["PluginLoadTimeout"], match="abandoned plugin loader"):
        loader["run_with_load_deadline"]("capped", Context(), forbidden_work)


def test_timeout_abandons_only_active_contexts_and_releases_worker(loader):
    loader["_resolve_plugin_load_timeout"] = lambda: 2.0
    outer, done, active = Context(), Context(), Context()
    entered, release = threading.Event(), threading.Event()

    def blocked_register():
        entered.set()
        assert release.wait(10), "fixture worker release missing"

    def import_and_register():
        loader["run_with_load_deadline"]("done", done, lambda: None)
        loader["run_with_load_deadline"]("active", active, blocked_register)

    try:
        with pytest.raises(loader["PluginLoadTimeout"], match="load timed out after 2s"):
            loader["run_with_load_deadline"]("outer", outer, import_and_register)
        assert entered.is_set()
        assert outer.abandoned and active.abandoned and not done.abandoned
        assert len(loader["_ABANDONED_LOADERS"]) == 1
    finally:
        release.set()
        for worker in loader["_ABANDONED_LOADERS"]:
            worker.join(10)
            assert not worker.is_alive()
