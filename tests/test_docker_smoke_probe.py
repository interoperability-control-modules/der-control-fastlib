"""docker/lib/smoke_probe.py against a stdlib HTTP server standing in for /api/rpc/call (#96).

The interpreter runs as smoke.sh runs it, as a subprocess, so the exit status
and the lines on stdout and stderr are what is asserted.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import http.server
import importlib.util
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKER_DIR = REPO_ROOT / "docker"
INTERPRETER = DOCKER_DIR / "lib" / "smoke_probe.py"

AGENT_PROBES = {
    "interoperability-service": ("platform.presentation", "lookup_transform", ["sunspec", "2030.5"]),
    "realtime-control-agent": ("der.rtc", "list_modes", []),
}

RPC_PROBE: dict[str, Any] = {
    "identity": "demo.agent",
    "kind": "rpc",
    "method": "list_things",
    "args": ["a", 2],
    "kwargs": {"k": "v"},
    "expect": {"type": "list", "min_length": 2},
}


class _RpcServer:
    """Answers POST /api/rpc/call with a configurable body and records requests."""

    def __init__(self) -> None:
        outer = self
        self.reply: Any = {"success": True, "result": ["x", "y"]}
        self.status = 200
        self.raw: bytes | None = None
        # Bytes written to the socket as the whole reply, status line included.
        self.wire: bytes | None = None
        self.headers: dict[str, str] = {}
        # Sends the body two bytes at a time, pausing between pieces.
        self.drip = False
        self.delay = threading.Event()
        self.requests: list[dict[str, Any]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append({"path": self.path, "body": json.loads(self.rfile.read(length))})
                if outer.delay.is_set():
                    # Longer than any client timeout in these tests.
                    threading.Event().wait(2)
                if outer.wire is not None:
                    self.wfile.write(outer.wire)
                    return
                body = outer.raw if outer.raw is not None else json.dumps(outer.reply).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in outer.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if not outer.drip:
                    self.wfile.write(body)
                    return
                try:
                    for start in range(0, len(body), 2):
                        self.wfile.write(body[start : start + 2])
                        self.wfile.flush()
                        threading.Event().wait(0.3)
                except OSError:
                    # The client gave up on the slow reply, which is what the test wants.
                    return

            def log_message(self, *args: Any) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


def _serving() -> Iterator[_RpcServer]:
    server = _RpcServer()
    server.thread.start()
    try:
        yield server
    finally:
        server.server.shutdown()
        server.thread.join(timeout=5)
        server.server.server_close()


@pytest.fixture
def rpc_server() -> Iterator[_RpcServer]:
    yield from _serving()


@pytest.fixture
def other_server() -> Iterator[_RpcServer]:
    """A second server, for a request that must never reach it."""
    yield from _serving()


def _write_probe(tmp_path: Path, probe: Any) -> Path:
    path = tmp_path / "smoke-probe.json"
    path.write_text(probe if isinstance(probe, str) else json.dumps(probe), encoding="utf-8")
    return path


def _probe(
    probe_file: Path,
    url: str,
    *extra: str,
    python: str = sys.executable,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [python, str(INTERPRETER), str(probe_file), url, *extra],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_a_matching_rpc_result_passes_and_sends_the_documented_request(tmp_path: Path, rpc_server: _RpcServer) -> None:
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "probe ok: demo.agent list_things: list length 2"
    assert result.stderr == ""
    assert rpc_server.requests == [
        {
            "path": "/api/rpc/call",
            "body": {"agent_id": "demo.agent", "method": "list_things", "args": ["a", 2], "kwargs": {"k": "v"}},
        }
    ]


def test_args_and_kwargs_default_to_empty(tmp_path: Path, rpc_server: _RpcServer) -> None:
    probe = {k: v for k, v in RPC_PROBE.items() if k not in ("args", "kwargs")}
    assert _probe(_write_probe(tmp_path, probe), rpc_server.url).returncode == 0
    assert rpc_server.requests[0]["body"] == {
        "agent_id": "demo.agent",
        "method": "list_things",
        "args": [],
        "kwargs": {},
    }


def test_the_python3_on_path_which_smoke_sh_invokes_runs_it(tmp_path: Path, rpc_server: _RpcServer) -> None:
    # The same interpreter as pytest's when a venv is on PATH; a different one
    # only where the host's python3 is another version.
    path_python = shutil.which("python3")
    assert path_python is not None
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url, python=path_python)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "probe ok: demo.agent list_things" in result.stdout


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        pytest.param({"success": True, "result": ["only-one"]}, "result length 1 is below min_length 2", id="short"),
        pytest.param({"success": True, "result": []}, "result length 0 is below min_length 2", id="empty"),
        pytest.param({"success": True, "result": {"a": 1, "b": 2}}, "result is dict, expected list", id="type"),
        pytest.param({"success": True, "result": None}, "result is NoneType, expected list", id="none"),
        pytest.param({"success": True}, "reply has no 'result'", id="no-result"),
    ],
)
def test_a_result_that_misses_expect_fails_with_the_reason(
    tmp_path: Path, rpc_server: _RpcServer, reply: dict[str, Any], message: str
) -> None:
    rpc_server.reply = reply
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.returncode == 1
    assert result.stdout == ""
    assert message in result.stderr, result.stderr


def test_a_false_success_prints_the_agent_error_and_a_result_does_not_rescue_it(
    tmp_path: Path, rpc_server: _RpcServer
) -> None:
    # A failure is still HTTP 200; a result list that would satisfy expect
    # must not rescue it.
    rpc_server.reply = {"success": False, "error": "Method nope not found or not exported", "result": ["x", "y"]}
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "probe failed: demo.agent.list_things failed: Method nope not found or not exported"
    )


@pytest.mark.parametrize("error", [pytest.param(None, id="null"), pytest.param("", id="empty")])
def test_a_false_success_without_a_usable_error_says_no_error_was_given(
    tmp_path: Path, rpc_server: _RpcServer, error: Any
) -> None:
    rpc_server.reply = {"success": False, "error": error}
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.stderr.strip() == "probe failed: demo.agent.list_things failed: no error given"
    rpc_server.reply = {"success": False}
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.stderr.strip() == "probe failed: demo.agent.list_things failed: no error given"


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        pytest.param({"result": ["x", "y"]}, "demo.agent.list_things reply has no 'success'", id="missing"),
        pytest.param(
            {"success": "true", "result": ["x", "y"]},
            "demo.agent.list_things reply 'success' is str, not a boolean",
            id="string",
        ),
        pytest.param(
            {"success": 1, "result": ["x", "y"]},
            "demo.agent.list_things reply 'success' is int, not a boolean",
            id="int",
        ),
        pytest.param(
            {"success": None, "result": ["x", "y"]},
            "demo.agent.list_things reply 'success' is NoneType, not a boolean",
            id="null",
        ),
        pytest.param(
            {"success": "no", "error": "agent said so"},
            "demo.agent.list_things reply 'success' is str, not a boolean (error: agent said so)",
            id="with-error",
        ),
    ],
)
def test_a_missing_or_non_boolean_success_is_named_as_such(
    tmp_path: Path, rpc_server: _RpcServer, reply: dict[str, Any], message: str
) -> None:
    rpc_server.reply = reply
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.returncode == 1
    assert result.stderr.strip() == f"probe failed: {message}"


def test_a_bool_does_not_satisfy_an_int_expect(tmp_path: Path, rpc_server: _RpcServer) -> None:
    rpc_server.reply = {"success": True, "result": True}
    probe = {**RPC_PROBE, "expect": {"type": "int"}}
    result = _probe(_write_probe(tmp_path, probe), rpc_server.url)
    assert result.returncode == 1
    assert "result is bool, expected int" in result.stderr


@pytest.mark.parametrize(
    ("kind", "good", "short"),
    [
        pytest.param("list", ["a"], [], id="list"),
        pytest.param("dict", {"a": 1}, {}, id="dict"),
        pytest.param("str", "a", "", id="str"),
    ],
)
def test_min_length_applies_to_every_sized_type(
    tmp_path: Path, rpc_server: _RpcServer, kind: str, good: Any, short: Any
) -> None:
    probe = _write_probe(tmp_path, {**RPC_PROBE, "expect": {"type": kind, "min_length": 1}})
    rpc_server.reply = {"success": True, "result": good}
    passed = _probe(probe, rpc_server.url)
    assert passed.returncode == 0, passed.stderr
    assert passed.stdout.strip() == f"probe ok: demo.agent list_things: {kind} length 1"
    rpc_server.reply = {"success": True, "result": short}
    failed = _probe(probe, rpc_server.url)
    assert failed.returncode == 1
    assert "result length 0 is below min_length 1" in failed.stderr


@pytest.mark.parametrize("value", [pytest.param(7, id="int"), pytest.param(7.5, id="float")])
def test_a_float_expect_accepts_an_int_and_a_float_but_not_a_bool(
    tmp_path: Path, rpc_server: _RpcServer, value: Any
) -> None:
    probe = _write_probe(tmp_path, {**RPC_PROBE, "expect": {"type": "float"}})
    rpc_server.reply = {"success": True, "result": value}
    passed = _probe(probe, rpc_server.url)
    assert passed.returncode == 0, passed.stderr
    assert passed.stdout.strip() == "probe ok: demo.agent list_things: float value ok"
    rpc_server.reply = {"success": True, "result": True}
    assert "result is bool, expected float" in _probe(probe, rpc_server.url).stderr


def test_a_scalar_expect_without_min_length_passes(tmp_path: Path, rpc_server: _RpcServer) -> None:
    rpc_server.reply = {"success": True, "result": 7}
    probe = {**RPC_PROBE, "expect": {"type": "int"}}
    result = _probe(_write_probe(tmp_path, probe), rpc_server.url)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "probe ok: demo.agent list_things: int value ok"


@pytest.mark.parametrize(
    ("probe", "message"),
    [
        pytest.param("{not json", "is not valid JSON", id="bad-json"),
        pytest.param("[1, 2]", "must hold a JSON object", id="not-object"),
        pytest.param(
            {k: v for k, v in RPC_PROBE.items() if k != "identity"}, "missing a string 'identity'", id="no-identity"
        ),
        pytest.param({k: v for k, v in RPC_PROBE.items() if k != "kind"}, "missing a string 'kind'", id="no-kind"),
        pytest.param(
            {k: v for k, v in RPC_PROBE.items() if k != "method"}, "missing a string 'method'", id="no-method"
        ),
        pytest.param(
            {k: v for k, v in RPC_PROBE.items() if k != "expect"}, "missing an object 'expect'", id="no-expect"
        ),
        pytest.param({**RPC_PROBE, "kind": "mqtt"}, "unknown probe kind 'mqtt'", id="unknown-kind"),
        pytest.param({**RPC_PROBE, "kwarg": {"k": "v"}}, "unknown probe keys ['kwarg']", id="unknown-key"),
        pytest.param({**RPC_PROBE, "args": "x"}, "'args' must be a list", id="args-type"),
        pytest.param({**RPC_PROBE, "kwargs": []}, "'kwargs' must be an object", id="kwargs-type"),
        pytest.param({**RPC_PROBE, "expect": {"type": "tuple"}}, "expect.type must be one of", id="expect-type"),
        pytest.param(
            {**RPC_PROBE, "expect": {"type": "list", "length": 1}}, "unknown expect keys ['length']", id="expect-key"
        ),
        pytest.param(
            {**RPC_PROBE, "expect": {"type": "list", "min_length": -1}},
            "min_length must be a non-negative integer",
            id="min-negative",
        ),
        pytest.param(
            {**RPC_PROBE, "expect": {"type": "list", "min_length": True}},
            "min_length must be a non-negative integer",
            id="min-bool",
        ),
        pytest.param(
            {**RPC_PROBE, "expect": {"type": "int", "min_length": 1}},
            "min_length needs type list, dict or str",
            id="min-on-int",
        ),
    ],
)
def test_a_malformed_probe_file_fails_with_a_named_error_before_any_call(
    tmp_path: Path, rpc_server: _RpcServer, probe: Any, message: str
) -> None:
    result = _probe(_write_probe(tmp_path, probe), rpc_server.url)
    assert result.returncode == 1
    assert result.stderr.startswith("probe failed: "), result.stderr
    assert message in result.stderr, result.stderr
    assert rpc_server.requests == []


def test_a_missing_probe_file_fails_naming_it(tmp_path: Path, rpc_server: _RpcServer) -> None:
    result = _probe(tmp_path / "absent.json", rpc_server.url)
    assert result.returncode == 1
    assert "cannot read probe file" in result.stderr and "absent.json" in result.stderr


def test_an_identity_other_than_the_expected_one_fails(tmp_path: Path, rpc_server: _RpcServer) -> None:
    probe_file = _write_probe(tmp_path, RPC_PROBE)
    assert _probe(probe_file, rpc_server.url, "--identity", "demo.agent").returncode == 0
    result = _probe(probe_file, rpc_server.url, "--identity", "other.agent")
    assert result.returncode == 1
    assert "probe identity 'demo.agent' is not the expected 'other.agent'" in result.stderr


def test_a_refused_connection_fails_naming_the_url(tmp_path: Path) -> None:
    url = _closed_port_url()
    result = _probe(_write_probe(tmp_path, RPC_PROBE), url)
    assert result.returncode == 1
    assert f"rpc call could not reach {url}" in result.stderr, result.stderr


def test_an_http_error_status_fails(tmp_path: Path, rpc_server: _RpcServer) -> None:
    rpc_server.status = 422
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.returncode == 1
    assert "rpc call returned HTTP 422" in result.stderr


@pytest.mark.parametrize(("raw", "message"), [(b"<html>", "not valid JSON"), (b'["x"]', "not a JSON object")])
def test_a_reply_that_is_not_a_json_object_fails(
    tmp_path: Path, rpc_server: _RpcServer, raw: bytes, message: str
) -> None:
    rpc_server.raw = raw
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.returncode == 1
    assert message in result.stderr


def test_a_slow_server_fails_with_a_timeout(tmp_path: Path, rpc_server: _RpcServer) -> None:
    rpc_server.delay.set()
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url, "--timeout", "0.3")
    assert result.returncode == 1
    assert "rpc call timed out after 0.3s" in result.stderr, result.stderr


def test_the_default_timeout_outlasts_the_servers_own_rpc_timeout() -> None:
    spec = importlib.util.spec_from_file_location("smoke_probe_under_test", INTERPRETER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server_rpc_timeout = 30
    assert module.DEFAULT_TIMEOUT > server_rpc_timeout


def test_the_timeout_bounds_the_whole_call_not_each_read(tmp_path: Path, rpc_server: _RpcServer) -> None:
    # The reply takes about 5.7 s to arrive in pieces, each well inside one read timeout.
    rpc_server.drip = True
    started = time.monotonic()
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url, "--timeout", "1")
    elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert result.stderr.strip() == "probe failed: rpc call timed out after 1s"
    assert elapsed < 4, elapsed


@pytest.mark.parametrize(
    ("wire", "name"),
    [
        pytest.param(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort", "IncompleteRead", id="short-body"),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n", "IncompleteRead", id="broken-chunk"
        ),
        pytest.param(b"garbage\r\n\r\n", "BadStatusLine", id="bad-status-line"),
        pytest.param(b"", "RemoteDisconnected", id="closed-without-reply"),
    ],
)
def test_a_broken_http_reply_fails_as_a_named_probe_failure_not_a_traceback(
    tmp_path: Path, rpc_server: _RpcServer, wire: bytes, name: str
) -> None:
    rpc_server.wire = wire
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.returncode == 1
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith(f"probe failed: rpc call got a broken HTTP reply: {name}"), result.stderr


@pytest.mark.parametrize("status", [302, 303, 307])
def test_a_redirect_is_refused_by_name_and_never_followed(
    tmp_path: Path, rpc_server: _RpcServer, other_server: _RpcServer, status: int
) -> None:
    target = f"{other_server.url}/api/rpc/call"
    rpc_server.status = status
    rpc_server.headers = {"Location": target}
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.returncode == 1
    assert result.stderr.strip() == f"probe failed: rpc call was redirected (HTTP {status}) to {target}; not followed"
    assert len(rpc_server.requests) == 1
    assert other_server.requests == []


def test_proxy_environment_variables_are_ignored(tmp_path: Path, rpc_server: _RpcServer) -> None:
    # A proxy that is not listening: a probe that honored these would fail.
    dead_proxy = _closed_port_url()
    env = dict.fromkeys(("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"), dead_proxy)
    env.update({"no_proxy": "", "NO_PROXY": ""})
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(rpc_server.requests) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(b"database is down\n", "rpc call returned HTTP 500: database is down", id="body"),
        pytest.param(b"line one\r\n  line   two\n", "rpc call returned HTTP 500: line one line two", id="whitespace"),
        pytest.param(b"", "rpc call returned HTTP 500", id="empty"),
    ],
)
def test_an_http_error_carries_the_start_of_its_body(
    tmp_path: Path, rpc_server: _RpcServer, raw: bytes, expected: str
) -> None:
    rpc_server.status = 500
    rpc_server.raw = raw
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.returncode == 1
    assert result.stderr.strip() == f"probe failed: {expected}"


def test_a_long_http_error_body_is_cut_to_a_short_excerpt(tmp_path: Path, rpc_server: _RpcServer) -> None:
    rpc_server.status = 500
    rpc_server.raw = b"x" * 5000
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.stderr.startswith("probe failed: rpc call returned HTTP 500: xxx")
    assert len(result.stderr) < 300, len(result.stderr)


@pytest.mark.parametrize("url", ["ftp://127.0.0.1:1", "127.0.0.1:1", "http://", "http://127.0.0.1:notaport"])
def test_a_base_url_that_is_not_http_host_port_is_refused_by_name(tmp_path: Path, url: str) -> None:
    result = _probe(_write_probe(tmp_path, RPC_PROBE), url)
    assert result.returncode == 1
    assert result.stderr.startswith("probe failed: base URL "), result.stderr


# --- the two probe files smoke.sh runs. ------------------------------------


@pytest.mark.parametrize("agent", sorted(AGENT_PROBES))
def test_each_shipped_probe_file_names_its_agents_identity_and_call(agent: str) -> None:
    identity, method, args = AGENT_PROBES[agent]
    probe = json.loads((DOCKER_DIR / agent / "smoke-probe.json").read_text(encoding="utf-8"))
    assert probe == {
        "identity": identity,
        "kind": "rpc",
        "method": method,
        "args": args,
        "expect": {"type": "list", "min_length": 1},
    }
    dockerfile = (DOCKER_DIR / agent / "Dockerfile").read_text(encoding="utf-8")
    assert re.findall(r'"--identity", "([^"]*)"', dockerfile) == [identity]


@pytest.mark.parametrize("agent", sorted(AGENT_PROBES))
def test_each_shipped_probe_passes_a_good_reply_and_fails_an_empty_one(agent: str, rpc_server: _RpcServer) -> None:
    identity, method, args = AGENT_PROBES[agent]
    probe_file = DOCKER_DIR / agent / "smoke-probe.json"
    rpc_server.reply = {"success": True, "result": [{"any": "thing"}]}
    good = _probe(probe_file, rpc_server.url, "--identity", identity)
    assert good.returncode == 0, good.stdout + good.stderr
    assert good.stdout.strip() == f"probe ok: {identity} {method}: list length 1"
    assert rpc_server.requests[-1]["body"] == {"agent_id": identity, "method": method, "args": args, "kwargs": {}}
    rpc_server.reply = {"success": True, "result": []}
    empty = _probe(probe_file, rpc_server.url, "--identity", identity)
    assert empty.returncode == 1
    assert "below min_length 1" in empty.stderr


# --- topic probes (#159): a bus monitor, an optional trigger, one exact publish. ---

TOPIC = "devices/PNNL/grid_information/price/all"
SENDER = "der.grid_signals"
TOPIC_PROBE: dict[str, Any] = {
    "identity": SENDER,
    "kind": "topic",
    "topic": TOPIC,
    "within_s": 5,
    "trigger": {"method": "publish_price_signal", "args": [], "expect": {"type": "int"}},
    "select": [0, "tou"],
    "expect": {"type": "float"},
}
PRICE_MESSAGE = [{"tou": 12.5}, {"tou": {"units": "cents", "tz": "PST", "type": "float"}}]
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _frame(payload: Any, opcode: int = 1, fin: bool = True, masked: bool = False) -> bytes:
    """One server frame, unmasked as the bus sends it unless `masked` asks for the violation."""
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    head = bytes([(0x80 if fin else 0) | opcode])
    mask_bit = 0x80 if masked else 0
    if len(data) < 126:
        head += bytes([mask_bit | len(data)])
    elif len(data) < 1 << 16:
        head += bytes([mask_bit | 126]) + struct.pack("!H", len(data))
    else:
        head += bytes([mask_bit | 127]) + struct.pack("!Q", len(data))
    return head + (b"\0\0\0\0" if masked else b"") + data


def _pub(topic: str = TOPIC, sender: str = SENDER, message: Any = PRICE_MESSAGE, opcode: int = 1) -> bytes:
    return _frame(
        {
            "type": "pubsub_message",
            "timestamp": "t",
            "topic": topic,
            "sender": sender,
            "headers": {},
            "message": message,
        },
        opcode=opcode,
    )


class _BusServer:
    """Stands in for the server's RPC route and WS /monitor/{id}, recording what each saw."""

    def __init__(self) -> None:
        outer = self
        self.events: list[tuple[str, Any]] = []
        self.refuse = False
        self.bad_accept = False
        self.trigger_reply: Any = {"success": True, "result": 24}
        self.on_connect: list[bytes] = []
        self.on_trigger: list[bytes] = []
        self.client_frames: list[tuple[int, bytes]] = []
        self.handshakes: list[dict[str, str]] = []
        self.monitor_done = threading.Event()
        self._monitor_files: list[Any] = []
        # Written verbatim in place of the 101 reply, then the connection closes.
        self.handshake_wire: bytes | None = None
        # Holds the handshake reply back until `release` is set or 5 s pass.
        self.stall = False
        self.release = threading.Event()
        # Pause between a refusal's headers and its body, so they arrive apart.
        self.refuse_pause = 0.0
        # "drop" closes the TCP connection with no close frame; "reset" sends an RST
        # once the first RPC has been answered, so the client has read the handshake.
        self.after_handshake = ""
        self.rpc_answered = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                outer.events.append(("rpc", json.loads(self.rfile.read(length))))
                body = json.dumps(outer.trigger_reply).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                outer.rpc_answered.set()
                for wfile in outer._monitor_files:
                    for wire in outer.on_trigger:
                        wfile.write(wire)
                        wfile.flush()

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                outer.events.append(("monitor", self.path))
                outer.handshakes.append({k.lower(): v for k, v in self.headers.items()})
                if outer.stall:
                    outer.release.wait(5)
                    return
                if outer.handshake_wire is not None:
                    self.wfile.write(outer.handshake_wire)
                    return
                if outer.refuse:
                    body = b"monitor is already connected"
                    self.send_response(409)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.flush()
                    if outer.refuse_pause:
                        threading.Event().wait(outer.refuse_pause)
                    self.wfile.write(body)
                    outer.monitor_done.set()
                    return
                key = self.headers["Sec-WebSocket-Key"]
                digest = hashlib.sha1((key + _WS_GUID).encode()).digest()  # noqa: S324 - RFC 6455
                accept = base64.b64encode(b"x" * 20 if outer.bad_accept else digest).decode()
                self.wfile.write(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode()
                )
                outer._monitor_files.append(self.wfile)
                for wire in outer.on_connect:
                    self.wfile.write(wire)
                self.wfile.flush()
                try:
                    if outer.after_handshake == "drop":
                        return
                    if outer.after_handshake == "reset":
                        outer.rpc_answered.wait(5)
                        # Linger 0 makes the close an RST rather than a FIN.
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                        self.rfile.close()
                        self.connection.close()
                        return
                    while True:
                        head = self.rfile.read(2)
                        if len(head) < 2:
                            break
                        length = head[1] & 0x7F
                        mask = self.rfile.read(4)
                        data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(length)))
                        outer.client_frames.append((head[0] & 0x0F, data))
                        if head[0] & 0x0F == 0x8:
                            break
                except OSError:
                    pass
                finally:
                    # A later probe against this server must not write to a closed monitor.
                    outer._monitor_files.remove(self.wfile)
                    outer.monitor_done.set()

            def log_message(self, *args: Any) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def rpcs(self) -> list[Any]:
        return [body for kind, body in self.events if kind == "rpc"]


