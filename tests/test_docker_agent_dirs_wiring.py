"""docker/docker.mk's real AGENT_DIRS default (#68): once it names both
interoperability-service and realtime-control-agent, `stack-up C=all` must
start the server then both agents, and `stack-down` must stop both agents
before the server. test_docker_stack_commands.py exercises this wiring
mechanism generically with a synthetic override; these tests exercise the
actual default this PR sets, with no override, through the same kind of
stub `docker` on PATH so no real daemon or image build is reached. The stub
loads each compose file with the real `docker compose config`, as docker
itself does before `up`, `down`, `ps` and `build`, and fails when it cannot.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STACK_PORT = 5410

_OVERRIDE_KEYS = ("DERHOST_PUBLISH_HOST", "EXPECTED", "DERHOST_CHECK_BASE_URL", "C")
_MAKE_RECURSION_KEYS = ("MAKELEVEL", "MAKEFLAGS", "MFLAGS")

_DOCKER_STUB = """#!/usr/bin/env bash
set -euo pipefail
{
    printf 'DER_AGENT_SRC=%s ARGS:' "${DER_AGENT_SRC-unset}"
    printf ' %q' "$@"
    printf '\\n'
} >> "$STUB_LOG"

if [ "${1-}" = "compose" ]; then
    shift
    sub=""
    pre=()
    for arg in "$@"; do
        case "$arg" in
            up|down|ps|build|config) sub="$arg"; break ;;
        esac
        pre+=("$arg")
    done
    case "$sub" in
        config) exec "$STUB_REAL_DOCKER" compose "$@" ;;
        up|down|ps|build)
            # Docker loads the compose file before any of these; a file it
            # cannot load (a required variable unset) fails the call here too.
            rc=0
            "$STUB_REAL_DOCKER" compose "${pre[@]}" config -q >/dev/null 2>&1 || rc=$?
            printf '%s rc=%s\\n' "$sub" "$rc" >> "$STUB_LOG.load"
            exit "$rc"
            ;;
        *)
            echo "stub: unexpected docker compose invocation: $*" >&2
            exit 1
            ;;
    esac
elif [ "${1-}" = "inspect" ]; then
    printf 'healthy\\n'
    exit 0
elif [ "${1-}" = "image" ] && [ "${2-}" = "inspect" ]; then
    exit 0
else
    echo "stub: unexpected docker invocation: $*" >&2
    exit 1
fi
"""


@pytest.fixture
def docker_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    stub_path = bin_dir / "docker"
    stub_path.write_text(_DOCKER_STUB)
    stub_path.chmod(0o755)
    real_docker = shutil.which("docker")
    assert real_docker is not None, "the real docker CLI must be on PATH to forward `compose ... config`"
    log_path = tmp_path / "stub.log"
    log_path.write_text("")
    Path(f"{log_path}.load").write_text("")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(log_path))
    monkeypatch.setenv("STUB_REAL_DOCKER", real_docker)
    return log_path


def _run_make(*args: str, overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in (*_OVERRIDE_KEYS, *_MAKE_RECURSION_KEYS):
        env.pop(key, None)
    if overrides:
        env.update(overrides)
    return subprocess.run(
        ["make", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _stub_calls(log_text: str) -> list[list[str]]:
    calls = []
    for line in log_text.splitlines():
        if not line.strip():
            continue
        _, _, rest = line.partition("ARGS:")
        calls.append(shlex.split(rest))
    return calls


def _project_dirs(calls: list[list[str]], subcommand: str) -> list[str]:
    dirs = []
    for call in calls:
        if not call or call[0] != "compose" or subcommand not in call:
            continue
        idx = call.index("--project-directory")
        dirs.append(call[idx + 1])
    return dirs


def _load_results(log_path: Path) -> list[str]:
    return Path(f"{log_path}.load").read_text().splitlines()


def _agent_events(log_text: str) -> list[tuple[str, str, str, list[str]]]:
    """(subcommand, agent directory name, DER_AGENT_SRC seen, args) for every
    compose call that names an agent's own compose file or project directory."""
    events = []
    for line in log_text.splitlines():
        if "ARGS:" not in line:
            continue
        head, _, rest = line.partition(" ARGS:")
        der_agent_src = head.removeprefix("DER_AGENT_SRC=")
        call = shlex.split(rest)
        if not call or call[0] != "compose":
            continue
        sub = next((a for a in call[1:] if a in ("up", "down", "ps", "build")), None)
        if sub is None:
            continue
        if "--project-directory" in call:
            where = call[call.index("--project-directory") + 1]
        else:
            where = str(Path(call[call.index("-f") + 1]).parent)
        name = Path(where).name
        if name == "server":
            continue
        events.append((sub, name, der_agent_src, call))
    return events


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _agents_checkout(tmp_path: Path, *, with_interoperability: bool = True) -> Path:
    """A committed throwaway checkout holding both agents' archived paths."""
    repo = tmp_path / "agents-src"
    (repo / "rt_control").mkdir(parents=True)
    (repo / "rt_control" / "agent.py").write_text("# placeholder\n")
    if with_interoperability:
        (repo / "src" / "interoperability").mkdir(parents=True)
        (repo / "src" / "interoperability" / "agent.py").write_text("# placeholder\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _require_free_stack_port() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", STACK_PORT))
    except OSError:
        pytest.skip(f"127.0.0.1:{STACK_PORT} is already held by something else on this host")
    finally:
        sock.close()


