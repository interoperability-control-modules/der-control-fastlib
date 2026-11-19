"""docker/lib/smoke_probe.py against a stdlib HTTP server standing in for /api/rpc/call (#96).

The interpreter runs as smoke.sh runs it, as a subprocess, so the exit status
and the lines on stdout and stderr are what is asserted.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import re
import shutil
import socket
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


def test_topic_probes_are_refused_by_name_and_make_no_call(tmp_path: Path, rpc_server: _RpcServer) -> None:
    # A topic probe reads WS /monitor/{id}; the standard library has no
    # WebSocket client, and neither current agent publishes.
    topic = {"identity": "demo.agent", "kind": "topic", "prefix": "demo/", "within_s": 5}
    result = _probe(_write_probe(tmp_path, topic), rpc_server.url)
    assert result.returncode == 1
    assert result.stderr.strip() == "probe failed: topic probes are not supported yet"
    assert rpc_server.requests == []


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
