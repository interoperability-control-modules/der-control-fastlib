"""Static content checks for docker/realtime-control-agent (#68).

test_docker_layout.py's generic rules cover compose-file hardening; these
cover the property specific to this agent: the image never pulls in
volttron, volttron-core or julia, and never installs the agent's own
package. Each check is exercised against a violating string too, so a pass
here is not a check that could never fail.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "docker" / "realtime-control-agent"
REVISION_AGENTS = ("interoperability-service", "realtime-control-agent")


def _uncommented_lines(text: str) -> str:
    """The requirements file and Dockerfile both explain the exclusion in a
    '#' comment; the check below must read the same install/dependency
    lines pip and docker build read, not the prose naming what is excluded
    on purpose."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


_EXCLUDED_PACKAGE_MARKERS = ("volttron-core", "volttron_core", "volttron", "julia")


def check_no_excluded_packages(label: str, text: str) -> None:
    code = _uncommented_lines(text)
    for marker in _EXCLUDED_PACKAGE_MARKERS:
        if marker in code:
            raise AssertionError(f"{label}: names {marker}, which this native-modes-only image must not install")


def test_dockerfile_never_names_excluded_packages() -> None:
    check_no_excluded_packages("Dockerfile", (AGENT_DIR / "Dockerfile").read_text(encoding="utf-8"))


def test_requirements_never_names_excluded_packages() -> None:
    check_no_excluded_packages("requirements.txt", (AGENT_DIR / "requirements.txt").read_text(encoding="utf-8"))


def test_check_no_excluded_packages_fixture_fails_volttron_core() -> None:
    with pytest.raises(AssertionError, match="volttron-core"):
        check_no_excluded_packages("fixture", "volttron-core>=2.0.0rc30")


def test_check_no_excluded_packages_fixture_fails_volttron() -> None:
    with pytest.raises(AssertionError, match="volttron"):
        check_no_excluded_packages("fixture", "volttron>=11.0.0rc1")


def test_check_no_excluded_packages_fixture_fails_julia() -> None:
    with pytest.raises(AssertionError, match="julia"):
        check_no_excluded_packages("fixture", "julia>=0.6.2")


def check_never_installs_agent_package(text: str) -> None:
    # The only "pip install" line in the Dockerfile must install exactly the
    # pinned requirements file, nothing appended: a trailing extra argument
    # (a bare "." for the build context) would pull in the agent's own
    # package, and transitively volttron/julia, despite the check above.
    for line in text.splitlines():
        if "pip install" not in line:
            continue
        normalized = line.strip().rstrip("\\").strip()
        if not normalized.endswith("pip install -r requirements.txt"):
            raise AssertionError(f"unexpected pip install line: {line!r}")


def test_dockerfile_never_installs_the_agent_package() -> None:
    check_never_installs_agent_package((AGENT_DIR / "Dockerfile").read_text(encoding="utf-8"))


def test_check_never_installs_agent_package_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="pip install line"):
        check_never_installs_agent_package("RUN /app/.venv/bin/pip install .\n")


def test_check_never_installs_agent_package_fixture_fails_on_appended_context() -> None:
    # Control: "-r requirements.txt" is still present, but a bare "." is
    # appended too, which also installs the build context itself (#68).
    with pytest.raises(AssertionError, match="pip install line"):
        check_never_installs_agent_package("RUN /app/.venv/bin/pip install -r requirements.txt .\n")


COPY_FROM_AGENT_SRC = "COPY --from=agent_src --chown=root:root rt_control ./rt_control"


def check_copy_from_agent_src_is_scoped(text: str) -> None:
    # The only line copying from the agent_src build context must copy
    # exactly rt_control, never the whole context: agent_src is the agent's
    # checkout (or, once archived, its committed tree), and a broader copy
    # would ship whatever else lives there (#68).
    for line in text.splitlines():
        if "--from=agent_src" not in line:
            continue
        if line.strip() != COPY_FROM_AGENT_SRC:
            raise AssertionError(f"unexpected COPY --from=agent_src line: {line!r}")


def test_dockerfile_copy_from_agent_src_is_scoped() -> None:
    check_copy_from_agent_src_is_scoped((AGENT_DIR / "Dockerfile").read_text(encoding="utf-8"))