def test_stub_refuses_an_agent_compose_file_it_cannot_load(docker_stub: Path) -> None:
    # Control: the stub must fail where real docker fails. Without
    # DER_AGENT_SRC an agent's compose file does not load, so `up` exits
    # non-zero and the load log records it.
    env = dict(os.environ)
    env.pop("DER_AGENT_SRC", None)
    result = subprocess.run(
        ["docker", "compose", "-p", "derhost-x", "-f", "docker/realtime-control-agent/docker-compose.yml",
         "--project-directory", "docker/realtime-control-agent", "up", "-d", "--no-build"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert _load_results(docker_stub) == ["up rc=1"]


def test_stack_down_default_stops_both_agents_before_the_server(docker_stub: Path) -> None:
    result = _run_make("stack-down")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _stub_calls(docker_stub.read_text())
    dirs = _project_dirs(calls, "down")
    assert dirs == [
        "docker/interoperability-service",
        "docker/realtime-control-agent",
        "docker/server",
    ], dirs
    # Each call must have loaded: an agent compose file that cannot load makes
    # `down` fail, which the recipe's `|| true` hides, leaving the agent up.
    assert _load_results(docker_stub) == ["down rc=0"] * 3


def test_stack_status_default_covers_the_server_and_both_agents(docker_stub: Path) -> None:
    result = _run_make("stack-status")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _stub_calls(docker_stub.read_text())
    dirs = _project_dirs(calls, "ps")
    assert dirs == [
        "docker/server",
        "docker/interoperability-service",
        "docker/realtime-control-agent",
    ], dirs
    assert _load_results(docker_stub) == ["ps rc=0"] * 3


def test_stack_up_all_default_builds_and_starts_the_server_then_both_agents(
    docker_stub: Path, tmp_path: Path
) -> None:
    _require_free_stack_port()
    checkout = _agents_checkout(tmp_path)
    result = _run_make("stack-up", overrides={"C": "all", "DER_AGENT_SRC": str(checkout)})
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _stub_calls(docker_stub.read_text())
    dirs = _project_dirs(calls, "up")
    assert dirs == [
        "docker/server",
        "docker/interoperability-service",
        "docker/realtime-control-agent",
    ], dirs
    # Each agent is built by its own build.sh (check-clean and git archive
    # included) and then started without a build, under a placeholder
    # DER_AGENT_SRC so its compose file loads.
    events = _agent_events(docker_stub.read_text())
    assert [(sub, name) for sub, name, _, _ in events] == [
        ("build", "interoperability-service"),
        ("up", "interoperability-service"),
        ("build", "realtime-control-agent"),
        ("up", "realtime-control-agent"),
    ], events
    for sub, _, der_agent_src, call in events:
        if sub == "build":
            assert der_agent_src not in ("unused", str(checkout)), der_agent_src
        else:
            assert der_agent_src == "unused", der_agent_src
            assert "--no-build" in call and "--build" not in call, call
    assert set(_load_results(docker_stub)) == {"build rc=0", "up rc=0"}


def test_stack_up_all_stops_at_the_first_failing_agent(docker_stub: Path, tmp_path: Path) -> None:
    _require_free_stack_port()
    checkout = _agents_checkout(tmp_path, with_interoperability=False)
    result = _run_make("stack-up", overrides={"C": "all", "DER_AGENT_SRC": str(checkout)})
    assert result.returncode != 0, result.stdout + result.stderr
    assert "interoperability-service" in result.stderr, result.stderr
    events = _agent_events(docker_stub.read_text())
    assert [name for _, name, _, _ in events if name == "realtime-control-agent"] == [], events
    assert [sub for sub, name, _, _ in events if name == "interoperability-service"] == [], events
