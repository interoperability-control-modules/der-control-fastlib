"""`make stack-smoke` / docker/smoke.sh over the server and every AGENT_DIRS agent (#95).

smoke.sh runs from a copy of the repo's Makefile and docker/ tree in a temp
directory, with a stub `docker` first on PATH and DOCKER_HOST at a missing
socket, so nothing here reaches a daemon. Each agent's build.sh is replaced
by a logging stub in the copy, because the real one needs a sibling checkout
of the agent. `docker compose ... config` is the one call forwarded to the
real binary: it needs no daemon, and the name-disjointness checks are about
what compose resolves, not about what the stub is told.
"""

from __future__ import annotations

import fcntl
import http.server
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKER_DIR = REPO_ROOT / "docker"

AGENTS = ("interoperability-service", "realtime-control-agent")
IDENTITIES = {"interoperability-service": "platform.presentation", "realtime-control-agent": "der.rtc"}
RESTART_AGENT = "realtime-control-agent"
RESTART_CONTAINER = f"derhost-smoke-{RESTART_AGENT}"
ALL_SMOKE_PROJECTS = {"derhost-smoke", *(f"derhost-smoke-{a}" for a in AGENTS)}

_MAKE_RECURSION_KEYS = ("MAKELEVEL", "MAKEFLAGS", "MFLAGS")
_SMOKE_ENV_KEYS = (
    "DERHOST_PUBLISH_HOST",
    "DERHOST_SMOKE_AGENT_DIRS",
    "DER_AGENT_SRC",
    "AGENT_DIRS",
    "COMPOSE_PROJECT_NAME",
    "DERHOST_SMOKE_LOCK",
)

_DOCKER_STUB = """#!/usr/bin/env bash
set -euo pipefail
{
    printf 'DERHOST_PUBLISH_HOST=%s DER_AGENT_SRC=%s ARGS:' "${DERHOST_PUBLISH_HOST-unset}" "${DER_AGENT_SRC-unset}"
    printf ' %q' "$@"
    printf '\\n'
} >> "$STUB_LOG"

downs_logged() {
    /usr/bin/grep -q ' down ' "$STUB_LOG"
}

# A listing returns one id when its filter names the leftover project, so a
# test can plant a leftover for exactly one project; the linger variant only
# shows it once a `down` has been logged, to model a teardown that leaves
# an object behind.
listing() {
    if [ -n "${STUB_LIST_EXIT-}" ]; then
        return "$STUB_LIST_EXIT"
    fi
    if [ -n "${STUB_LIST_EXIT_AFTER_DOWN-}" ] && downs_logged; then
        return 1
    fi
    if [ -n "${STUB_LEFTOVER_PROJECT-}" ] && [[ " $* " == *"com.docker.compose.project=${STUB_LEFTOVER_PROJECT} "* ]]; then
        echo deadbeef0001
    elif [ -n "${STUB_LINGER_PROJECT-}" ] && downs_logged && [[ " $* " == *"com.docker.compose.project=${STUB_LINGER_PROJECT} "* ]]; then
        echo deadbeef0002
    elif [ -n "${STUB_LINGER_NAME-}" ] && downs_logged && [[ " $* " == *" name=derhost-smoke "* ]]; then
        echo deadbeef0002
    fi
    return 0
}

if [ "${1-}" = "compose" ]; then
    shift
    case " $* " in
        *" config "*)
            # Signals smoke.sh during its first config step, then outlasts any
            # test bound, so only a prompt reaction ends the run in time.
            if [ -n "${STUB_SIGNAL_CONFIG-}" ]; then
                kill "-$STUB_SIGNAL_CONFIG" "$(cat "$STUB_SMOKE_PIDFILE")"
                exec sleep 25
            fi
            exec "$STUB_REAL_DOCKER" compose "$@" ;;
        *" port "*) printf '%s\\n' "$STUB_PORT_OUTPUT" ;;
        *" up "*) exit "${STUB_UP_EXIT:-0}" ;;
        *" down "*)
            # Signals the running smoke.sh once, at the first logged `down`,
            # then returns normally: a trap runs once the foreground step ends.
            if [ -n "${STUB_SIGNAL_FIRST_DOWN-}" ] && [ "$(/usr/bin/grep -c ' down ' "$STUB_LOG")" -eq 1 ]; then
                kill "-$STUB_SIGNAL_FIRST_DOWN" "$(cat "$STUB_SMOKE_PIDFILE")"
            fi
            exit "${STUB_DOWN_EXIT:-0}" ;;
        *" build "*)
            # A server build that signals smoke.sh and then runs far longer than
            # any test waits, so only a prompt reaction ends the run in time.
            if [ -n "${STUB_SIGNAL_SERVER_BUILD-}" ]; then
                kill "-$STUB_SIGNAL_SERVER_BUILD" "$(cat "$STUB_SMOKE_PIDFILE")"
                exec sleep 25
            fi
            exit 0 ;;
        *) echo "stub: unexpected docker compose invocation: $*" >&2; exit 1 ;;
    esac
elif [ "${1-}" = "ps" ] || { [ "${1-}" = "network" ] && [ "${2-}" = "ls" ]; } || { [ "${1-}" = "volume" ] && [ "${2-}" = "ls" ]; }; then
    listing "$@"
elif [ "${1-}" = "image" ] && [ "${2-}" = "ls" ]; then
    if [ -n "${STUB_IMAGE_LINGER-}" ] && downs_logged; then echo deadbeef0003; fi
elif [ "${1-}" = "image" ] && [ "${2-}" = "inspect" ]; then
    exit 0
elif [ "${1-}" = "image" ] && [ "${2-}" = "rm" ]; then
    exit "${STUB_IMAGE_RM_EXIT:-0}"
elif [ "${1-}" = "container" ] && [ "${2-}" = "inspect" ]; then
    # StartedAt moves once per logged `start`, as a real container's does,
    # unless a test pins it to model a restart that never happened.
    if [ -n "${STUB_STARTED_AT_FIXED-}" ]; then
        echo T0
    else
        printf 'T%s\\n' "$(/usr/bin/grep -c 'ARGS: start ' "$STUB_LOG" || true)"
    fi
elif [ "${1-}" = "tag" ] || [ "${1-}" = "stop" ] || [ "${1-}" = "start" ] || [ "${1-}" = "restart" ]; then
    exit 0
else
    echo "stub: unexpected docker invocation: $*" >&2
    exit 1
fi
"""

