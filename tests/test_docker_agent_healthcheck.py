"""Agent container healthchecks must not match their own process (#68).

The healthcheck is `python3 -c <code>` run by `docker exec`. A check that
scans every /proc/<pid>/cmdline for the agent's script name finds the
healthcheck's own command line, which holds that name inside the code, so it
reports healthy with no agent running. The agent is the container's PID 1
(no ENTRYPOINT, no init), so the check reads /proc/1/cmdline only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKER_DIR = REPO_ROOT / "docker"
AGENTS = ("interoperability-service", "realtime-control-agent")

SELF_MATCHING_CODE = (
    "import os,sys; sys.exit(0 if any(b'start-legacy.py' in open(f'/proc/{p}/cmdline','rb').read() "
    "for p in os.listdir('/proc') if p.isdigit()) else 1)"
)


def _dockerfile_code(agent: str) -> str:
    for line in (DOCKER_DIR / agent / "Dockerfile").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("CMD [") and "-c" in stripped and "/proc" in stripped:
            argv = json.loads(stripped.removeprefix("CMD "))
            return argv[argv.index("-c") + 1]
    raise AssertionError(f"{agent}: no healthcheck CMD in the Dockerfile")


def _compose_code(agent: str) -> str:
    compose = yaml.safe_load((DOCKER_DIR / agent / "docker-compose.yml").read_text(encoding="utf-8"))
    argv = compose["services"][f"derhost-{agent}"]["healthcheck"]["test"]
    return argv[argv.index("-c") + 1]


def _run(code: str) -> int:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, check=False).returncode


def _with_cmdline_file(code: str, cmdline_file: Path) -> str:
    assert "/proc/1/cmdline" in code, code
    return code.replace("/proc/1/cmdline", str(cmdline_file))


def test_self_matching_form_reports_healthy_with_no_agent_running() -> None:
    # Control: this is the defect. Nothing on the test host runs the agent
    # as a process whose own command line lacks the name, yet the scan
    # finds the scanner itself.
    assert _run(SELF_MATCHING_CODE) == 0


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("source", ["Dockerfile", "compose"])
def test_healthcheck_is_unhealthy_when_the_agent_is_not_running(agent: str, source: str) -> None:
    code = _dockerfile_code(agent) if source == "Dockerfile" else _compose_code(agent)
    # PID 1 on the test host is not the agent, so a check that reads PID 1
    # only must fail; the self-matching form passes here.
    assert _run(code) == 1


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("source", ["Dockerfile", "compose"])
def test_healthcheck_reads_pid_1_for_the_agent_script(agent: str, source: str, tmp_path: Path) -> None:
    code = _dockerfile_code(agent) if source == "Dockerfile" else _compose_code(agent)
    cmdline = tmp_path / "cmdline"
    cmdline.write_bytes(b"python3\x00/app/start-legacy.py\x00--identity\x00x\x00")
    assert _run(_with_cmdline_file(code, cmdline)) == 0
    cmdline.write_bytes(b"sleep\x00100\x00")
    assert _run(_with_cmdline_file(code, cmdline)) == 1
