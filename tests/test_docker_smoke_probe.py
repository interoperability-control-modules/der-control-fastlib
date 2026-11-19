"""docker/lib/smoke_probe.py against a stdlib HTTP server standing in for /api/rpc/call (#96).

The interpreter runs as smoke.sh runs it, as a subprocess, so the exit status
and the lines on stdout and stderr are what is asserted.
"""

from __future__ import annotations

import http.server
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
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
        self.delay = threading.Event()
        self.requests: list[dict[str, Any]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append({"path": self.path, "body": json.loads(self.rfile.read(length))})
                if outer.delay.is_set():
                    # Longer than any client timeout in these tests.
                    threading.Event().wait(2)
                body = outer.raw if outer.raw is not None else json.dumps(outer.reply).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def rpc_server() -> Iterator[_RpcServer]:
    server = _RpcServer()
    server.thread.start()
    try:
        yield server
    finally:
        server.server.shutdown()
        server.thread.join(timeout=5)
        server.server.server_close()


def _write_probe(tmp_path: Path, probe: Any) -> Path:
    path = tmp_path / "smoke-probe.json"
    path.write_text(probe if isinstance(probe, str) else json.dumps(probe), encoding="utf-8")
    return path


def _probe(probe_file: Path, url: str, *extra: str, python: str = sys.executable) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [python, str(INTERPRETER), str(probe_file), url, *extra],
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


def test_the_host_python3_runs_it_too(tmp_path: Path, rpc_server: _RpcServer) -> None:
    host_python = shutil.which("python3")
    assert host_python is not None
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url, python=host_python)
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


@pytest.mark.parametrize("success", [False, "true", 1, None])
def test_success_must_be_exactly_true_and_the_agent_error_is_printed(
    tmp_path: Path, rpc_server: _RpcServer, success: Any
) -> None:
    # A failure is still HTTP 200; a result list that would satisfy expect
    # must not rescue it.
    rpc_server.reply = {"success": success, "error": "Method nope not found or not exported", "result": ["x", "y"]}
    result = _probe(_write_probe(tmp_path, RPC_PROBE), rpc_server.url)
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "probe failed: demo.agent.list_things failed: Method nope not found or not exported"
    )


def test_a_bool_does_not_satisfy_an_int_expect(tmp_path: Path, rpc_server: _RpcServer) -> None:
    rpc_server.reply = {"success": True, "result": True}
    probe = {**RPC_PROBE, "expect": {"type": "int"}}
    result = _probe(_write_probe(tmp_path, probe), rpc_server.url)
    assert result.returncode == 1
    assert "result is bool, expected int" in result.stderr


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
