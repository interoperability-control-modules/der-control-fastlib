"""The layout rules over what `docker compose config` resolves (#75).

The raw-file rules in test_docker_layout.py read one YAML file at a time, so
an interpolated bind source, an `include` or an `extends` can reach a host
path they never see. Here the forbidding rules run over Compose's own
resolved model of each stack, alone and with its smoke overlay. `config`
needs no daemon.
"""

from __future__ import annotations

import copy
import json
import os
import posixpath
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_docker_layout import (
    check_cap_drop_all,
    check_named_volumes_only,
    check_no_cap_add,
    check_no_docker_socket,
    check_no_host_namespace,
    check_no_new_privileges,
    check_no_secret_env,
    check_not_privileged,
    check_top_level_sources,
    check_user_is_1000,
    discover_compose_files,
    discover_overlay_files,
    load_compose,
)

# Not here: `check_no_env_file` (Compose copies an env_file's variables into
# `environment` and prints no `env_file`, so the secret rule reads them), and
# `check_no_compose_references` (`config` merges `include` and `extends` and
# prints neither key; the merged services are what these rules read).
_RESOLVED_RULES = (
    check_no_docker_socket,
    check_named_volumes_only,
    check_user_is_1000,
    check_cap_drop_all,
    check_no_cap_add,
    check_not_privileged,
    check_no_host_namespace,
    check_no_new_privileges,
    check_no_secret_env,
)


def require_docker_compose(*, ci: bool) -> str:
    """The docker CLI path, or a skip (a fail in CI) when it cannot run `compose`."""
    # A skip in CI would let this check vanish if the runner image dropped the
    # CLI or its compose plugin, so CI fails instead; locally it only costs
    # this file.
    cli = shutil.which("docker")
    problem = "the docker CLI is required to resolve the compose stacks"
    if cli is not None:
        problem = "`docker compose version` failed: the compose plugin is required to resolve the compose stacks"
        try:
            probe = subprocess.run([cli, "compose", "version"], capture_output=True, timeout=30, check=False)  # noqa: S603
        except (OSError, subprocess.TimeoutExpired):
            pass
        else:
            if probe.returncode == 0:
                return cli
    if ci:
        pytest.fail(problem)
    pytest.skip(problem)


@pytest.fixture(scope="module")
def docker_cli() -> str:
    return require_docker_compose(ci=os.environ.get("CI") == "true")


def _config_argv(directory: Path, overlay: bool, env_file: Path | None) -> list[str]:
    # `--profile '*'` keeps a profiled service in the model: `config` drops it otherwise.
    argv = ["docker", "compose", "-p", "layout-check", "--profile", "*", "--project-directory", str(directory)]
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


def _relative_to_project(directory: Path, resolved: dict[str, Any]) -> dict[str, Any]:
    """The model with each secret and config `file` inside `directory` written relative.

    Compose prints `file: ./secrets/jwt` as an absolute path, which the
    top-level rule would refuse; one outside the project keeps its absolute or
    `..` form and is still refused.
    """
    model = copy.deepcopy(resolved)
    prefix = posixpath.normpath(str(directory)) + "/"
    for section in ("secrets", "configs"):
        for entry in (model.get(section) or {}).values():
            source = entry.get("file") if isinstance(entry, dict) else None
            if isinstance(source, str) and "$" not in source:
                normalized = posixpath.normpath(source)
                if normalized.startswith(prefix):
                    entry["file"] = normalized[len(prefix) :]
    return model


def check_resolved(
    path: Path,
    resolved: dict[str, Any],
    *,
    rules: tuple[Any, ...] = _RESOLVED_RULES,
    top_level: bool = True,
) -> None:
    """Every forbidding rule over a resolved model: its services, then its top level."""
    for name, service in (resolved.get("services") or {}).items():
        for rule in rules:
            rule(name, service)
    if top_level:
        check_top_level_sources(path, _relative_to_project(path.parent, resolved))


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


