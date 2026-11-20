#!/usr/bin/env python3
"""Runs one smoke-probe.json against a running server (#96).

Usage: smoke_probe.py PROBE_FILE BASE_URL [--identity NAME] [--timeout SECS]

An rpc probe calls one RPC and checks its result. A topic probe opens a bus
monitor, optionally calls a trigger RPC, then waits for one publish by the
probe's identity on an exact topic and checks a value inside it.

Standard library only: smoke.sh runs it with the host `python3`. Exits 0 when
the probe passes and 1 with a one-line reason on stderr otherwise.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.client
import json
import os
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

# The server's own RPC timeout is 30 s, so a shorter client timeout would
# report a slow agent as a connection failure.
DEFAULT_TIMEOUT = 35.0

_TYPES: dict[str, type | tuple[type, ...]] = {
    "list": list,
    "dict": dict,
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
}
_SIZED = ("list", "dict", "str")
_PROBE_KEYS = {"identity", "kind", "method", "args", "kwargs", "expect"}
_TOPIC_KEYS = {"identity", "kind", "topic", "within_s", "trigger", "select", "expect"}
_TRIGGER_KEYS = {"method", "args", "kwargs", "expect"}
# RFC 6455 section 1.3: the accept key is derived from the client key with this GUID.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# A frame larger than this is a protocol failure, not something to buffer.
_MAX_FRAME_BYTES = 1 << 20
# The handshake reply headers are read up to this size.
_MAX_HANDSHAKE_BYTES = 16384
# How much of an error reply's body is quoted in the failure line.
_EXCERPT_BYTES = 200


class ProbeError(Exception):
    """A probe that failed or could not be run; the message is the reason."""


def load_probe(path: Path, identity: str | None) -> dict[str, Any]:
    try:
        probe = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProbeError(f"cannot read probe file {path}: {exc}") from exc
    except ValueError as exc:
        raise ProbeError(f"probe file {path} is not valid JSON: {exc}") from exc
    if not isinstance(probe, dict):
        raise ProbeError(f"probe file {path} must hold a JSON object")
    for key in ("identity", "kind"):
        if not isinstance(probe.get(key), str) or not probe[key]:
            raise ProbeError(f"probe file {path} is missing a string '{key}'")
    if identity is not None and probe["identity"] != identity:
        raise ProbeError(f"probe identity '{probe['identity']}' is not the expected '{identity}'")
    kind = probe["kind"]
    if kind == "topic":
        _check_topic_shape(probe, path)
        return probe
    if kind != "rpc":
        raise ProbeError(f"unknown probe kind '{kind}' (expected 'rpc' or 'topic')")
    _check_rpc_shape(probe, path)
    return probe


def _check_rpc_shape(probe: dict[str, Any], path: Path) -> None:
    unknown_keys = set(probe) - _PROBE_KEYS
    if unknown_keys:
        raise ProbeError(f"probe file {path}: unknown probe keys {sorted(unknown_keys)}")
    if not isinstance(probe.get("method"), str) or not probe["method"]:
        raise ProbeError(f"probe file {path} is missing a string 'method'")
    if not isinstance(probe.get("args", []), list):
        raise ProbeError(f"probe file {path}: 'args' must be a list")
    if not isinstance(probe.get("kwargs", {}), dict):
        raise ProbeError(f"probe file {path}: 'kwargs' must be an object")
    _check_expect_shape(probe.get("expect"), path, "expect")


def _check_expect_shape(expect: Any, path: Path, name: str) -> None:
    if not isinstance(expect, dict):
        raise ProbeError(f"probe file {path} is missing an object '{name}'")
    if expect.get("type") not in _TYPES:
        raise ProbeError(f"probe file {path}: {name}.type must be one of {sorted(_TYPES)}")
    unknown = set(expect) - {"type", "min_length"}
    if unknown:
        raise ProbeError(f"probe file {path}: unknown {name} keys {sorted(unknown)}")
    if "min_length" in expect:
        min_length = expect["min_length"]
        if isinstance(min_length, bool) or not isinstance(min_length, int) or min_length < 0:
            raise ProbeError(f"probe file {path}: {name}.min_length must be a non-negative integer")
        if expect["type"] not in _SIZED:
            raise ProbeError(f"probe file {path}: {name}.min_length needs type list, dict or str")


def _check_call_shape(call: dict[str, Any], path: Path, where: str) -> None:
    if not isinstance(call.get("method"), str) or not call["method"]:
        raise ProbeError(f"probe file {path} is missing a string '{where}method'")
    if not isinstance(call.get("args", []), list):
        raise ProbeError(f"probe file {path}: '{where}args' must be a list")
    if not isinstance(call.get("kwargs", {}), dict):
        raise ProbeError(f"probe file {path}: '{where}kwargs' must be an object")


def _check_topic_shape(probe: dict[str, Any], path: Path) -> None:
    unknown_keys = set(probe) - _TOPIC_KEYS
    if unknown_keys:
        raise ProbeError(f"probe file {path}: unknown probe keys {sorted(unknown_keys)}")
    if not isinstance(probe.get("topic"), str) or not probe["topic"]:
        raise ProbeError(f"probe file {path} is missing a string 'topic'")
    within = probe.get("within_s")
    if isinstance(within, bool) or not isinstance(within, (int, float)) or within <= 0:
        raise ProbeError(f"probe file {path}: 'within_s' must be a positive number")
    select = probe.get("select", [])
    if not isinstance(select, list) or any(
        isinstance(step, bool) or not isinstance(step, (str, int)) for step in select
    ):
        raise ProbeError(f"probe file {path}: 'select' must be a list of keys and indexes")
    _check_expect_shape(probe.get("expect"), path, "expect")
    if "trigger" in probe:
        trigger = probe["trigger"]
        if not isinstance(trigger, dict):
            raise ProbeError(f"probe file {path}: 'trigger' must be an object")
        unknown_trigger = set(trigger) - _TRIGGER_KEYS
        if unknown_trigger:
            raise ProbeError(f"probe file {path}: unknown trigger keys {sorted(unknown_trigger)}")
        _check_call_shape(trigger, path, "trigger.")
        _check_expect_shape(trigger.get("expect"), path, "trigger.expect")


def _split_base_url(base_url: str, suffix: str) -> tuple[str, int | None, str]:
    try:
        parts = urllib.parse.urlsplit(base_url)
        port = parts.port
    except ValueError as exc:
        raise ProbeError(f"base URL {base_url!r} is not http://HOST[:PORT]: {exc}") from exc
    if parts.scheme != "http" or not parts.hostname:
        raise ProbeError(f"base URL {base_url!r} is not http://HOST[:PORT]")
    return parts.hostname, port, parts.path.rstrip("/") + suffix


def _excerpt(response: http.client.HTTPResponse) -> str:
    """The start of an error reply's body on one line, or "" when it has none."""
    try:
        raw = response.read(_EXCERPT_BYTES)
    except (OSError, http.client.HTTPException):
        # Only extra context for a failure that is already being reported.
        return ""
    return " ".join(raw.decode("utf-8", errors="replace").split())


