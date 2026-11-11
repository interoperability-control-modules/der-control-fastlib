"""Unit tests for the shared helpers in tests/utils.py."""

import logging
from types import SimpleNamespace

from tests.utils import MessageBusManager, records_from


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


def _record(name, levelno):
    return logging.LogRecord(name, levelno, __file__, 0, f"{name} at {levelno}", None, None)


def _caplog(*records):
    return SimpleNamespace(records=list(records))


def test_records_from_matches_the_logger_and_its_children_but_not_a_prefix_sibling():
    exact = _record("derhost.server", logging.WARNING)
    child = _record("derhost.server.x", logging.WARNING)
    sibling = _record("derhost.serverx", logging.WARNING)
    other = _record("derhost.client", logging.WARNING)

    found = records_from(_caplog(exact, child, sibling, other), "derhost.server")

    assert found == [exact, child]


def test_records_from_level_is_an_exact_match():
    info = _record("derhost.server", logging.INFO)
    warning = _record("derhost.server", logging.WARNING)
    error = _record("derhost.server", logging.ERROR)
    caplog = _caplog(info, warning, error)

    assert records_from(caplog, "derhost.server", logging.WARNING) == [warning]
    assert records_from(caplog, "derhost.server") == [info, warning, error]
