"""Memory-trim coverage for the long-lived messaging gateway housekeeper."""

import gateway.run as gateway_run


class _OneTickStopEvent:
    """Run one housekeeping tick without a sleep or background thread."""

    def __init__(self):
        self.waited = False

    def is_set(self):
        return self.waited

    def wait(self, timeout=None):
        self.waited = True
        return True


def test_gateway_housekeeping_calls_periodic_memory_trim(monkeypatch):
    from hermes_cli import mem_trim

    calls = []
    monkeypatch.setattr(
        mem_trim,
        "trim_memory",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    gateway_run._start_gateway_housekeeping(_OneTickStopEvent(), interval=0)

    # Timer lane: allocator trim only. A forced full gc.collect() here held the GIL for 1-9.7 s
    # once a minute on a live gateway (#139763), past the worker WebSocket open timeout.
    assert calls == [{"reason": "messaging gateway housekeeping", "collect": False}]