def test_check_copy_from_agent_src_is_scoped_fixture_fails() -> None:
    # Control: copying the whole context instead of the named subdirectory (#68).
    with pytest.raises(AssertionError, match="COPY --from=agent_src line"):
        check_copy_from_agent_src_is_scoped("COPY --from=agent_src . ./\n")


def test_config_example_sets_no_mode_to_use_julia() -> None:
    # The baked-in config must not itself flip a mode into the Julia path
    # this image has no interpreter for (#68).
    text = (AGENT_DIR / "config.example.json").read_text(encoding="utf-8")
    assert "use_julia" not in text, text
    assert "ctrl_eval_engine_app_path" not in text, text


def test_config_example_use_cases_is_empty() -> None:
    # The agent's own shipped configs (sample_config.json, es_control_test.json)
    # all configure a PeakLimiting use case that omits realtime_power_point,
    # a required constructor argument with no default
    # (rt_control/use_cases/peak_limiting.py:7 at 16804d8): loading one of
    # those configs raises inside configure_main before the periodic control
    # loop is ever scheduled. Left empty here rather than reproducing that
    # defect; not a change to the agent's own source.
    config = json.loads((AGENT_DIR / "config.example.json").read_text(encoding="utf-8"))
    assert config["use_cases"] == []


# Mode modules under rt_control/modes/reactive at 6ea0e71 carry no Julia code.
NATIVE_REACTIVE_MODES = frozenset(
    {
        "ConstantVar",
        "FixedPowerFactor",
        "VoltVar",
        "WattVar",
        "PowerFactorCorrection",
        "DynamicReactiveCurrentSupport",
    }
)


def _example_config() -> dict:
    return json.loads((AGENT_DIR / "config.example.json").read_text(encoding="utf-8"))


def check_modes_are_one_native_constant_var(modes: list[dict]) -> None:
    # rt_control.modes.novel imports julia at module scope
    # (es_control_mode.py:2 at 6ea0e71) and this image installs no Julia, so
    # the single configured mode must come from the native reactive set.
    if len(modes) != 1:
        raise AssertionError(f"expected exactly one mode, got {len(modes)}: {modes!r}")
    mode = modes[0]
    if mode.get("class_name") not in NATIVE_REACTIVE_MODES:
        raise AssertionError(f"mode is not a native reactive mode: {mode!r}")
    if mode.get("class_name") != "ConstantVar":
        raise AssertionError(f"mode is not ConstantVar: {mode!r}")
    if "module_name" in mode and "modes.novel" in str(mode["module_name"]):
        raise AssertionError(f"mode comes from rt_control/modes/novel: {mode!r}")
    if mode.get("use_julia"):
        raise AssertionError(f"mode sets use_julia: {mode!r}")
    if mode.get("priority") != 1:
        raise AssertionError(f"priority is not 1: {mode!r}")
    if mode.get("reactive_power_target") != 10.0:
        raise AssertionError(f"reactive_power_target is not 10.0: {mode!r}")


def test_config_example_configures_one_native_constant_var_mode() -> None:
    config = _example_config()
    check_modes_are_one_native_constant_var(config["modes"])
    assert config["modes"] == [{"class_name": "ConstantVar", "priority": 1, "reactive_power_target": 10.0}]


GOOD_MODE = {"class_name": "ConstantVar", "priority": 1, "reactive_power_target": 10.0}


@pytest.mark.parametrize(
    "modes, message",
    [
        ([], "exactly one mode"),
        ([GOOD_MODE, GOOD_MODE], "exactly one mode"),
        ([{**GOOD_MODE, "class_name": "Pid"}], "not a native reactive mode"),
        ([{**GOOD_MODE, "class_name": "VoltVar"}], "not ConstantVar"),
        (
            [{**GOOD_MODE, "module_name": "rt_control.modes.novel.pid"}],
            "modes/novel",
        ),
        ([{**GOOD_MODE, "use_julia": True}], "use_julia"),
        ([{**GOOD_MODE, "priority": 2}], "priority is not 1"),
        ([{**GOOD_MODE, "reactive_power_target": 50.0}], "reactive_power_target"),
    ],
)
def test_check_modes_fixture_fails(modes: list[dict], message: str) -> None:
    # Control: each assertion above goes red on a config that breaks it (#97).
    with pytest.raises(AssertionError, match=message):
        check_modes_are_one_native_constant_var(modes)