@pytest.fixture
def bus() -> Iterator[_BusServer]:
    server = _BusServer()
    server.thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.server.shutdown()
        server.thread.join(timeout=5)
        server.server.server_close()


def _topic_probe(tmp_path: Path, bus: _BusServer, probe: Any = TOPIC_PROBE) -> subprocess.CompletedProcess[str]:
    return _probe(_write_probe(tmp_path, probe), bus.url, "--timeout", "10")


def test_a_topic_probe_opens_the_monitor_then_triggers_then_passes_on_the_matching_frame(
    tmp_path: Path, bus: _BusServer
) -> None:
    bus.on_trigger = [_pub()]
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        f"probe ok: {SENDER} published {TOPIC} after publish_price_signal int value ok: float value ok"
    )
    assert [kind for kind, _ in bus.events] == ["monitor", "rpc"]
    assert re.fullmatch(rf"/monitor/smoke-{re.escape(SENDER)}-\d+-[0-9a-f]{{8}}", bus.events[0][1]), bus.events[0]
    assert bus.rpcs == [{"agent_id": SENDER, "method": "publish_price_signal", "args": [], "kwargs": {}}]
    assert bus.handshakes[0]["upgrade"] == "websocket"
    assert bus.handshakes[0]["sec-websocket-version"] == "13"
    assert bus.monitor_done.wait(5)
    assert bus.client_frames[-1][0] == 0x8