def call_rpc(base_url: str, probe: dict[str, Any], timeout: float) -> dict[str, Any]:
    host, port, path = _split_base_url(base_url, "/api/rpc/call")
    body = json.dumps(
        {
            "agent_id": probe["identity"],
            "method": probe["method"],
            "args": probe.get("args", []),
            "kwargs": probe.get("kwargs", {}),
        }
    ).encode()
    # http.client, not urllib: no proxy lookup (the server is a local published
    # port, and a proxy variable would send the call somewhere that cannot
    # reach it) and no redirect handling to refuse.
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    expired = threading.Event()
    # Held here because the connection drops its own reference to the socket
    # once it hands a closing reply to the response.
    held: list[socket.socket] = []

    def expire() -> None:
        # A socket timeout restarts on every read, so a reply dripped in small
        # pieces would outlive --timeout. Shutting the socket down ends
        # whichever read is blocked, and `expired` says why it ended.
        expired.set()
        for sock in held:
            # Already closed means the blocked read has ended either way.
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    deadline = threading.Timer(timeout, expire)
    deadline.start()
    try:
        conn.connect()
        if conn.sock is not None:
            held.append(conn.sock)
        if expired.is_set():
            raise TimeoutError
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        if 300 <= response.status < 400:
            raise ProbeError(
                f"rpc call was redirected (HTTP {response.status}) to {response.getheader('Location')}; not followed"
            )
        if not 200 <= response.status < 300:
            excerpt = _excerpt(response)
            raise ProbeError(f"rpc call returned HTTP {response.status}" + (f": {excerpt}" if excerpt else ""))
        raw = response.read()
    except (TimeoutError, http.client.HTTPException, OSError) as exc:
        if expired.is_set() or isinstance(exc, TimeoutError):
            raise ProbeError(f"rpc call timed out after {timeout:g}s") from exc
        reason = " ".join(f"{type(exc).__name__}: {exc}".split())
        if isinstance(exc, http.client.HTTPException):
            raise ProbeError(f"rpc call got a broken HTTP reply: {reason}") from exc
        raise ProbeError(f"rpc call could not reach {base_url}: {exc}") from exc
    finally:
        deadline.cancel()
        conn.close()
    try:
        reply = json.loads(raw)
    except ValueError as exc:
        raise ProbeError(f"rpc reply is not valid JSON: {exc}") from exc
    if not isinstance(reply, dict):
        raise ProbeError("rpc reply is not a JSON object")
    return reply


