"""Unit tests for the shared helpers in tests/utils.py."""

from tests.utils import MessageBusManager


class _RecordingAgent:
    def __init__(self, events):
        self._events = events

    def disconnect(self):
        self._events.append("agent.disconnect")


class _RecordingBus:
    def __init__(self, events):
        self._events = events

    def is_running(self):
        return True

    def stop(self):
        self._events.append("bus.stop")


def test_stop_bus_disconnects_agents_before_stopping_the_bus(monkeypatch):
    """An agent left connected when the bus stops reconnects forever."""
    monkeypatch.setattr("tests.utils.time.sleep", lambda _seconds: None)
    events = []
    manager = MessageBusManager()
    manager.bus = _RecordingBus(events)
    manager.agents = [_RecordingAgent(events), _RecordingAgent(events)]

    manager.stop_bus()

    assert events == ["agent.disconnect", "agent.disconnect", "bus.stop"]
    assert manager.agents == []
    assert manager.bus is None