# One refusal per resolved rule, as Compose prints it. `rule` is the only rule
# that may refuse the shape: the attribution test below runs the shape without
# it, so dropping a rule from `_RESOLVED_RULES` turns its row red.
_TOP_LEVEL = check_top_level_sources
_ROOT_USER = _HARDENED.replace('"1000"', '"0"')
_OTHER_CAP_DROP = _HARDENED.replace("[ALL]", "[NET_ADMIN]")
_NO_NEW_PRIVILEGES_LINE = '    security_opt: ["no-new-privileges:true"]\n'
_WITHOUT_NO_NEW_PRIVILEGES = _HARDENED.replace(_NO_NEW_PRIVILEGES_LINE, "")
_RESOLVED_REFUSALS = [
    (
        check_no_docker_socket,
        f'services:\n  x:\n{_HARDENED}    volumes: ["docker.sock:/s"]\nvolumes:\n  docker.sock:\n',
        "Docker socket",
    ),
    (
        check_named_volumes_only,
        f'services:\n  x:\n{_HARDENED}    volumes: ["/etc:/e"]\n',
        "not a named volume",
    ),
    (check_user_is_1000, f"services:\n  x:\n{_ROOT_USER}", "user"),
    (check_cap_drop_all, f"services:\n  x:\n{_OTHER_CAP_DROP}", "cap_drop"),
    (check_no_cap_add, f"services:\n  x:\n{_HARDENED}    cap_add: [NET_ADMIN]\n", "cap_add"),
    (check_not_privileged, f"services:\n  x:\n{_HARDENED}    privileged: true\n", "privileged"),
    (check_no_host_namespace, f"services:\n  x:\n{_HARDENED}    pid: host\n", "pid: host"),
    (
        check_no_new_privileges,
        f"services:\n  x:\n{_WITHOUT_NO_NEW_PRIVILEGES}",
        "no-new-privileges",
    ),
    (
        check_no_secret_env,
        f"services:\n  x:\n{_HARDENED}    environment:\n      JWT_SECRET_KEY: not-a-real-secret\n",
        "JWT_SECRET_KEY",
    ),
    (
        _TOP_LEVEL,
        f"services:\n  x:\n{_HARDENED}    secrets: [s]\nsecrets:\n  s:\n    file: /etc/passwd\n",
        "plain relative path",
    ),
    (
        _TOP_LEVEL,
        f"services:\n  x:\n{_HARDENED}    secrets: [s]\nsecrets:\n  s:\n    file: ../outside/s\n",
        "plain relative path",
    ),
    (
        _TOP_LEVEL,
        f"services:\n  x:\n{_HARDENED}    secrets: [s]\nsecrets:\n  s:\n    file: ../stack-two/s\n",
        "plain relative path",
    ),
    (
        _TOP_LEVEL,
        f"services:\n  x:\n{_HARDENED}    configs: [c]\nconfigs:\n  c:\n    file: /etc/hostname\n",
        "plain relative path",
    ),
    (
        _TOP_LEVEL,
        f"services:\n  x:\n{_HARDENED}    volumes:\n      - vol:/data\nvolumes:\n  vol:\n    driver_opts: {{type: none, o: bind, device: /var/run}}\n",
        "driver_opts",
    ),
]
_REFUSAL_IDS = [f"{rule.__name__}-{index}" for index, (rule, _, _) in enumerate(_RESOLVED_REFUSALS)]


def test_every_resolved_rule_has_a_refusal_row() -> None:
    covered = {rule for rule, _, _ in _RESOLVED_REFUSALS}
    assert covered == {*_RESOLVED_RULES, _TOP_LEVEL}, covered ^ {*_RESOLVED_RULES, _TOP_LEVEL}


@pytest.mark.parametrize(("rule", "text", "needle"), _RESOLVED_REFUSALS, ids=_REFUSAL_IDS)
def test_each_resolved_rule_refuses_its_shape(
    docker_cli: str, tmp_path: Path, rule: Any, text: str, needle: str
) -> None:
    stack = _write_stack(tmp_path / "stack", text)
    resolved = resolved_stack(stack, False, tmp_path)
    with pytest.raises(AssertionError) as excinfo:
        check_resolved(stack / "docker-compose.yml", resolved)
    assert needle in str(excinfo.value)