def check_result(probe: dict[str, Any], reply: dict[str, Any]) -> str:
    """Returns a one-line description of the passing result, or raises."""
    # A failed call is still HTTP 200, so success is the only signal.
    call = f"{probe['identity']}.{probe['method']}"
    success = reply.get("success")
    if not isinstance(success, bool):
        problem = (
            "has no 'success'" if "success" not in reply else f"'success' is {type(success).__name__}, not a boolean"
        )
        suffix = f" (error: {reply['error']})" if "error" in reply else ""
        raise ProbeError(f"{call} reply {problem}{suffix}")
    if not success:
        raise ProbeError(f"{call} failed: {reply.get('error') or 'no error given'}")
    if "result" not in reply:
        raise ProbeError(f"{probe['identity']}.{probe['method']} reply has no 'result'")
    return _check_value(reply["result"], probe["expect"], "result")


def _check_value(value: Any, expect: dict[str, Any], label: str) -> str:
    kind = expect["type"]
    # bool is an int in Python; a True must not satisfy an int or float expect.
    wrong = not isinstance(value, _TYPES[kind]) or (kind in ("int", "float") and isinstance(value, bool))
    if wrong:
        raise ProbeError(f"{label} is {type(value).__name__}, expected {kind}")
    if "min_length" in expect and len(value) < expect["min_length"]:
        raise ProbeError(f"{label} length {len(value)} is below min_length {expect['min_length']}")
    detail = f"length {len(value)}" if kind in _SIZED else "value ok"
    return f"{kind} {detail}"


