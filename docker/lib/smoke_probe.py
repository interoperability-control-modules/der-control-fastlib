#!/usr/bin/env python3
"""Runs one smoke-probe.json against a running server (#96).

Usage: smoke_probe.py PROBE_FILE BASE_URL [--identity NAME] [--timeout SECS]

Standard library only: smoke.sh runs it with the host `python3`. Exits 0 when
the probe passes and 1 with a one-line reason on stderr otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import socket
import sys
import threading
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
        raise ProbeError("topic probes are not supported yet")
    if kind != "rpc":
        raise ProbeError(f"unknown probe kind '{kind}' (expected 'rpc')")
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
    expect = probe.get("expect")
    if not isinstance(expect, dict):
        raise ProbeError(f"probe file {path} is missing an object 'expect'")
    if expect.get("type") not in _TYPES:
        raise ProbeError(f"probe file {path}: expect.type must be one of {sorted(_TYPES)}")
    unknown = set(expect) - {"type", "min_length"}
    if unknown:
        raise ProbeError(f"probe file {path}: unknown expect keys {sorted(unknown)}")
    if "min_length" in expect:
        min_length = expect["min_length"]
        if isinstance(min_length, bool) or not isinstance(min_length, int) or min_length < 0:
            raise ProbeError(f"probe file {path}: expect.min_length must be a non-negative integer")
        if expect["type"] not in _SIZED:
            raise ProbeError(f"probe file {path}: expect.min_length needs type list, dict or str")


def _split_base_url(base_url: str) -> tuple[str, int | None, str]:
    try:
        parts = urllib.parse.urlsplit(base_url)
        port = parts.port
    except ValueError as exc:
        raise ProbeError(f"base URL {base_url!r} is not http://HOST[:PORT]: {exc}") from exc
    if parts.scheme != "http" or not parts.hostname:
        raise ProbeError(f"base URL {base_url!r} is not http://HOST[:PORT]")
    return parts.hostname, port, parts.path.rstrip("/") + "/api/rpc/call"


def _excerpt(response: http.client.HTTPResponse) -> str:
    """The start of an error reply's body on one line, or "" when it has none."""
    try:
        raw = response.read(_EXCERPT_BYTES)
    except (OSError, http.client.HTTPException):
        # Only extra context for a failure that is already being reported.
        return ""
    return " ".join(raw.decode("utf-8", errors="replace").split())


def call_rpc(base_url: str, probe: dict[str, Any], timeout: float) -> dict[str, Any]:
    host, port, path = _split_base_url(base_url)
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
    result = reply["result"]
    expect = probe["expect"]
    kind = expect["type"]
    # bool is an int in Python; a True must not satisfy an int or float expect.
    wrong = not isinstance(result, _TYPES[kind]) or (kind in ("int", "float") and isinstance(result, bool))
    if wrong:
        raise ProbeError(f"result is {type(result).__name__}, expected {kind}")
    if "min_length" in expect and len(result) < expect["min_length"]:
        raise ProbeError(f"result length {len(result)} is below min_length {expect['min_length']}")
    detail = f"length {len(result)}" if kind in _SIZED else "value ok"
    return f"{kind} {detail}"


def run(path: Path, base_url: str, identity: str | None, timeout: float) -> str:
    probe = load_probe(path, identity)
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
