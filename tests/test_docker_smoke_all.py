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

import http.server
import json
import os
import shlex
import shutil
import subprocess
import threading
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
    if [ -n "${STUB_LEFTOVER_PROJECT-}" ] && [[ " $* " == *"com.docker.compose.project=${STUB_LEFTOVER_PROJECT} "* ]]; then
        echo deadbeef0001
    elif [ -n "${STUB_LINGER_PROJECT-}" ] && downs_logged && [[ " $* " == *"com.docker.compose.project=${STUB_LINGER_PROJECT} "* ]]; then
        echo deadbeef0002
    fi
    return 0
}

if [ "${1-}" = "compose" ]; then
    shift
    case " $* " in
        *" config "*) exec "$STUB_REAL_DOCKER" compose "$@" ;;
        *" port "*) printf '%s\\n' "$STUB_PORT_OUTPUT" ;;
        *" up "*) exit "${STUB_UP_EXIT:-0}" ;;
        *" down "*) exit "${STUB_DOWN_EXIT:-0}" ;;
        *" build "*) exit 0 ;;
        *) echo "stub: unexpected docker compose invocation: $*" >&2; exit 1 ;;
    esac
elif [ "${1-}" = "ps" ] || { [ "${1-}" = "network" ] && [ "${2-}" = "ls" ]; } || { [ "${1-}" = "volume" ] && [ "${2-}" = "ls" ]; }; then
    listing "$@"
elif [ "${1-}" = "image" ] && [ "${2-}" = "ls" ]; then
    if [ -n "${STUB_IMAGE_LINGER-}" ] && downs_logged; then echo deadbeef0003; fi
elif [ "${1-}" = "image" ] && { [ "${2-}" = "inspect" ] || [ "${2-}" = "rm" ]; }; then
    exit 0
elif [ "${1-}" = "container" ] && [ "${2-}" = "inspect" ]; then
    # StartedAt moves once per logged `start`, as a real container's does.
    printf 'T%s\\n' "$(/usr/bin/grep -c 'ARGS: start ' "$STUB_LOG" || true)"
elif [ "${1-}" = "tag" ] || [ "${1-}" = "stop" ] || [ "${1-}" = "start" ] || [ "${1-}" = "restart" ]; then
    exit 0
else
    echo "stub: unexpected docker invocation: $*" >&2
    exit 1
fi
"""

_BUILD_STUB = """#!/usr/bin/env bash
set -euo pipefail
printf 'DERHOST_PUBLISH_HOST=%s DER_AGENT_SRC=%s ARGS: build.sh %s\\n' "${DERHOST_PUBLISH_HOST-unset}" "${DER_AGENT_SRC-unset}" "$(basename "$(dirname "$0")")" >> "$STUB_LOG"
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
    restarted agent is absent while its container's last logged event is a stop."""

    def __init__(self, log_path: Path, always_connected: bool = False, never_returns: bool = False) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                body = json.dumps({"connections": {i: {} for i in outer.connected()}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                return

        self.log_path = log_path
        self.always_connected = always_connected
        self.never_returns = never_returns
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def connected(self) -> list[str]:
        identities = list(IDENTITIES.values())
        events = [
            c[0]
            for c in _stub_calls(self.log_path.read_text())
            if len(c) > 1 and c[0] in ("stop", "start") and c[1] == RESTART_CONTAINER
        ]
        stopped = bool(events) and events[-1] == "stop"
        if self.never_returns and "stop" in events:
            stopped = True
        if stopped and not self.always_connected:
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
        # The :smoke tag build.sh's :local image is retagged to is what the overlay runs.
        assert ["tag", f"derhost-{agent}:local", f"derhost-{agent}:smoke"] in calls
        assert build < _index_of(calls, lambda c, a=agent: c[:2] == ["tag", f"derhost-{a}:local"]) < up

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
    assert all("DER_AGENT_SRC=unused" in ln for ln in agent_compose), agent_compose


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
    assert "still connected" in result.stdout, result.stdout
    assert "smoke passed" not in result.stdout


def test_restart_fails_when_the_identity_does_not_return_within_the_window(
    smoke_tree: Path, stub_env: dict[str, str], connections: _ConnectionsServer
) -> None:
    connections.never_returns = True
    result = _make_smoke(smoke_tree, stub_env)
    assert result.returncode != 0
    assert "did not reconnect" in result.stdout, result.stdout
    assert "smoke passed" not in result.stdout
    # The failure path still tears everything down, agents before the server.
    calls = _log_calls(stub_env)
    assert _index_of(
        calls, lambda c: c[0] == "compose" and "down" in c and _flag_value(c, "-p") == f"derhost-smoke-{RESTART_AGENT}"
    ) < _index_of(calls, lambda c: c[0] == "compose" and "down" in c and _flag_value(c, "-p") == "derhost-smoke")


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


# --- merged configs share no object name with the base stack. --------------


def _compose_config(project_dir: Path, files: list[Path], project: str | None = None) -> dict[str, Any]:
    env = os.environ.copy()
    for key in _SMOKE_ENV_KEYS:
        env.pop(key, None)
    env["DER_AGENT_SRC"] = "unused"
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