_BUILD_STUB = """#!/usr/bin/env bash
set -euo pipefail
dir="$(basename "$(dirname "$0")")"
printf 'DERHOST_PUBLISH_HOST=%s DER_AGENT_SRC=%s DERHOST_BUILD_OVERLAY=%s ARGS: build.sh %s\\n' "${DERHOST_PUBLISH_HOST-unset}" "${DER_AGENT_SRC-unset}" "${DERHOST_BUILD_OVERLAY-unset}" "$dir" >> "$STUB_LOG"
if [ "${STUB_BUILD_FAIL_DIR-}" = "$dir" ]; then
    exit 1
fi
# Signals the smoke.sh that is running this build, then returns normally: a
# trap runs once the foreground build ends, so the run must not resume.
if [ "${STUB_SIGNAL_DIR-}" = "$dir" ]; then
    kill "-$STUB_SIGNAL" "$(cat "$STUB_SMOKE_PIDFILE")"
    # A build that keeps running after it signals, to show the run does not
    # wait for it.
    if [ -n "${STUB_BUILD_SLEEP-}" ]; then
        exec sleep 25
    fi
fi
"""


def _stub_calls(log_text: str) -> list[list[str]]:
    calls = []
    for line in log_text.splitlines():
        if line.strip():
            _, _, rest = line.partition("ARGS:")
            calls.append(shlex.split(rest))
    return calls


def _compose_calls(calls: list[list[str]], subcommand: str) -> list[list[str]]:
    return [c for c in calls if c and c[0] == "compose" and subcommand in c]


def _flag_value(call: list[str], flag: str) -> str | None:
    return call[call.index(flag) + 1] if flag in call else None


def _index_of(calls: list[list[str]], predicate: Any) -> int:
    for i, call in enumerate(calls):
        if predicate(call):
            return i
    raise AssertionError(f"no matching call in {calls}")


class _ConnectionsServer:
    """Serves /connections from the stub log: every identity, except that the
    restarted agent is absent while its container's last logged event is a stop.
    POST /api/rpc/call records the request and answers from `rpc_replies` by
    agent_id, with a passing non-empty list for an identity not listed there."""

    def __init__(self, log_path: Path, always_connected: bool = False, never_returns: bool = False) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                body = json.dumps({"connections": {i: {} for i in outer.connected()}}).encode()
                status = 500 if outer.fail_after_stop and outer.restart_container_stopped() else 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                call = json.loads(self.rfile.read(length))
                call["path"] = self.path
                call["stops_so_far"] = len(outer._restart_events("stop"))
                outer.rpc_calls.append(call)
                reply = outer.rpc_replies.get(call["agent_id"], {"success": True, "result": [{"stub": True}]})
                body = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return

        self.log_path = log_path
        self.rpc_calls: list[dict[str, Any]] = []
        self.rpc_replies: dict[str, dict[str, Any]] = {}
        self.always_connected = always_connected
        self.never_returns = never_returns
        # Identities the server never reports, and a server that answers 500
        # while the restarted agent is stopped (an error, not "MISSING").
        self.hidden: set[str] = set()
        self.fail_after_stop = False
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _restart_events(self, only: str | None = None) -> list[str]:
        return [
            c[0]
            for c in _stub_calls(self.log_path.read_text())
            if len(c) > 1 and c[0] in ("stop", "start") and c[1] == RESTART_CONTAINER and only in (None, c[0])
        ]

    def restart_container_stopped(self) -> bool:
        events = self._restart_events()
        stopped = bool(events) and events[-1] == "stop"
        return stopped or (self.never_returns and "stop" in events)

    def connected(self) -> list[str]:
        identities = [i for i in IDENTITIES.values() if i not in self.hidden]
        if self.restart_container_stopped() and not self.always_connected and IDENTITIES[RESTART_AGENT] in identities:
            identities.remove(IDENTITIES[RESTART_AGENT])
        return identities

    @property
    def address(self) -> str:
        host, port = self.server.server_address[:2]
        return f"{host!s}:{port}"


@pytest.fixture
def smoke_tree(tmp_path: Path) -> Path:
    """A copy of Makefile + docker/ whose agent build.sh files only log."""
    tree = tmp_path / "repo"
    tree.mkdir()
    shutil.copy2(REPO_ROOT / "Makefile", tree / "Makefile")
    shutil.copytree(DOCKER_DIR, tree / "docker")
    for agent in AGENTS:
        build = tree / "docker" / agent / "build.sh"
        build.write_text(_BUILD_STUB)
        build.chmod(0o755)
    return tree


@pytest.fixture
def stub_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(_DOCKER_STUB)
    stub.chmod(0o755)
    real_docker = shutil.which("docker")
    assert real_docker is not None, "the real docker CLI must be on PATH to forward `compose ... config`"
    log = tmp_path / "stub.log"
    log.write_text("")
    env = os.environ.copy()
    for key in (*_SMOKE_ENV_KEYS, *_MAKE_RECURSION_KEYS):
        env.pop(key, None)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "DOCKER_HOST": "unix:///nonexistent/derhost-smoke-test.sock",
            "STUB_LOG": str(log),
            "STUB_REAL_DOCKER": real_docker,
            "STUB_PORT_OUTPUT": "127.0.0.1:1",
            "DERHOST_SMOKE_LOCK": str(tmp_path / "smoke.lock"),
            "DERHOST_SMOKE_STEP_TIMEOUT": "30",
            "DERHOST_SMOKE_POLL_SECS": "0.1",
            "DERHOST_SMOKE_RESTART_WINDOW": "3",
            "DERHOST_SMOKE_WAIT_TIMEOUT": "5",
        }
    )
    return env


@pytest.fixture
def connections(stub_env: dict[str, str]) -> Iterator[_ConnectionsServer]:
    server = _ConnectionsServer(Path(stub_env["STUB_LOG"]))
    server.thread.start()
    stub_env["STUB_PORT_OUTPUT"] = server.address
    try:
        yield server
    finally:
        server.server.shutdown()
        server.thread.join(timeout=5)
        server.server.server_close()


def _run(cmd: list[str], tree: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, timeout=60, check=False)


def _make_smoke(tree: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["make", "stack-smoke", *args], tree, env)


def _log_calls(env: dict[str, str]) -> list[list[str]]:
    return _stub_calls(Path(env["STUB_LOG"]).read_text())


# --- the happy path: every listed agent gets its own smoke project. ---------


