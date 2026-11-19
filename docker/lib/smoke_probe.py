#!/usr/bin/env python3
"""Runs one smoke-probe.json against a running server (#96).

Usage: smoke_probe.py PROBE_FILE BASE_URL [--identity NAME] [--timeout SECS]

Standard library only: smoke.sh runs it with the host `python3`. Exits 0 when
the probe passes and 1 with a one-line reason on stderr otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
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


def call_rpc(base_url: str, probe: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(
        {
            "agent_id": probe["identity"],
            "method": probe["method"],
            "args": probe.get("args", []),
            "kwargs": probe.get("kwargs", {}),
        }
    ).encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/rpc/call",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # No proxy: the server is a local published port, and a proxy variable in
    # the environment would send the call somewhere that cannot reach it.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise ProbeError(f"rpc call returned HTTP {exc.code}") from exc
    except TimeoutError as exc:
        raise ProbeError(f"rpc call timed out after {timeout:g}s") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProbeError(f"rpc call could not reach {base_url}: {exc}") from exc
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
    if reply.get("success") is not True:
        raise ProbeError(f"{probe['identity']}.{probe['method']} failed: {reply.get('error', 'no error given')}")
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
