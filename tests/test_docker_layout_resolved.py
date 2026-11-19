"""The layout rules over what `docker compose config` resolves (#75).

The raw-file rules in test_docker_layout.py read one YAML file at a time, so
an interpolated bind source, an `include` or an `extends` can reach a host
path they never see. Here the forbidding rules run over Compose's own
resolved model of each stack, alone and with its smoke overlay. `config`
needs no daemon.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_docker_layout import (
    check_cap_drop_all,
    check_named_volumes_only,
    check_no_cap_add,
    check_no_compose_references,
    check_no_docker_socket,
    check_no_env_file,
    check_no_host_namespace,
    check_no_new_privileges,
    check_no_secret_env,
    check_not_privileged,
    check_top_level_sources,
    check_user_is_1000,
    discover_compose_files,
    discover_overlay_files,
)

_RESOLVED_RULES = (
    check_no_docker_socket,
    check_named_volumes_only,
    check_user_is_1000,
    check_cap_drop_all,
    check_no_cap_add,
    check_not_privileged,
    check_no_host_namespace,
    check_no_new_privileges,
    check_no_env_file,
    check_no_secret_env,
)


@pytest.fixture(scope="module")
def docker_cli() -> str:
    # A skip in CI would let this check vanish if the runner image dropped the
    # CLI, so CI fails instead; locally a missing CLI only costs this file.
    cli = shutil.which("docker")
    if cli is None:
        if os.environ.get("CI") == "true":
            pytest.fail("the docker CLI is required in CI to resolve the compose stacks")
        pytest.skip("requires the docker CLI")
    return cli


def _config_argv(directory: Path, overlay: bool, env_file: Path | None) -> list[str]:
    argv = ["docker", "compose", "-p", "layout-check", "--project-directory", str(directory)]
    argv += ["-f", str(directory / "docker-compose.yml")]
    if overlay:
        argv += ["-f", str(directory / "compose.smoke.yml")]
    if env_file is not None:
        argv += ["--env-file", str(env_file)]
    return [*argv, "config", "--format", "json"]


def resolved_stack(directory: Path, overlay: bool, tmp_path: Path, *, use_env_file: bool = True) -> dict[str, Any]:
    """`docker compose config` of one stack, with only PATH, HOME and DER_AGENT_SRC in its environment."""
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "DER_AGENT_SRC": str(tmp_path),
    }
    argv = _config_argv(directory, overlay, empty_env if use_env_file else None)
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
    if result.returncode != 0:
        raise AssertionError(f"{' '.join(argv)} exited {result.returncode}: {result.stderr.strip()}")
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def check_resolved(path: Path, resolved: dict[str, Any]) -> None:
    """Every forbidding rule over a resolved model: its services, then its top level."""
    for name, service in (resolved.get("services") or {}).items():
        for rule in _RESOLVED_RULES:
            rule(name, service)
    check_top_level_sources(path, resolved)
    check_no_compose_references(path, resolved)


_REAL_STACKS = [
    pytest.param(path.parent, overlay, id=f"{path.parent.name}-{'overlay' if overlay else 'alone'}")
    for path in discover_compose_files()
    for overlay in (False, True)
]


def test_there_are_six_real_resolutions() -> None:
    # Not vacuous: 3 stacks, each alone and with its overlay.
    assert len(_REAL_STACKS) == 6, _REAL_STACKS
    assert len(discover_overlay_files()) == 3


@pytest.mark.parametrize(("directory", "overlay"), _REAL_STACKS)
def test_the_rules_hold_on_every_resolved_real_stack(
    docker_cli: str, tmp_path: Path, directory: Path, overlay: bool
) -> None:
    resolved = resolved_stack(directory, overlay, tmp_path)
    assert resolved["services"], resolved
    check_resolved(directory / "docker-compose.yml", resolved)


def _write_stack(directory: Path, services: str, extra: dict[str, str] | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "docker-compose.yml").write_text(services, encoding="utf-8")
    for name, text in (extra or {}).items():
        (directory / name).write_text(text, encoding="utf-8")
    return directory


_HARDENED = """    image: example:1
    user: "1000"
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
"""

# Three shapes that pass a read of docker-compose.yml alone and each
# reach the host once Compose has merged, resolved or interpolated it.
_LEON_SHAPES = [
    pytest.param(
        "include:\n  - other.yml\n",
        {"other.yml": f"services:\n  x:\n{_HARDENED}    pid: host\n"},
        "pid: host",
        id="include-sets-pid-host",
    ),
    pytest.param(
        "services:\n  x:\n    extends:\n      file: base.yml\n      service: b\n    image: example:1\n",
        {"base.yml": f"services:\n  b:\n{_HARDENED}    privileged: true\n"},
        "privileged",
        id="extends-file-sets-privileged",
    ),
    pytest.param(
        f"services:\n  x:\n{_HARDENED}    volumes:\n      - ${{D:-/var/run}}:/a\n",
        {},
        "not a named volume",
        id="interpolated-bind-defaults-to-var-run",
    ),
]


@pytest.mark.parametrize(("text", "extra", "needle"), _LEON_SHAPES)
def test_a_stack_that_reaches_the_host_only_after_resolution_is_refused(
    docker_cli: str, tmp_path: Path, text: str, extra: dict[str, str], needle: str
) -> None:
    stack = _write_stack(tmp_path / "stack", text, extra)
    resolved = resolved_stack(stack, False, tmp_path)
    with pytest.raises(AssertionError) as excinfo:
        check_resolved(stack / "docker-compose.yml", resolved)
    assert needle in str(excinfo.value)


def test_a_hardened_tmp_stack_passes_the_resolved_rules(docker_cli: str, tmp_path: Path) -> None:
    # Control: the refusals above come from the shapes, not from the fixture.
    stack = _write_stack(tmp_path / "stack", f"services:\n  x:\n{_HARDENED}")
    check_resolved(stack / "docker-compose.yml", resolved_stack(stack, False, tmp_path))


def test_a_dotenv_beside_a_stack_does_not_change_the_resolution(docker_cli: str, tmp_path: Path) -> None:
    stack = _write_stack(
        tmp_path / "stack",
        f"services:\n  x:\n{_HARDENED}    environment:\n      MODE: ${{MODE:-default}}\n",
        {".env": "MODE=from-dotenv\n"},
    )
    pinned = resolved_stack(stack, False, tmp_path)
    assert pinned["services"]["x"]["environment"]["MODE"] == "default"
    # Control: without --env-file Compose does read the .env, so the pinned
    # result above is the flag's doing.
    unpinned = resolved_stack(stack, False, tmp_path, use_env_file=False)
    assert unpinned["services"]["x"]["environment"]["MODE"] == "from-dotenv"