def test_dockerfile_bakes_config_at_the_path_start_legacy_resolves() -> None:
    # start-legacy.py resolves --config relative to --agent-dir first
    # (rt_control/agent.py:191 at 16804d8 calls vip_main(RTControlAgent), so
    # RTControlAgent's config_path comes from that flag): the CMD must pass
    # "config.json" and the Dockerfile must copy the example to that exact
    # name under /agent, or the container starts with no config at all.
    dockerfile = (AGENT_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --chown=root:root config.example.json ./config.json" in dockerfile, dockerfile
    assert '"--config", "config.json"' in dockerfile, dockerfile


def test_dockerfile_names_the_der_rtc_identity() -> None:
    # The agent's own README installs it as der.rtc; the container's CMD
    # must register the same identity (#68).
    dockerfile = (AGENT_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert '"--identity", "der.rtc"' in dockerfile, dockerfile


def test_dockerfile_cmd_enables_debug_logging() -> None:
    # The periodic control tick (rt_control/agent.py's loop()) and the
    # scheduler's own per-event trace both log at DEBUG only
    # (src/derhost/client/agent.py:3258 at b748a1f); without --debug,
    # start-legacy.py's setup_logging() leaves the root logger at INFO and
    # no tick ever appears in the container's log (#68).
    dockerfile = (AGENT_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert '"--debug"' in dockerfile, dockerfile


# --- the revision stamp: compose build arg -> Dockerfile ARG -> LABEL ------


def _build_args(agent: str, revision: str | None) -> dict[str, str]:
    env = dict(os.environ, DER_AGENT_SRC="/dev/null/no-build")
    env.pop("AGENT_REVISION", None)
    if revision is not None:
        env["AGENT_REVISION"] = revision
    compose_file = REPO_ROOT / "docker" / agent / "docker-compose.yml"
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "config", "--format", "json"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return json.loads(result.stdout)["services"][f"derhost-{agent}"]["build"]["args"]


@pytest.mark.parametrize("agent", REVISION_AGENTS)
def test_compose_passes_the_exported_revision_through_as_a_build_arg(agent: str) -> None:
    assert _build_args(agent, "abc123") == {"AGENT_REVISION": "abc123"}


@pytest.mark.parametrize("agent", REVISION_AGENTS)
def test_compose_revision_build_arg_falls_back_to_unknown_when_unset(agent: str) -> None:
    # Control: a fixed value in the compose file would pass the test above
    # only by luck; the fallback shows the arg is read from the environment.
    assert _build_args(agent, None) == {"AGENT_REVISION": "unknown"}


def check_dockerfile_stamps_the_revision(text: str) -> None:
    lines = [line.strip() for line in text.splitlines()]
    label = "LABEL org.opencontainers.image.revision=${AGENT_REVISION}"
    if label not in lines:
        raise AssertionError(f"no {label!r} line")
    if "ARG AGENT_REVISION=unknown" not in lines[: lines.index(label)]:
        raise AssertionError("AGENT_REVISION is not declared by an ARG before the LABEL that uses it")


@pytest.mark.parametrize("agent", REVISION_AGENTS)
def test_dockerfile_labels_the_image_with_the_revision_arg(agent: str) -> None:
    check_dockerfile_stamps_the_revision((REPO_ROOT / "docker" / agent / "Dockerfile").read_text(encoding="utf-8"))


def test_check_dockerfile_stamps_the_revision_fixture_fails_without_the_label() -> None:
    with pytest.raises(AssertionError, match="no 'LABEL"):
        check_dockerfile_stamps_the_revision("ARG AGENT_REVISION=unknown\n")


def test_check_dockerfile_stamps_the_revision_fixture_fails_without_the_arg() -> None:
    # Control: a LABEL whose ARG is undeclared expands to an empty string.
    with pytest.raises(AssertionError, match="not declared by an ARG"):
        check_dockerfile_stamps_the_revision("LABEL org.opencontainers.image.revision=${AGENT_REVISION}\n")