@pytest.mark.parametrize(("rule", "text", "needle"), _RESOLVED_REFUSALS, ids=_REFUSAL_IDS)
def test_no_other_resolved_rule_refuses_that_shape(
    docker_cli: str, tmp_path: Path, rule: Any, text: str, needle: str
) -> None:
    # Control for the row above: without `rule` the shape passes, so the
    # refusal is that rule's and dropping the rule turns the row red.
    stack = _write_stack(tmp_path / "stack", text)
    resolved = resolved_stack(stack, False, tmp_path)
    others = tuple(other for other in _RESOLVED_RULES if other is not rule)
    check_resolved(stack / "docker-compose.yml", resolved, rules=others, top_level=rule is not _TOP_LEVEL)


_COMPOSE_SECRET = f"""services:
  x:
{_HARDENED}    environment:
      JWT_SECRET_KEY_FILE: /run/secrets/jwt
    secrets:
      - jwt
secrets:
  jwt:
    file: ./secrets/jwt
"""


def test_the_compose_secret_shape_passes_raw_and_resolved(docker_cli: str, tmp_path: Path) -> None:
    stack = _write_stack(tmp_path / "stack", _COMPOSE_SECRET)
    path = stack / "docker-compose.yml"
    raw = load_compose(path)
    check_no_secret_env("x", raw["services"]["x"])
    check_top_level_sources(path, raw)
    resolved = resolved_stack(stack, False, tmp_path)
    # Not vacuous: Compose prints the normalized forms the rules must accept.
    assert resolved["secrets"]["jwt"]["file"] == str(stack / "secrets" / "jwt"), resolved["secrets"]
    assert resolved["services"]["x"]["secrets"] == [{"source": "jwt", "target": "/run/secrets/jwt"}]
    check_resolved(path, resolved)


def test_a_profiled_service_is_resolved_and_checked(docker_cli: str, tmp_path: Path) -> None:
    text = f'services:\n  x:\n{_HARDENED}  p:\n{_HARDENED}    profiles: [debug]\n    volumes: ["/var/run:/r"]\n'
    stack = _write_stack(tmp_path / "stack", text)
    resolved = resolved_stack(stack, False, tmp_path)
    assert sorted(resolved["services"]) == ["p", "x"], resolved["services"]
    with pytest.raises(AssertionError, match="not a named volume"):
        check_resolved(stack / "docker-compose.yml", resolved)


def _fake_docker(directory: Path, exit_code: int | None) -> None:
    directory.mkdir()
    if exit_code is not None:
        stub = directory / "docker"
        stub.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
        stub.chmod(0o755)


def _outcome(ci: bool) -> tuple[str, str]:
    """How `require_docker_compose` ends: its outcome and message (a skip is not a pass here)."""
    try:
        return "returned", require_docker_compose(ci=ci)
    except pytest.skip.Exception as skipped:
        return "skipped", str(skipped)
    except pytest.fail.Exception as failed:
        return "failed", str(failed)


@pytest.mark.parametrize(
    ("exit_code", "needle"),
    [(None, "docker CLI is required"), (1, "compose plugin is required")],
    ids=["no-docker", "docker-without-compose"],
)
def test_a_missing_docker_or_compose_skips_outside_ci_and_fails_in_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int | None, needle: str
) -> None:
    # A stub `docker` first on PATH: the real binary is never reached.
    _fake_docker(tmp_path / "bin", exit_code)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    outside_ci, outside_message = _outcome(ci=False)
    assert outside_ci == "skipped" and needle in outside_message, (outside_ci, outside_message)
    in_ci, in_message = _outcome(ci=True)
    assert in_ci == "failed" and needle in in_message, (in_ci, in_message)


def test_a_docker_whose_compose_runs_is_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Control: the skips above come from the missing CLI or plugin.
    _fake_docker(tmp_path / "bin", 0)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert _outcome(ci=True) == ("returned", str(tmp_path / "bin" / "docker"))


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