class _WebSocket:
    """The client half of RFC 6455 that a bus monitor needs, and nothing more.

    Reads unmasked server text frames, joins fragments, answers a ping and
    sends a masked close; it never sends data frames.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = b""

    def _fill(self, count: int, deadline: float) -> bytes:
        while len(self.buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ProbeError("monitor connection closed by the server")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        # Client frames are always masked (RFC 6455 section 5.3).
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        if len(payload) < 126:
            header += bytes([0x80 | len(payload)])
        else:
            header += bytes([0x80 | 126]) + struct.pack("!H", len(payload))
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def read_message(self, deadline: float) -> bytes | None:
        """The next whole text message, or None for a binary one; raises TimeoutError at the deadline."""
        parts: list[bytes] = []
        first_opcode: int | None = None
        while True:
            head = self._fill(2, deadline)
            fin = bool(head[0] & 0x80)
            opcode = head[0] & 0x0F
            if head[0] & 0x70:
                raise ProbeError("monitor sent a frame with reserved bits set")
            if head[1] & 0x80:
                raise ProbeError("monitor sent a masked frame")
            length = head[1] & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._fill(2, deadline))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._fill(8, deadline))
            if length > _MAX_FRAME_BYTES:
                raise ProbeError(f"monitor sent a {length}-byte frame, over the {_MAX_FRAME_BYTES}-byte cap")
            payload = self._fill(length, deadline)
            if opcode == 0x9:
                self.send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                with contextlib.suppress(OSError):
                    self.send_frame(0x8, payload[:2])
                raise ProbeError("monitor closed by the server")
            if opcode in (0x1, 0x2):
                if first_opcode is not None:
                    raise ProbeError("monitor started a new message inside a fragmented one")
                first_opcode = opcode
            elif opcode == 0x0:
                if first_opcode is None:
                    raise ProbeError("monitor sent a continuation frame with nothing to continue")
            else:
                raise ProbeError(f"monitor sent an unknown frame opcode {opcode}")
            parts.append(payload)
            if sum(len(part) for part in parts) > _MAX_FRAME_BYTES:
                raise ProbeError(f"monitor message is over the {_MAX_FRAME_BYTES}-byte cap")
            if fin:
                return b"".join(parts) if first_opcode == 0x1 else None


def _open_monitor(base_url: str, monitor_id: str, timeout: float) -> _WebSocket:
    host, port, base_path = _split_base_url(base_url, "/monitor/")
    path = base_path + urllib.parse.quote(monitor_id, safe="")
    key = base64.b64encode(os.urandom(16)).decode()
    expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()  # noqa: S324 - RFC 6455 mandates SHA-1
    try:
        sock = socket.create_connection((host, port or 80), timeout=timeout)
    except OSError as exc:
        raise ProbeError(f"monitor could not reach {base_url}: {exc}") from exc
    try:
        host_header = host if port is None else f"{host}:{port}"
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode())
        deadline = time.monotonic() + timeout
        raw = b""
        while b"\r\n\r\n" not in raw:
            if len(raw) > _MAX_HANDSHAKE_BYTES:
                raise ProbeError("monitor handshake reply is too large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock.settimeout(remaining)
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
        head, _, rest = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status_parts = lines[0].split(" ", 2)
        if len(status_parts) < 2 or not status_parts[1].isdigit():
            raise ProbeError(f"monitor handshake got a broken reply: {lines[0][:_EXCERPT_BYTES]!r}")
        status = int(status_parts[1])
        if status != 101:
            # The denial's body may trail its headers in a later segment.
            while len(rest) < _EXCERPT_BYTES and (remaining := deadline - time.monotonic()) > 0:
                sock.settimeout(remaining)
                more = sock.recv(4096)
                if not more:
                    break
                rest += more
            body = " ".join(rest.decode("utf-8", errors="replace").split())[:_EXCERPT_BYTES]
            raise ProbeError(f"monitor {monitor_id} was refused (HTTP {status})" + (f": {body}" if body else ""))
        headers = {
            name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])
        }
        if headers.get("sec-websocket-accept") != expected:
            raise ProbeError("monitor handshake Sec-WebSocket-Accept does not match the key sent")
    except TimeoutError as exc:
        sock.close()
        raise ProbeError(f"monitor handshake timed out after {timeout:g}s") from exc
    except (ProbeError, OSError):
        sock.close()
        raise
    ws = _WebSocket(sock)
    ws.buffer = rest
    return ws


def _select(message: Any, select: list[Any]) -> Any:
    value = message
    for position, step in enumerate(select):
        try:
            if isinstance(step, str):
                if not isinstance(value, dict) or step not in value:
                    raise KeyError(step)
                value = value[step]
            else:
                if not isinstance(value, list) or not -len(value) <= step < len(value):
                    raise IndexError(step)
                value = value[step]
        except (KeyError, IndexError) as exc:
            raise ProbeError(
                f"select path {json.dumps(select)} not found in the message: no step {json.dumps(step)} "
                f"(step {position + 1}, in a {type(value).__name__})"
            ) from exc
    return value


def run_topic(probe: dict[str, Any], base_url: str, timeout: float) -> str:
    identity = probe["identity"]
    topic = probe["topic"]
    monitor_id = f"smoke-{identity}-{os.getpid()}-{secrets.token_hex(4)}"
    ws = _open_monitor(base_url, monitor_id, timeout)
    try:
        trigger_note = ""
        trigger = probe.get("trigger")
        if trigger is not None:
            call = {"identity": identity, **trigger}
            trigger_note = f" after {call['method']} " + check_result(call, call_rpc(base_url, call, timeout))
        deadline = time.monotonic() + probe["within_s"]
        other_senders: list[str] = []
        while True:
            try:
                raw = ws.read_message(deadline)
            except TimeoutError:
                reason = f"no frame on {topic} from {identity} within {probe['within_s']:g}s"
                if other_senders:
                    reason += f" (topic published by {', '.join(sorted(set(other_senders)))})"
                raise ProbeError(reason) from None
            except OSError as exc:
                raise ProbeError(f"monitor connection failed: {exc}") from exc
            if raw is None:
                continue
            try:
                frame = json.loads(raw)
            except ValueError:
                # Not a frame this probe can match; the bus only sends JSON objects.
                continue
            if not isinstance(frame, dict) or frame.get("type") != "pubsub_message" or frame.get("topic") != topic:
                continue
            if frame.get("sender") != identity:
                other_senders.append(str(frame.get("sender")))
                continue
            detail = _check_value(
                _select(frame.get("message"), probe.get("select", [])), probe["expect"], "message value"
            )
            return f"{identity} published {topic}{trigger_note}: {detail}"
    finally:
        with contextlib.suppress(OSError, ProbeError):
            ws.send_frame(0x8, struct.pack("!H", 1000))
        ws.sock.close()


def run(path: Path, base_url: str, identity: str | None, timeout: float) -> str:
    probe = load_probe(path, identity)
    if probe["kind"] == "topic":
        return run_topic(probe, base_url, timeout)
    reply = call_rpc(base_url, probe, timeout)
    return f"{probe['identity']} {probe['method']}: {check_result(probe, reply)}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("probe_file", type=Path)
    parser.add_argument("base_url")
    parser.add_argument("--identity", help="the identity the probe file must name")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    try:
        print(f"probe ok: {run(args.probe_file, args.base_url, args.identity, args.timeout)}")
    except ProbeError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