def test_every_listed_agent_runs_in_its_own_smoke_project(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _log_calls(stub_env)
    assert calls, "stub log is empty: the run never reached docker"

    compose_calls = [c for c in calls if c and c[0] == "compose"]
    # Not vacuous: exactly the server project plus one per listed directory.
    assert {_flag_value(c, "-p") for c in compose_calls} == ALL_SMOKE_PROJECTS, compose_calls

    for agent in AGENTS:
        project = f"derhost-smoke-{agent}"
        mine = [c for c in compose_calls if _flag_value(c, "-p") == project]
        assert mine, project
        for call in mine:
            files = [call[i + 1] for i, tok in enumerate(call) if tok == "-f"]
            assert [Path(f).name for f in files] == ["docker-compose.yml", "compose.smoke.yml"], call
            assert all(Path(f).parent.name == agent for f in files), call
        assert len(_compose_calls(mine, "up")) == 1, mine
        assert "--no-build" in _compose_calls(mine, "up")[0], mine


def test_run_order_is_server_then_agents_then_agents_down_before_server(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _log_calls(stub_env)

    def compose(sub: str, project: str) -> Any:
        return lambda c: c[0] == "compose" and sub in c and _flag_value(c, "-p") == project

    server_up = _index_of(calls, compose("up", "derhost-smoke"))
    server_down = _index_of(calls, compose("down", "derhost-smoke"))
    for agent in AGENTS:
        project = f"derhost-smoke-{agent}"
        build = _index_of(calls, lambda c, a=agent: c[:2] == ["build.sh", a])
        up = _index_of(calls, compose("up", project))
        down = _index_of(calls, compose("down", project))
        assert server_up < build < up < down < server_down, (agent, server_up, build, up, down, server_down)

    for call in _compose_calls(calls, "down"):
        assert "-v" in call and _flag_value(call, "--rmi") == "local", call


def test_agents_build_and_start_without_the_developers_der_agent_src(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = Path(stub_env["STUB_LOG"]).read_text().splitlines()
    build_lines = [ln for ln in lines if "ARGS: build.sh" in ln]
    assert len(build_lines) == len(AGENTS), lines
    assert all("DER_AGENT_SRC=unset" in ln for ln in build_lines), build_lines
    agent_compose = [
        ln for ln in lines if "derhost-smoke-" in ln and "ARGS: compose" in ln and "--project-directory" in ln
    ]
    assert len(agent_compose) >= len(AGENTS), lines
    assert all("DER_AGENT_SRC=/dev/null/no-build" in ln for ln in agent_compose), agent_compose


def test_restart_shows_the_identity_absent_then_connected_with_a_new_started_at(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _log_calls(stub_env)
    stop = _index_of(calls, lambda c: c[:2] == ["stop", RESTART_CONTAINER])
    start = _index_of(calls, lambda c: c[:2] == ["start", RESTART_CONTAINER])
    assert stop < start
    assert "der.rtc: MISSING" in result.stdout, result.stdout
    assert "der.rtc: connected" in result.stdout.split("der.rtc: MISSING", 1)[1], result.stdout
    assert "StartedAt changed" in result.stdout, result.stdout
    assert "smoke passed" in result.stdout


def test_restart_fails_when_the_identity_never_goes_absent(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    # Control: the absent check can fail. A server that keeps reporting the
    # identity after the container stopped makes the restart step meaningless.
    connections.always_connected = True
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "still connected" in result.stderr, result.stderr
    assert "smoke passed" not in result.stdout


def test_restart_fails_when_the_identity_does_not_return_within_the_window(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    connections.never_returns = True
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "did not reconnect" in result.stderr, result.stderr
    assert "smoke passed" not in result.stdout
    # The failure path still tears everything down, agents before the server.
    calls = _log_calls(stub_env)
    assert _index_of(
        calls, lambda c: c[0] == "compose" and "down" in c and _flag_value(c, "-p") == f"derhost-smoke-{RESTART_AGENT}"
    ) < _index_of(calls, lambda c: c[0] == "compose" and "down" in c and _flag_value(c, "-p") == "derhost-smoke")


# --- job probes: one per listed agent, after connect and before the restart. ---


def _probe_file(tree: Path, agent: str) -> dict[str, Any]:
    return json.loads((tree / "docker" / agent / "smoke-probe.json").read_text(encoding="utf-8"))


def test_each_listed_agent_is_probed_once_with_its_own_probe_file_before_the_restart(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [(c["agent_id"], c["method"], c["args"]) for c in connections.rpc_calls] == [
        (
            _probe_file(smoke_tree, a)["identity"],
            _probe_file(smoke_tree, a)["method"],
            _probe_file(smoke_tree, a)["args"],
        )
        for a in AGENTS
    ]
    assert {c["path"] for c in connections.rpc_calls} == {"/api/rpc/call"}
    # No probe saw the restarted container stopped, so all ran before the restart.
    assert [c["stops_so_far"] for c in connections.rpc_calls] == [0, 0]
    assert "[smoke] probe interoperability-service" in result.stdout
    assert "probe ok: platform.presentation lookup_transform: list length 1" in result.stdout, result.stdout
    assert "probe ok: der.rtc list_modes: list length 1" in result.stdout, result.stdout
    assert "none yet" not in result.stdout
    assert "smoke passed" in result.stdout


def test_an_empty_agent_list_makes_no_probe_call(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    stub_env["DERHOST_SMOKE_AGENT_DIRS"] = ""
    result = _run(["bash", str(smoke_tree / "docker" / "smoke.sh")], smoke_tree, stub_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert connections.rpc_calls == []


def _down_indexes(calls: list[list[str]]) -> list[int]:
    return [i for i, c in enumerate(calls) if c[0] == "compose" and "down" in c]


@pytest.mark.parametrize("failing", AGENTS)
def test_a_failing_probe_fails_the_run_prints_the_error_and_tears_down(
    failing: str, smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    identity = IDENTITIES[failing]
    connections.rpc_replies[identity] = {"success": False, "error": "boom from the agent"}
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert f"job probe failed for {failing} (exit status 1)" in result.stderr, result.stderr
    assert (
        f"probe failed: {identity}.{_probe_file(smoke_tree, failing)['method']} failed: boom from the agent"
        in result.stderr
    )
    assert "smoke passed" not in result.stdout
    calls = _log_calls(stub_env)
    # The restart never starts, and every project is still taken down.
    assert not [c for c in calls if c[0] in ("stop", "start")]
    assert len(_down_indexes(calls)) == len(AGENTS) + 1
    assert "teardown: 0 smoke objects remain" in result.stdout


def test_a_probe_that_misses_its_expect_fails_the_run(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    connections.rpc_replies[IDENTITIES[RESTART_AGENT]] = {"success": True, "result": []}
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "below min_length 1" in result.stderr, result.stderr
    assert f"job probe failed for {RESTART_AGENT}" in result.stderr
    assert "smoke passed" not in result.stdout


@pytest.mark.parametrize("missing", AGENTS)
def test_a_listed_dir_without_a_probe_file_is_refused_before_any_docker_call(
    missing: str, smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    (smoke_tree / "docker" / missing / "smoke-probe.json").unlink()
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert f"docker/{missing}/smoke-probe.json is missing" in result.stderr, result.stderr
    assert "smoke passed" not in result.stdout
    # Checked with the rest of the agent directory's files: nothing was built,
    # started or torn down, because nothing had been created yet.
    assert _log_calls(stub_env) == []
    assert "teardown" not in result.stdout


def test_a_probe_whose_identity_differs_from_the_dockerfiles_fails(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    path = smoke_tree / "docker" / AGENTS[0] / "smoke-probe.json"
    probe = json.loads(path.read_text(encoding="utf-8"))
    probe["identity"] = "someone.else"
    path.write_text(json.dumps(probe), encoding="utf-8")
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "probe identity 'someone.else' is not the expected 'platform.presentation'" in result.stderr, result.stderr
    assert connections.rpc_calls == []


# --- refusals happen before anything is built. ------------------------------


@pytest.mark.parametrize("project", sorted(ALL_SMOKE_PROJECTS))
def test_a_leftover_smoke_label_refuses_before_any_build(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer, project: str
) -> None:
    stub_env["STUB_LEFTOVER_PROJECT"] = project
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "[smoke] refusing" in result.stdout, result.stdout
    calls = _log_calls(stub_env)
    assert calls, "the refusal read nothing from docker"
    assert not [c for c in calls if c[0] == "build.sh"], calls
    for sub in ("build", "up", "down"):
        assert not _compose_calls(calls, sub), (sub, calls)


# --- one smoke run per host at a time (#85). ---------------------------------


def test_a_held_smoke_lock_refuses_before_any_docker_call(smoke_tree: Path, stub_env: dict[str, str]) -> None:
    with Path(stub_env["DERHOST_SMOKE_LOCK"]).open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "another smoke run" in result.stdout + result.stderr, result.stdout + result.stderr
    assert Path(stub_env["STUB_LOG"]).read_text() == ""


def _lock_is_free(env: dict[str, str]) -> bool:
    with Path(env["DERHOST_SMOKE_LOCK"]).open("a") as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def test_the_lock_probe_sees_a_held_lock(stub_env: dict[str, str]) -> None:
    # Control for the release tests below: the held-lock test above shows the
    # lock is real, and this shows the probe can see one, so "free" is not vacuous.
    with Path(stub_env["DERHOST_SMOKE_LOCK"]).open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not _lock_is_free(stub_env)
    assert _lock_is_free(stub_env)


def test_the_smoke_lock_is_released_when_a_run_is_ended_by_a_signal(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    stub_env["STUB_SIGNAL_SERVER_BUILD"] = "TERM"
    ended_by_signal = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS))
    assert ended_by_signal.returncode == 143, ended_by_signal.stdout + ended_by_signal.stderr
    assert _lock_is_free(stub_env)


def test_the_smoke_lock_is_released_when_a_run_refuses(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    stub_env["STUB_LEFTOVER_PROJECT"] = "derhost-smoke"
    refused = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS))
    assert "[smoke] refusing" in refused.stdout, refused.stdout
    assert _lock_is_free(stub_env)


def test_the_default_smoke_lock_is_one_fixed_path_whatever_xdg_runtime_dir_says(
    smoke_tree: Path, stub_env: dict[str, str], tmp_path: Path
) -> None:
    # Read the computed path by sourcing the script, so nothing is opened under /tmp.
    env = {k: v for k, v in stub_env.items() if k != "DERHOST_SMOKE_LOCK"}
    for xdg in (None, str(tmp_path / "run-user")):
        run_env = dict(env)
        if xdg is not None:
            run_env["XDG_RUNTIME_DIR"] = xdg
        else:
            run_env.pop("XDG_RUNTIME_DIR", None)
        result = _run(
            ["bash", "-c", 'source "$1"; smoke_lock_path', "sourcing", str(smoke_tree / "docker" / "smoke.sh")],
            smoke_tree,
            run_env,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "/tmp/derhost-smoke.lock", (xdg, result.stdout)
    override = _run(
        ["bash", "-c", 'source "$1"; smoke_lock_path', "sourcing", str(smoke_tree / "docker" / "smoke.sh")],
        smoke_tree,
        stub_env,
    )
    assert override.stdout == stub_env["DERHOST_SMOKE_LOCK"]


def test_a_new_smoke_lock_file_is_created_with_the_callers_umask_not_world_writable(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    lock = Path(stub_env["DERHOST_SMOKE_LOCK"])
    assert not lock.exists()
    old_umask = os.umask(0o022)
    try:
        stub_env["STUB_LEFTOVER_PROJECT"] = "derhost-smoke"
        _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS))
    finally:
        os.umask(old_umask)
    mode = stat.S_IMODE(lock.stat().st_mode)
    assert mode == 0o644, oct(mode)
    assert not mode & stat.S_IWOTH


def _assert_lock_path_refused(
    smoke_tree: Path, stub_env: dict[str, str], result: subprocess.CompletedProcess[str]
) -> None:
    assert result.returncode != 0, result.stdout + result.stderr
    assert "must be a plain file" in result.stdout + result.stderr, result.stdout + result.stderr
    assert Path(stub_env["STUB_LOG"]).read_text() == ""


def test_a_smoke_lock_path_that_is_a_symlink_is_refused_before_any_docker_call(
    smoke_tree: Path, stub_env: dict[str, str], tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    Path(stub_env["DERHOST_SMOKE_LOCK"]).symlink_to(target)
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS))
    _assert_lock_path_refused(smoke_tree, stub_env, result)
    assert target.read_text() == "keep\n"


def test_a_smoke_lock_path_that_is_a_fifo_is_refused_not_waited_on(
    smoke_tree: Path, stub_env: dict[str, str]
) -> None:
    # Opening a FIFO for write blocks until a reader appears, so a short
    # timeout turns a hang into a failure instead of a stalled suite.
    os.mkfifo(stub_env["DERHOST_SMOKE_LOCK"])
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS), timeout=10)
    _assert_lock_path_refused(smoke_tree, stub_env, result)


def test_a_smoke_lock_path_that_is_a_directory_is_refused_before_any_docker_call(
    smoke_tree: Path, stub_env: dict[str, str]
) -> None:
    Path(stub_env["DERHOST_SMOKE_LOCK"]).mkdir()
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS))
    _assert_lock_path_refused(smoke_tree, stub_env, result)


@pytest.mark.parametrize("reserved", ["server", "smoke", "interoperability-service server"])
def test_reserved_agent_dir_is_refused_before_any_docker_call(
    smoke_tree: Path, stub_env: dict[str, str], reserved: str
) -> None:
    result = _make_smoke(smoke_tree, stub_env, f"AGENT_DIRS={reserved}")
    assert result.returncode != 0
    assert "reserved" in result.stderr, result.stderr
    assert Path(stub_env["STUB_LOG"]).read_text() == ""


def test_smoke_sh_refuses_a_reserved_name_and_an_unset_list_on_its_own(
    smoke_tree: Path, stub_env: dict[str, str]
) -> None:
    smoke = str(smoke_tree / "docker" / "smoke.sh")
    for value, needle in (
        ("server", "reserved"),
        ("smoke", "reserved"),
        ("../x", "invalid"),
        (f"{RESTART_AGENT} {RESTART_AGENT}", "duplicate"),
    ):
        env = {**stub_env, "DERHOST_SMOKE_AGENT_DIRS": value}
        result = _run([smoke], smoke_tree, env)
        assert result.returncode != 0, value
        assert needle in result.stdout + result.stderr, (value, result.stdout, result.stderr)
    unset = _run([smoke], smoke_tree, stub_env)
    assert unset.returncode != 0
    assert "DERHOST_SMOKE_AGENT_DIRS" in unset.stdout + unset.stderr
    assert Path(stub_env["STUB_LOG"]).read_text() == ""


def test_a_listed_dir_without_an_overlay_is_refused(smoke_tree: Path, stub_env: dict[str, str]) -> None:
    (smoke_tree / "docker" / RESTART_AGENT / "compose.smoke.yml").unlink()
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "compose.smoke.yml" in result.stdout + result.stderr
    assert Path(stub_env["STUB_LOG"]).read_text() == ""


# --- the teardown count is taken after `down` and must be zero. ------------


@pytest.mark.parametrize(
    "linger",
    [
        {"STUB_LINGER_PROJECT": "derhost-smoke"},
        {"STUB_LINGER_PROJECT": f"derhost-smoke-{RESTART_AGENT}"},
        {"STUB_IMAGE_LINGER": "1"},
    ],
)
def test_an_object_left_after_down_fails_the_run(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer, linger: dict[str, str]
) -> None:
    stub_env.update(linger)
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "smoke passed" not in result.stdout
    assert "teardown" in result.stdout, result.stdout


def test_a_clean_run_reports_a_zero_teardown_count(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "teardown: 0 smoke objects remain" in result.stdout, result.stdout


def test_publish_host_and_timeouts_reach_the_compose_calls(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    stub_env["DERHOST_PUBLISH_HOST"] = "127.0.0.2"
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode == 0, result.stdout + result.stderr
    up_lines = [
        ln for ln in Path(stub_env["STUB_LOG"]).read_text().splitlines() if "ARGS: compose" in ln and " up " in ln
    ]
    assert len(up_lines) == 1 + len(AGENTS), up_lines
    assert all("DERHOST_PUBLISH_HOST=127.0.0.2" in ln for ln in up_lines), up_lines
    server_up = next(c for c in _compose_calls(_log_calls(stub_env), "up") if _flag_value(c, "-p") == "derhost-smoke")
    assert _flag_value(server_up, "--wait-timeout") == "5", server_up


# --- a smoke run never writes the developer's :local image (#95). ----------


def test_smoke_builds_the_smoke_image_directly_and_never_touches_local(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode == 0, result.stdout + result.stderr
    log_lines = Path(stub_env["STUB_LOG"]).read_text().splitlines()
    calls = _log_calls(stub_env)
    assert not [c for c in calls if c[0] == "tag"], calls
    assert not [ln for ln in log_lines if ":local" in ln], log_lines
    build_lines = [ln for ln in log_lines if "ARGS: build.sh" in ln]
    assert len(build_lines) == len(AGENTS), build_lines
    for agent in AGENTS:
        overlay = smoke_tree / "docker" / agent / "compose.smoke.yml"
        assert any(f"DERHOST_BUILD_OVERLAY={overlay} ARGS: build.sh {agent}" in ln for ln in build_lines), build_lines


# --- INT and TERM tear down and end the run (#85 item 1, this script). ------


def _smoke_sh(
    tree: Path,
    env: dict[str, str],
    dirs: str,
    signal_at: tuple[str, str] | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    """Runs smoke.sh directly as a process whose pid the build stub can signal."""
    run_env = {**env, "DERHOST_SMOKE_AGENT_DIRS": dirs, "STUB_SMOKE_PIDFILE": str(tree.parent / "smoke.pid")}
    if signal_at is not None:
        run_env["STUB_SIGNAL_DIR"], run_env["STUB_SIGNAL"] = signal_at
    script = 'echo $$ > "$STUB_SMOKE_PIDFILE"; exec "$0"'
    return subprocess.run(
        ["bash", "-c", script, str(tree / "docker" / "smoke.sh")],
        cwd=tree,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _down_projects(calls: list[list[str]]) -> set[str | None]:
    return {_flag_value(c, "-p") for c in _compose_calls(calls, "down")}


@pytest.mark.parametrize(("signal_name", "status"), [("INT", 130), ("TERM", 143)])
def test_a_signal_tears_down_and_ends_the_run_with_its_status(
    smoke_tree: Path,
    stub_env: dict[str, str],
    connections: _ConnectionsServer,
    signal_name: str,
    status: int,
) -> None:
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS), signal_at=(AGENTS[0], signal_name))
    assert result.returncode == status, result.stdout + result.stderr
    assert "smoke passed" not in result.stdout
    calls = _log_calls(stub_env)
    # The run did not resume: the second agent was never built, and no agent was started.
    assert [c[1] for c in calls if c[0] == "build.sh"] == [AGENTS[0]], calls
    assert {_flag_value(c, "-p") for c in _compose_calls(calls, "up")} == {"derhost-smoke"}, calls
    # Teardown ran for every project, and its count was taken.
    assert _down_projects(calls) == ALL_SMOKE_PROJECTS, calls
    assert "teardown: 0 smoke objects remain" in result.stdout, result.stdout


@pytest.mark.parametrize(("signal_name", "status"), [("INT", 130), ("TERM", 143)])
def test_a_signal_during_the_server_build_is_acted_on_at_once_and_nothing_later_runs(
    smoke_tree: Path, stub_env: dict[str, str], signal_name: str, status: int
) -> None:
    # The stub's build outlasts the 15 s bound by 10 s: a run that waits for the
    # step to end instead of reacting to the signal fails the bound.
    stub_env["STUB_SIGNAL_SERVER_BUILD"] = signal_name
    started = time.monotonic()
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS))
    elapsed = time.monotonic() - started
    assert result.returncode == status, result.stdout + result.stderr
    assert elapsed < 15, elapsed
    assert "smoke passed" not in result.stdout
    calls = _log_calls(stub_env)
    assert not [c for c in calls if c[0] == "build.sh"], calls
    assert not _compose_calls(calls, "up"), calls
    assert _down_projects(calls) == ALL_SMOKE_PROJECTS, calls
    assert "teardown: 0 smoke objects remain" in result.stdout, result.stdout


@pytest.mark.parametrize(("signal_name", "status"), [("INT", 130), ("TERM", 143)])
def test_a_signal_during_the_first_config_step_ends_the_run_before_anything_is_built_or_torn_down(
    smoke_tree: Path, stub_env: dict[str, str], signal_name: str, status: int
) -> None:
    # The stub's config outlasts the 15 s bound by 10 s. Nothing exists yet, so
    # no teardown runs: the exit trap is not armed this early.
    stub_env["STUB_SIGNAL_CONFIG"] = signal_name
    started = time.monotonic()
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS))
    elapsed = time.monotonic() - started
    assert result.returncode == status, result.stdout + result.stderr
    assert elapsed < 15, elapsed
    assert f"caught {signal_name}, stopping" in result.stdout, result.stdout
    calls = _log_calls(stub_env)
    assert [c for c in calls if c[0] == "compose"], "the config step never ran"
    assert not [c for c in calls if c[0] == "build.sh"], calls
    assert not _compose_calls(calls, "build"), calls
    assert not _compose_calls(calls, "up"), calls
    assert not _compose_calls(calls, "down"), calls
    assert "teardown" not in result.stdout, result.stdout


@pytest.mark.parametrize(("signal_name", "status"), [("INT", 130), ("TERM", 143)])
def test_a_signal_during_an_agent_build_is_acted_on_at_once_and_tears_down(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer, signal_name: str, status: int
) -> None:
    # The stub's build outlasts the 15 s bound by 10 s: an agent build run in
    # the foreground, where a trap waits for it, fails the bound.
    stub_env["STUB_BUILD_SLEEP"] = "1"
    started = time.monotonic()
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS), signal_at=(AGENTS[0], signal_name))
    elapsed = time.monotonic() - started
    assert result.returncode == status, result.stdout + result.stderr
    assert elapsed < 15, elapsed
    calls = _log_calls(stub_env)
    assert [c[1] for c in calls if c[0] == "build.sh"] == [AGENTS[0]], calls
    assert {_flag_value(c, "-p") for c in _compose_calls(calls, "up")} == {"derhost-smoke"}, calls
    assert _down_projects(calls) == ALL_SMOKE_PROJECTS, calls
    assert "teardown: 0 smoke objects remain" in result.stdout, result.stdout


# --- the job probe step: a signal ends it at once, a stuck probe is killed (#96). ---

# A probe that outlasts every bound here. When STUB_SIGNAL_PROBE names a signal
# it first signals the running smoke.sh, as the build stubs do.
_STUB_PROBE = """import os
import signal
import time

name = os.environ.get("STUB_SIGNAL_PROBE")
if name:
    with open(os.environ["STUB_SMOKE_PIDFILE"]) as pidfile:
        os.kill(int(pidfile.read()), getattr(signal, "SIG" + name))
time.sleep(40)
"""


def _use_stub_probe(tree: Path) -> None:
    (tree / "docker" / "lib" / "smoke_probe.py").write_text(_STUB_PROBE, encoding="utf-8")


def _assert_probe_step_ended_with_teardown(result: subprocess.CompletedProcess[str], env: dict[str, str]) -> None:
    assert "smoke passed" not in result.stdout
    calls = _log_calls(env)
    # The restart comes after the probes, so it never started.
    assert not [c for c in calls if c[0] in ("stop", "start")], calls
    assert _down_projects(calls) == ALL_SMOKE_PROJECTS, calls
    assert "teardown: 0 smoke objects remain" in result.stdout, result.stdout


@pytest.mark.parametrize(("signal_name", "status"), [("INT", 130), ("TERM", 143)])
def test_a_signal_during_the_job_probe_is_acted_on_at_once_and_tears_down(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer, signal_name: str, status: int
) -> None:
    # The stub probe sleeps 40 s after signalling: a probe run in the
    # foreground, where the trap waits for it, fails the 15 s bound.
    _use_stub_probe(smoke_tree)
    stub_env["STUB_SIGNAL_PROBE"] = signal_name
    started = time.monotonic()
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS))
    elapsed = time.monotonic() - started
    assert result.returncode == status, result.stdout + result.stderr
    assert elapsed < 15, elapsed
    assert f"caught {signal_name}, stopping" in result.stdout, result.stdout
    _assert_probe_step_ended_with_teardown(result, stub_env)


def test_a_job_probe_that_outlasts_the_step_timeout_is_killed_and_its_status_reported(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    # Without the timeout wrapper the 40 s stub runs to its end and fails the bound.
    _use_stub_probe(smoke_tree)
    stub_env["DERHOST_SMOKE_STEP_TIMEOUT"] = "4"
    started = time.monotonic()
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS))
    elapsed = time.monotonic() - started
    assert result.returncode == 1, result.stdout + result.stderr
    assert elapsed < 20, elapsed
    # 124 is what `timeout` returns for a step it ended.
    assert f"job probe failed for {AGENTS[0]} (exit status 124)" in result.stderr, result.stderr
    _assert_probe_step_ended_with_teardown(result, stub_env)