def test_a_topic_probe_without_a_trigger_passes_on_a_publish_and_calls_no_rpc(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_connect = [_pub()]
    probe = {k: v for k, v in TOPIC_PROBE.items() if k != "trigger"}
    result = _topic_probe(tmp_path, bus, probe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"probe ok: {SENDER} published {TOPIC}: float value ok"
    assert bus.rpcs == []


def test_each_monitor_id_is_unique_to_its_run(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_connect = [_pub()]
    probe = {k: v for k, v in TOPIC_PROBE.items() if k != "trigger"}
    for _ in range(2):
        assert _topic_probe(tmp_path, bus, probe).returncode == 0
    ids = [path for kind, path in bus.events if kind == "monitor"]
    assert len(ids) == 2 and ids[0] != ids[1]
    # Each part is pinned: the pid separates concurrent probes, the token separates
    # two runs that happen to share a pid.
    parts = [re.fullmatch(rf"/monitor/smoke-{re.escape(SENDER)}-(\d+)-([0-9a-f]{{8}})", path) for path in ids]
    assert all(parts), ids
    first, second = (match.groups() for match in parts if match)
    assert first[0] != second[0], ids
    assert first[1] != second[1], ids


def test_a_topic_with_no_publish_fails_at_within_s(tmp_path: Path, bus: _BusServer) -> None:
    result = _topic_probe(tmp_path, bus, {**TOPIC_PROBE, "within_s": 0.5})
    assert result.returncode == 1
    assert result.stderr.strip() == f"probe failed: no frame on {TOPIC} from {SENDER} within 0.5s"


def test_the_topic_from_another_sender_fails_naming_that_sender(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_trigger = [_pub(sender="impostor.agent")]
    result = _topic_probe(tmp_path, bus, {**TOPIC_PROBE, "within_s": 0.5})
    assert result.returncode == 1
    assert result.stderr.strip() == (
        f"probe failed: no frame on {TOPIC} from {SENDER} within 0.5s (topic published by impostor.agent)"
    )


def test_other_topics_are_skipped_until_the_exact_one_arrives(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_trigger = [_pub(topic=TOPIC + "/extra"), _pub(topic="devices/PNNL/other"), _pub()]
    assert _topic_probe(tmp_path, bus).returncode == 0


def test_a_longer_topic_does_not_match_an_exact_topic(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_trigger = [_pub(topic=TOPIC + "/extra")]
    result = _topic_probe(tmp_path, bus, {**TOPIC_PROBE, "within_s": 0.5})
    assert result.returncode == 1
    assert "no frame on" in result.stderr


def test_the_first_matching_frame_decides_so_a_later_good_one_does_not_rescue_a_bad_one(
    tmp_path: Path, bus: _BusServer
) -> None:
    bus.on_trigger = [_pub(message=[{"tou": "high"}]), _pub()]
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 1
    assert "message value is str, expected float" in result.stderr


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        pytest.param({"success": False, "error": "no price config"}, "failed: no price config", id="failed"),
        pytest.param({"success": True, "result": "24"}, "result is str, expected int", id="wrong-type"),
    ],
)
def test_a_failed_trigger_fails_and_waits_for_no_frame(
    tmp_path: Path, bus: _BusServer, reply: dict[str, Any], message: str
) -> None:
    bus.trigger_reply = reply
    bus.on_trigger = [_pub()]
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 1
    assert message in result.stderr, result.stderr
    assert result.stderr.startswith("probe failed: ")


@pytest.mark.parametrize(
    ("select", "message"),
    [
        pytest.param([0, "price"], 'select path [0, "price"] not found', id="missing-key"),
        pytest.param([5, "tou"], 'select path [5, "tou"] not found', id="index-out-of-range"),
        pytest.param(["tou"], 'select path ["tou"] not found', id="key-on-a-list"),
    ],
)
def test_a_select_path_that_does_not_exist_fails_naming_the_path(
    tmp_path: Path, bus: _BusServer, select: list[Any], message: str
) -> None:
    bus.on_trigger = [_pub()]
    result = _topic_probe(tmp_path, bus, {**TOPIC_PROBE, "select": select})
    assert result.returncode == 1
    assert message in result.stderr, result.stderr


def test_a_selected_value_of_the_wrong_type_fails(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_trigger = [_pub()]
    result = _topic_probe(tmp_path, bus, {**TOPIC_PROBE, "expect": {"type": "str"}})
    assert result.returncode == 1
    assert "message value is float, expected str" in result.stderr


def test_an_empty_select_checks_the_whole_message(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_trigger = [_pub()]
    probe = {k: v for k, v in TOPIC_PROBE.items() if k != "select"}
    result = _topic_probe(tmp_path, bus, {**probe, "expect": {"type": "list", "min_length": 2}})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("list length 2")


def test_a_refused_monitor_id_fails_naming_the_status_and_makes_no_call(tmp_path: Path, bus: _BusServer) -> None:
    bus.refuse = True
    # The body trails the headers, so the excerpt must be read from a later segment.
    bus.refuse_pause = 0.3
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 1
    assert "was refused (HTTP 409): monitor is already connected" in result.stderr, result.stderr
    assert bus.rpcs == []


def test_a_handshake_whose_accept_key_is_wrong_is_refused(tmp_path: Path, bus: _BusServer) -> None:
    bus.bad_accept = True
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 1
    assert "Sec-WebSocket-Accept does not match" in result.stderr
    assert bus.rpcs == []


def test_a_fragmented_message_is_joined_and_a_ping_is_answered(tmp_path: Path, bus: _BusServer) -> None:
    whole = _pub()
    payload = whole[2:] if whole[1] < 126 else whole[4:]
    half = len(payload) // 2
    bus.on_trigger = [
        _frame(b"ping-data", opcode=0x9),
        _frame(payload[:half], opcode=1, fin=False),
        _frame(b"mid-pong", opcode=0xA),
        _frame(payload[half:], opcode=0, fin=True),
    ]
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 0, result.stderr
    assert bus.monitor_done.wait(5)
    assert (0xA, b"ping-data") in bus.client_frames


def test_a_server_close_ends_the_probe_with_a_named_failure(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_trigger = [_frame(struct.pack("!H", 1008) + b"monitor too slow", opcode=0x8)]
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 1
    assert "monitor closed by the server" in result.stderr


def test_a_masked_server_frame_is_a_protocol_failure(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_trigger = [_frame({"x": 1}, masked=True)]
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 1
    assert "monitor sent a masked frame" in result.stderr


def test_an_oversize_frame_is_refused_without_reading_it(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_trigger = [bytes([0x81, 127]) + struct.pack("!Q", 1 << 30)]
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 1
    assert "over the" in result.stderr and "byte cap" in result.stderr


def test_a_monitor_that_cannot_be_reached_fails_naming_the_url(tmp_path: Path) -> None:
    url = _closed_port_url()
    result = _probe(_write_probe(tmp_path, TOPIC_PROBE), url)
    assert result.returncode == 1
    assert f"monitor could not reach {url}" in result.stderr


def test_a_topic_probe_gives_up_at_within_s_not_at_the_overall_timeout(tmp_path: Path, bus: _BusServer) -> None:
    within = 0.3
    start = time.monotonic()
    result = _probe(_write_probe(tmp_path, {**TOPIC_PROBE, "within_s": within}), bus.url, "--timeout", "20")
    elapsed = time.monotonic() - start
    assert result.returncode == 1
    assert result.stderr.strip() == f"probe failed: no frame on {TOPIC} from {SENDER} within {within}s"
    # Far under --timeout; the slack only absorbs interpreter start-up.
    assert elapsed < within + 5, elapsed


def _reserved_bits() -> bytes:
    wire = _frame({"x": 1})
    return bytes([wire[0] | 0x40]) + wire[1:]


@pytest.mark.parametrize(
    ("frames", "message"),
    [
        pytest.param([_reserved_bits()], "monitor sent a frame with reserved bits set", id="reserved-bits"),
        pytest.param(
            [_frame(b"abc", opcode=0)],
            "monitor sent a continuation frame with nothing to continue",
            id="bad-continuation",
        ),
        pytest.param(
            [_frame(b"{", opcode=1, fin=False), _frame(b"{", opcode=1)],
            "monitor started a new message inside a fragmented one",
            id="interleaved",
        ),
        pytest.param([_frame(b"", opcode=3)], "monitor sent an unknown frame opcode 3", id="unknown-opcode"),
        pytest.param(
            [_frame(b"x" * 600_000, opcode=1, fin=False), _frame(b"x" * 600_000, opcode=0)],
            "monitor message is over the 1048576-byte cap",
            id="joined-size-cap",
        ),
    ],
)
def test_a_malformed_monitor_frame_fails_with_its_named_reason(
    tmp_path: Path, bus: _BusServer, frames: list[bytes], message: str
) -> None:
    bus.on_trigger = frames
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 1
    assert result.stderr.strip() == f"probe failed: {message}", result.stderr


def test_a_binary_message_is_skipped_even_when_it_holds_the_matching_frame(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_trigger = [_pub(opcode=2)]
    result = _topic_probe(tmp_path, bus, {**TOPIC_PROBE, "within_s": 0.5})
    assert result.returncode == 1
    assert result.stderr.strip() == f"probe failed: no frame on {TOPIC} from {SENDER} within 0.5s"


def test_a_text_message_after_a_binary_one_still_decides(tmp_path: Path, bus: _BusServer) -> None:
    bus.on_trigger = [_pub(opcode=2), _pub()]
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "junk",
    [pytest.param(b"not json at all", id="non-json"), pytest.param(["a", "list"], id="non-object")],
)
def test_a_frame_that_is_not_a_json_object_is_skipped(tmp_path: Path, bus: _BusServer, junk: Any) -> None:
    bus.on_trigger = [_frame(junk), _pub()]
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 0, result.stderr


def test_a_tcp_drop_with_no_close_frame_is_a_connection_failure_not_a_missing_frame(
    tmp_path: Path, bus: _BusServer
) -> None:
    bus.after_handshake = "drop"
    probe = {k: v for k, v in TOPIC_PROBE.items() if k != "trigger"}
    result = _topic_probe(tmp_path, bus, probe)
    assert result.returncode == 1
    assert result.stderr.strip() == "probe failed: monitor connection closed by the server", result.stderr


def test_a_connection_reset_mid_read_is_a_named_probe_failure_not_a_traceback(tmp_path: Path, bus: _BusServer) -> None:
    bus.after_handshake = "reset"
    result = _topic_probe(tmp_path, bus)
    assert result.returncode == 1
    assert result.stderr.startswith("probe failed: monitor connection failed: "), result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("wire", "message"),
    [
        pytest.param(b"garbage", "monitor handshake got a broken reply: 'garbage'", id="early-end-of-stream"),
        pytest.param(b"HTTP/1.1 abc Nope\r\n\r\n", "got a broken reply: 'HTTP/1.1 abc Nope'", id="non-numeric-status"),
        pytest.param(b"HTTP/1.1\r\n\r\n", "got a broken reply: 'HTTP/1.1'", id="no-status-code"),
        pytest.param(b"x" * 20000, "monitor handshake reply is too large", id="reply-too-large"),
    ],
)
def test_a_broken_handshake_reply_fails_with_its_named_reason_and_makes_no_call(
    tmp_path: Path, bus: _BusServer, wire: bytes, message: str
) -> None:
    bus.handshake_wire = wire
    start = time.monotonic()
    result = _probe(_write_probe(tmp_path, TOPIC_PROBE), bus.url, "--timeout", "10")
    elapsed = time.monotonic() - start
    assert result.returncode == 1
    assert message in result.stderr, result.stderr
    assert "timed out" not in result.stderr
    # An end of stream must end the read at once, not run it out to --timeout.
    assert elapsed < 8, elapsed
    assert bus.rpcs == []


def test_a_handshake_that_never_answers_times_out_by_name_and_makes_no_call(tmp_path: Path, bus: _BusServer) -> None:
    bus.stall = True
    result = _probe(_write_probe(tmp_path, TOPIC_PROBE), bus.url, "--timeout", "1")
    assert result.returncode == 1
    assert result.stderr.strip() == "probe failed: monitor handshake timed out after 1s", result.stderr
    assert bus.rpcs == []


def _without(key: str) -> dict[str, Any]:
    return {k: v for k, v in TOPIC_PROBE.items() if k != key}


@pytest.mark.parametrize(
    ("probe", "message"),
    [
        pytest.param(_without("topic"), "missing a string 'topic'", id="no-topic"),
        pytest.param(_without("within_s"), "'within_s' must be a positive number", id="no-within"),
        pytest.param({**TOPIC_PROBE, "within_s": 0}, "'within_s' must be a positive number", id="zero-within"),
        pytest.param({**TOPIC_PROBE, "within_s": True}, "'within_s' must be a positive number", id="bool-within"),
        pytest.param(_without("expect"), "missing an object 'expect'", id="no-expect"),
        pytest.param({**TOPIC_PROBE, "select": "tou"}, "'select' must be a list of keys and indexes", id="select-type"),
        pytest.param(
            {**TOPIC_PROBE, "select": [1.5]}, "'select' must be a list of keys and indexes", id="select-float"
        ),
        pytest.param({**TOPIC_PROBE, "prefix": "devices/"}, "unknown probe keys ['prefix']", id="old-prefix-key"),
        pytest.param({**TOPIC_PROBE, "trigger": []}, "'trigger' must be an object", id="trigger-type"),
        pytest.param(
            {**TOPIC_PROBE, "trigger": {"expect": {"type": "int"}}}, "'trigger.method'", id="trigger-no-method"
        ),
        pytest.param(
            {**TOPIC_PROBE, "trigger": {"method": "m"}}, "missing an object 'trigger.expect'", id="trigger-no-expect"
        ),
        pytest.param(
            {**TOPIC_PROBE, "trigger": {"method": "m", "expect": {"type": "int"}, "x": 1}},
            "unknown trigger keys ['x']",
            id="trigger-key",
        ),
    ],
)
def test_a_malformed_topic_probe_fails_before_any_connection(
    tmp_path: Path, bus: _BusServer, probe: Any, message: str
) -> None:
    result = _topic_probe(tmp_path, bus, probe)
    assert result.returncode == 1
    assert message in result.stderr, result.stderr
    assert bus.events == []


def test_the_interpreter_imports_only_the_standard_library() -> None:
    tree = ast.parse(INTERPRETER.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert imported, "the scan found no imports"
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