# --- a second signal during teardown (#95). ---------------------------------


@pytest.mark.parametrize("second", ["INT", "TERM"])
def test_a_second_signal_during_teardown_is_ignored_until_teardown_finishes(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer, second: str
) -> None:
    # The first TERM comes from the first agent's build; the second arrives at
    # the first `down` of the teardown that follows it.
    stub_env["STUB_SIGNAL_FIRST_DOWN"] = second
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS), signal_at=(AGENTS[0], "TERM"))
    assert result.returncode == 143, result.stdout + result.stderr
    assert f"caught {second} during teardown" in result.stdout, result.stdout
    calls = _log_calls(stub_env)
    downs = [_flag_value(c, "-p") for c in _compose_calls(calls, "down")]
    assert sorted(downs) == sorted(ALL_SMOKE_PROJECTS), downs
    assert "teardown: 0 smoke objects remain" in result.stdout, result.stdout


def test_a_signal_at_the_first_down_of_the_success_path_still_tears_every_project_down(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    # The signal ends the run mid-teardown, so the teardown has not finished
    # when the exit trap runs; it must run again rather than be skipped.
    stub_env["STUB_SIGNAL_FIRST_DOWN"] = "TERM"
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS))
    assert result.returncode == 143, result.stdout + result.stderr
    assert "smoke passed" not in result.stdout
    assert _down_projects(_log_calls(stub_env)) == ALL_SMOKE_PROJECTS
    assert "teardown: 0 smoke objects remain" in result.stdout, result.stdout


# --- a failing teardown is reported on both paths (#95). --------------------


_DOWN_STEPS = ("down server", *(f"down {a}" for a in AGENTS))


def _assert_teardown_failures_reported(result: subprocess.CompletedProcess[str], steps: tuple[str, ...]) -> None:
    for step in steps:
        assert f"[smoke] teardown step failed: {step}\n" in result.stdout, (step, result.stdout)
    assert "teardown: 0 smoke objects remain" in result.stdout, result.stdout
    assert "smoke passed" not in result.stdout


def test_a_failing_down_on_the_success_path_is_named_counted_and_fails_the_run(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    stub_env["STUB_DOWN_EXIT"] = "1"
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    _assert_teardown_failures_reported(result, _DOWN_STEPS)


def test_a_failing_image_removal_on_the_success_path_is_named_counted_and_fails_the_run(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    stub_env["STUB_IMAGE_RM_EXIT"] = "1"
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    _assert_teardown_failures_reported(
        result, ("image rm derhost-server:smoke", *(f"image rm derhost-{a}:smoke" for a in AGENTS))
    )


def test_a_failing_down_on_the_failure_path_is_named_and_counted(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    stub_env["STUB_UP_EXIT"] = "1"
    stub_env["STUB_DOWN_EXIT"] = "1"
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    _assert_teardown_failures_reported(result, _DOWN_STEPS)


def test_a_failing_teardown_keeps_the_original_exit_status(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    stub_env["STUB_DOWN_EXIT"] = "1"
    result = _smoke_sh(smoke_tree, stub_env, " ".join(AGENTS), signal_at=(AGENTS[0], "TERM"))
    assert result.returncode == 143, result.stdout + result.stderr
    _assert_teardown_failures_reported(result, _DOWN_STEPS)


def test_a_teardown_that_cannot_list_objects_fails_closed_and_says_so(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    # The listing works for the refusal check and fails once `down` has run.
    stub_env["STUB_LIST_EXIT_AFTER_DOWN"] = "1"
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "teardown: cannot list the remaining smoke objects" in result.stdout, result.stdout
    assert "smoke passed" not in result.stdout


# --- the checks that were unprotected at ba4335a (#95). ----------------------


def test_the_run_fails_when_an_agent_identity_never_connects(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    connections.hidden = {IDENTITIES[AGENTS[0]]}
    stub_env["DERHOST_SMOKE_WAIT_TIMEOUT"] = "2"
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "not every identity connected within 2s" in result.stderr, result.stderr
    assert "smoke passed" not in result.stdout
    # It stops before the restart step touches any container.
    assert not [c for c in _log_calls(stub_env) if c[0] in ("stop", "start")]


def test_the_run_fails_when_the_restart_leaves_started_at_unchanged(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    stub_env["STUB_STARTED_AT_FIXED"] = "1"
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert f"{RESTART_CONTAINER} StartedAt did not change (T0)" in result.stderr, result.stderr
    assert "StartedAt changed" not in result.stdout
    assert "smoke passed" not in result.stdout


def test_an_error_after_the_stop_is_not_taken_for_the_identity_being_absent(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    connections.fail_after_stop = True
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "der.rtc: MISSING" not in result.stdout
    assert "is still connected after" in result.stderr, result.stderr
    # The container is never started: the wait for MISSING is what gates it.
    assert not [c for c in _log_calls(stub_env) if c[0] == "start"], _log_calls(stub_env)
    assert "smoke passed" not in result.stdout


@pytest.mark.parametrize("failing", AGENTS)
def test_a_failing_build_sh_stops_before_that_agent_or_a_later_one_starts(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer, failing: str
) -> None:
    stub_env["STUB_BUILD_FAIL_DIR"] = failing
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert f"build.sh failed for {failing}" in result.stderr, result.stderr
    calls = _log_calls(stub_env)
    started = {_flag_value(c, "-p") for c in _compose_calls(calls, "up")}
    assert f"derhost-smoke-{failing}" not in started, calls
    built = [c[1] for c in calls if c[0] == "build.sh"]
    assert built == list(AGENTS[: AGENTS.index(failing) + 1]), built
    assert "smoke passed" not in result.stdout
    assert _down_projects(calls) == ALL_SMOKE_PROJECTS, calls


def test_each_smoke_image_is_removed_once_by_name_and_no_local_image_is(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode == 0, result.stdout + result.stderr
    removed = sorted(c[2] for c in _log_calls(stub_env) if c[:2] == ["image", "rm"])
    assert removed == sorted(["derhost-server:smoke", *(f"derhost-{a}:smoke" for a in AGENTS)]), removed


def test_an_empty_agent_list_runs_the_server_alone_through_to_a_pass(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    result = _smoke_sh(smoke_tree, stub_env, "")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "smoke passed" in result.stdout
    assert f"restart: {RESTART_AGENT} is not in AGENT_DIRS, skipped" in result.stdout, result.stdout
    assert "teardown: 0 smoke objects remain" in result.stdout, result.stdout
    calls = _log_calls(stub_env)
    assert {_flag_value(c, "-p") for c in calls if c[0] == "compose"} == {"derhost-smoke"}, calls
    assert not [c for c in calls if c[0] in ("build.sh", "stop", "start")], calls


# --- small fixes: stderr from a die in `$(...)`, input refusals, the count. --


def test_a_die_inside_a_command_substitution_reaches_stderr(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    dockerfile = smoke_tree / "docker" / AGENTS[0] / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text().replace('"--identity"', '"--identit"'), encoding="utf-8")
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert f"docker/{AGENTS[0]}/Dockerfile passes no --identity" in result.stderr, result.stderr


def test_a_listing_that_fails_is_reported_on_stderr(smoke_tree: Path, stub_env: dict[str, str]) -> None:
    stub_env["STUB_LIST_EXIT"] = "1"
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "cannot list containers" in result.stderr, result.stderr
    assert not _compose_calls(_log_calls(stub_env), "up")


def test_a_newline_in_the_agent_list_is_refused_before_any_docker_call(
    smoke_tree: Path, stub_env: dict[str, str]
) -> None:
    result = _smoke_sh(smoke_tree, stub_env, f"{AGENTS[0]}\n{AGENTS[1]}")
    assert result.returncode != 0
    assert "must be one line of space-separated names" in result.stderr, result.stderr
    assert Path(stub_env["STUB_LOG"]).read_text() == ""


@pytest.mark.parametrize("separator", ["\t", "\r", "\f"])
def test_other_whitespace_in_the_agent_list_is_refused_before_any_docker_call(
    smoke_tree: Path, stub_env: dict[str, str], separator: str
) -> None:
    result = _smoke_sh(smoke_tree, stub_env, f"{AGENTS[0]}{separator}{AGENTS[1]}")
    assert result.returncode != 0
    assert "must be one line of space-separated names" in result.stderr, result.stderr
    assert Path(stub_env["STUB_LOG"]).read_text() == ""


@pytest.mark.parametrize("empty", ["", " "])
def test_make_refuses_an_empty_agent_dirs_before_any_docker_call(
    smoke_tree: Path, stub_env: dict[str, str], empty: str
) -> None:
    result = _make_smoke(smoke_tree, stub_env, f"AGENT_DIRS={empty}")
    assert result.returncode != 0
    assert "AGENT_DIRS is empty" in result.stderr, result.stderr
    assert Path(stub_env["STUB_LOG"]).read_text() == ""


def test_the_leftover_count_counts_an_object_matched_by_two_filters_once(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    # One lingering object per kind (container, network, volume), listed by
    # the project label and again by the name filter: 3 objects, not 6.
    stub_env["STUB_LINGER_PROJECT"] = "derhost-smoke"
    stub_env["STUB_LINGER_NAME"] = "1"
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "teardown: 3 smoke objects remain" in result.stdout, result.stdout


def test_the_readme_names_every_smoke_timeout_and_the_local_images_it_keeps() -> None:
    smoke_sh = (DOCKER_DIR / "smoke.sh").read_text(encoding="utf-8")
    readme = (DOCKER_DIR / "README.md").read_text(encoding="utf-8")
    knobs = sorted(set(re.findall(r"\$\{(DERHOST_SMOKE_[A-Z_]+):-", smoke_sh)))
    # Not vacuous: the four timeouts, the restart window, the poll interval and the lock path.
    assert len(knobs) == 6, knobs
    for knob in knobs:
        assert knob in readme, knob
    assert "derhost-base:local" in readme


# --- merged configs share no object name with the base stack. --------------


def _compose_config(project_dir: Path, files: list[Path], project: str | None = None) -> dict[str, Any]:
    env = os.environ.copy()
    for key in _SMOKE_ENV_KEYS:
        env.pop(key, None)
    env["DER_AGENT_SRC"] = "/dev/null/no-build"
    cmd = ["docker", "compose"]
    if project:
        cmd += ["-p", project]
    for f in files:
        cmd += ["-f", str(f)]
    cmd += ["--project-directory", str(project_dir), "config", "--format", "json"]
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def _object_names(config: dict[str, Any]) -> set[str]:
    names = {config["name"]}
    for service in config["services"].values():
        names.add(service["container_name"])
        names.add(service["image"])
    for section in ("networks", "volumes"):
        names.update(item["name"] for item in (config.get(section) or {}).values())
    return names


def shared_names(directory: Path, overlay: Path, smoke_project: str) -> set[str]:
    base = _compose_config(directory, [directory / "docker-compose.yml"])
    merged = _compose_config(directory, [directory / "docker-compose.yml", overlay], project=smoke_project)
    return _object_names(base) & _object_names(merged)


@pytest.mark.parametrize(
    ("directory", "project"),
    [("server", "derhost-smoke"), *((a, f"derhost-smoke-{a}") for a in AGENTS)],
)
def test_overlay_shares_no_object_name_with_the_base_stack(directory: str, project: str) -> None:
    path = DOCKER_DIR / directory
    assert shared_names(path, path / "compose.smoke.yml", project) == set()


@pytest.mark.parametrize("agent", AGENTS)
def test_overlay_without_its_network_rename_is_caught(agent: str, tmp_path: Path) -> None:
    # Control: the disjointness check goes red when an overlay drops the
    # network rename, so a green result above is not a check that cannot fail.
    overlay = (DOCKER_DIR / agent / "compose.smoke.yml").read_text(encoding="utf-8")
    doctored = overlay.split("networks:")[0]
    assert "derhost-smoke-net" not in doctored and "networks:" in overlay
    path = tmp_path / "overlay.yml"
    path.write_text(doctored, encoding="utf-8")
    assert shared_names(DOCKER_DIR / agent, path, f"derhost-smoke-{agent}") == {"derhost-net"}


@pytest.mark.parametrize("agent", AGENTS)
def test_agent_overlay_keeps_the_bus_address_and_joins_the_server_smoke_network(agent: str) -> None:
    path = DOCKER_DIR / agent
    base = _compose_config(path, [path / "docker-compose.yml"])
    merged = _compose_config(
        path, [path / "docker-compose.yml", path / "compose.smoke.yml"], project=f"derhost-smoke-{agent}"
    )
    service = f"derhost-{agent}"
    assert merged["services"][service]["environment"] == base["services"][service]["environment"]
    assert merged["networks"]["derhost-net"]["name"] == "derhost-smoke-net"
    assert merged["networks"]["derhost-net"]["external"] is True
    assert merged["services"][service]["container_name"] == f"derhost-smoke-{agent}"
    assert merged["services"][service]["image"] == f"derhost-{agent}:smoke"
