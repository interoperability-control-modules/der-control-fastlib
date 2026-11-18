"""Layout rules for every docker/*/docker-compose.yml (#68, #73).

Each rule is a pure check function asserted against every real compose file
found under docker/, plus a fixture test that proves the same check rejects
a violating service or document. Rules that harden a container (user,
capabilities, privilege escalation, restart policy, host-namespace and
host-mount denials, secret-shaped environment values) run over every service
in every discovered file, not only docker/server: a future agent compose
file gets the same checks with no extra wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKER_DIR = REPO_ROOT / "docker"

HEALTH_PATH = "/health"


def discover_compose_files() -> list[Path]:
    """Every docker/<name>/docker-compose.yml under the repo root."""
    return sorted(DOCKER_DIR.glob("*/docker-compose.yml"))


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that reads compose's `!override` and `!reset` merge tags as
    the plain value they wrap, since an overlay file may carry them."""


def _construct_merge_tag(loader: _ComposeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)  # type: ignore[arg-type]


_ComposeLoader.add_constructor("!override", _construct_merge_tag)
_ComposeLoader.add_constructor("!reset", _construct_merge_tag)


def load_compose(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_ComposeLoader)  # noqa: S506 - SafeLoader subclass


def _env_mapping(service: dict[str, Any]) -> dict[str, str]:
    """Normalize compose's two environment forms (mapping, or a KEY=VALUE list)."""
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    pairs: dict[str, str] = {}
    for entry in raw:
        key, _, value = str(entry).partition("=")
        pairs[key] = value
    return pairs


def _all_services() -> list[tuple[Path, str, dict[str, Any]]]:
    """(path, service name, service) for every service in every compose file."""
    entries: list[tuple[Path, str, dict[str, Any]]] = []
    for path in discover_compose_files():
        compose = load_compose(path)
        for name, service in compose["services"].items():
            entries.append((path, name, service))
    return entries


# --- Rule 1: only the server publishes a port. -----------------------------

SERVER_SERVICE = "derhost-server"
SERVER_PORT_MAPPING = "${DERHOST_PUBLISH_HOST:-127.0.0.1}:5410:8000"


def check_only_server_publishes_port(name: str, service: dict[str, Any]) -> None:
    if name == SERVER_SERVICE:
        ports = service.get("ports")
        if ports != [SERVER_PORT_MAPPING]:
            raise AssertionError(f"{name}: expected ports == [{SERVER_PORT_MAPPING!r}], got {ports!r}")
    elif "ports" in service:
        raise AssertionError(f"{name}: publishes a port; only {SERVER_SERVICE} may")


def test_only_server_publishes_port() -> None:
    for _, name, service in _all_services():
        check_only_server_publishes_port(name, service)


def test_only_server_publishes_port_fixture_fails() -> None:
    # Control: an agent service that publishes a port must be rejected.
    with pytest.raises(AssertionError, match="publishes a port"):
        check_only_server_publishes_port("derhost-some-agent", {"ports": ["127.0.0.1:9999:9999"]})


def test_server_internal_bind_is_all_interfaces() -> None:
    # The compose port mapping publishes 5410 -> 8000; the container itself
    # must listen on 0.0.0.0:8000 inside the network for that to reach it.
    dockerfile = (DOCKER_DIR / "server" / "Dockerfile").read_text(encoding="utf-8")
    assert '"--host", "0.0.0.0", "--port", "8000"' in dockerfile, dockerfile


# --- Rule 2: no Docker socket mount, and no /var/run or /run mount. ---------


def check_no_docker_socket(name: str, service: dict[str, Any]) -> None:
    for volume in service.get("volumes", []):
        if "docker.sock" in str(volume):
            raise AssertionError(f"{name}: mounts the Docker socket: {volume!r}")


def test_no_docker_socket_mount() -> None:
    for _, name, service in _all_services():
        check_no_docker_socket(name, service)


def test_no_docker_socket_mount_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="Docker socket"):
        check_no_docker_socket(SERVER_SERVICE, {"volumes": ["/var/run/docker.sock:/var/run/docker.sock:ro"]})


def check_no_var_run_mount(name: str, service: dict[str, Any]) -> None:
    for volume in service.get("volumes", []):
        host_path = str(volume).split(":", 1)[0]
        if host_path in ("/var/run", "/run") or host_path.startswith(("/var/run/", "/run/")):
            raise AssertionError(f"{name}: mounts a host runtime directory: {volume!r}")


def test_no_var_run_mount() -> None:
    for _, name, service in _all_services():
        check_no_var_run_mount(name, service)


def test_no_var_run_mount_fixture_fails() -> None:
    # /var/run is the Docker socket's parent directory: mounting it reaches
    # the socket without naming it.
    with pytest.raises(AssertionError, match="host runtime directory"):
        check_no_var_run_mount(SERVER_SERVICE, {"volumes": ["/var/run:/var/run"]})


# --- Rule 3: non-root user 1000, every service. ------------------------------


def check_user_is_1000(name: str, service: dict[str, Any]) -> None:
    if str(service.get("user")) != "1000":
        raise AssertionError(f"{name}: expected user: \"1000\", got {service.get('user')!r}")


def test_user_is_1000() -> None:
    for _, name, service in _all_services():
        check_user_is_1000(name, service)


def test_user_is_1000_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="user"):
        check_user_is_1000(SERVER_SERVICE, {"user": "0"})


# --- Rule 4: cap_drop ALL, every service. ------------------------------------


def check_cap_drop_all(name: str, service: dict[str, Any]) -> None:
    if "ALL" not in (service.get("cap_drop") or []):
        raise AssertionError(f"{name}: expected cap_drop to include ALL, got {service.get('cap_drop')!r}")


def test_cap_drop_all() -> None:
    for _, name, service in _all_services():
        check_cap_drop_all(name, service)


def test_cap_drop_all_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="cap_drop"):
        check_cap_drop_all(SERVER_SERVICE, {"cap_drop": ["NET_ADMIN"]})


# --- Rule 4b: no cap_add, every service. -------------------------------------


def check_no_cap_add(name: str, service: dict[str, Any]) -> None:
    if service.get("cap_add"):
        raise AssertionError(f"{name}: cap_add must not be set, got {service.get('cap_add')!r}")


def test_no_cap_add() -> None:
    for _, name, service in _all_services():
        check_no_cap_add(name, service)


def test_no_cap_add_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="cap_add"):
        check_no_cap_add(SERVER_SERVICE, {"cap_add": ["NET_ADMIN"]})


# --- Rule 4c: never privileged, every service. -------------------------------


def check_not_privileged(name: str, service: dict[str, Any]) -> None:
    if service.get("privileged"):
        raise AssertionError(f"{name}: privileged must not be set")


def test_not_privileged() -> None:
    for _, name, service in _all_services():
        check_not_privileged(name, service)


def test_not_privileged_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="privileged"):
        check_not_privileged(SERVER_SERVICE, {"privileged": True})


# --- Rule 4d: no host network or PID namespace, every service. --------------


def check_no_host_namespace(name: str, service: dict[str, Any]) -> None:
    if service.get("network_mode") == "host":
        raise AssertionError(f"{name}: network_mode: host must not be set")
    if service.get("pid") == "host":
        raise AssertionError(f"{name}: pid: host must not be set")


def test_no_host_namespace() -> None:
    for _, name, service in _all_services():
        check_no_host_namespace(name, service)


def test_no_host_namespace_fixture_fails_network() -> None:
    with pytest.raises(AssertionError, match="network_mode"):
        check_no_host_namespace(SERVER_SERVICE, {"network_mode": "host"})


def test_no_host_namespace_fixture_fails_pid() -> None:
    with pytest.raises(AssertionError, match="pid"):
        check_no_host_namespace(SERVER_SERVICE, {"pid": "host"})


# --- Rule 4e: no env_file, every service. ------------------------------------


def check_no_env_file(name: str, service: dict[str, Any]) -> None:
    if "env_file" in service:
        raise AssertionError(f"{name}: env_file is not allowed; use environment or a compose secret")


def test_no_env_file() -> None:
    for _, name, service in _all_services():
        check_no_env_file(name, service)


def test_no_env_file_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="env_file"):
        check_no_env_file(SERVER_SERVICE, {"env_file": [".env"]})


# --- Rule 5: no-new-privileges, every service. -------------------------------


def check_no_new_privileges(name: str, service: dict[str, Any]) -> None:
    if "no-new-privileges:true" not in (service.get("security_opt") or []):
        raise AssertionError(f"{name}: expected security_opt to include no-new-privileges:true")


def test_no_new_privileges() -> None:
    for _, name, service in _all_services():
        check_no_new_privileges(name, service)


def test_no_new_privileges_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="no-new-privileges"):
        check_no_new_privileges(SERVER_SERVICE, {"security_opt": []})


# --- Rule 6: restart unless-stopped, every service. --------------------------


def check_restart_unless_stopped(name: str, service: dict[str, Any]) -> None:
    if service.get("restart") != "unless-stopped":
        raise AssertionError(f"{name}: expected restart: unless-stopped, got {service.get('restart')!r}")


def test_restart_unless_stopped() -> None:
    for _, name, service in _all_services():
        check_restart_unless_stopped(name, service)


def test_restart_unless_stopped_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="restart"):
        check_restart_unless_stopped(SERVER_SERVICE, {"restart": "always"})


# --- Rule 7: /health healthcheck on the server (not the root stack's /health/).

# The server is the only service that fronts an HTTP health endpoint today;
# this rule stays scoped to it rather than looping over every service.


def check_health_endpoint(name: str, service: dict[str, Any]) -> None:
    test = service.get("healthcheck", {}).get("test", [])
    joined = " ".join(str(t) for t in test)
    if HEALTH_PATH not in joined:
        raise AssertionError(f"{name}: healthcheck does not target {HEALTH_PATH}: {test!r}")
    if HEALTH_PATH + "/" in joined:
        raise AssertionError(f"{name}: healthcheck targets the root stack's /health/, not {HEALTH_PATH}")


def test_server_health_endpoint() -> None:
    compose = load_compose(DOCKER_DIR / "server" / "docker-compose.yml")
    check_health_endpoint(SERVER_SERVICE, compose["services"][SERVER_SERVICE])


def test_health_endpoint_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="/health"):
        check_health_endpoint(SERVER_SERVICE, {"healthcheck": {"test": ["CMD", "curl", "-f", "http://localhost:8000/status"]}})


def test_health_endpoint_fixture_rejects_trailing_slash() -> None:
    # Control: the root stack's own healthcheck (/health/) must not pass here.
    with pytest.raises(AssertionError, match="root stack"):
        check_health_endpoint(SERVER_SERVICE, {"healthcheck": {"test": ["CMD", "curl", "-f", "http://localhost:8000/health/"]}})


# --- Rule 8: compose name derhost-<dir>. -------------------------------------


def check_compose_name(path: Path, compose: dict[str, Any]) -> None:
    expected = f"derhost-{path.parent.name}"
    if compose.get("name") != expected:
        raise AssertionError(f"{path}: expected name: {expected}, got {compose.get('name')!r}")


def test_compose_name_matches_directory() -> None:
    for path in discover_compose_files():
        check_compose_name(path, load_compose(path))


def test_compose_name_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="expected name"):
        check_compose_name(DOCKER_DIR / "server" / "docker-compose.yml", {"name": "wrong-name"})


# --- Rule 9: service and container names are derhost-<dir>. -----------------


def check_service_and_container_name(path: Path, compose: dict[str, Any]) -> None:
    expected = f"derhost-{path.parent.name}"
    services = compose["services"]
    if expected not in services:
        raise AssertionError(f"{path}: expected a service named {expected}, got {list(services)!r}")
    if services[expected].get("container_name") != expected:
        raise AssertionError(f"{path}: expected container_name: {expected}, got {services[expected].get('container_name')!r}")


def test_service_and_container_name() -> None:
    for path in discover_compose_files():
        check_service_and_container_name(path, load_compose(path))


def test_service_and_container_name_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="container_name"):
        check_service_and_container_name(
            DOCKER_DIR / "server" / "docker-compose.yml",
            {"services": {SERVER_SERVICE: {"container_name": "something-else"}}},
        )


# --- Rule 10: derhost-net is owned by server, external everywhere else. -----


def check_network_declaration(path: Path, compose: dict[str, Any]) -> None:
    networks = compose.get("networks") or {}
    if "derhost-net" not in networks:
        raise AssertionError(f"{path}: expected a derhost-net network declaration")
    is_external = bool((networks["derhost-net"] or {}).get("external"))
    if path.parent.name == "server":
        if is_external:
            raise AssertionError("derhost-net must be owned (not external) in the server compose file")
    elif not is_external:
        raise AssertionError(f"{path}: derhost-net must be declared external: true outside the server compose file")


def test_network_declared_correctly() -> None:
    for path in discover_compose_files():
        check_network_declaration(path, load_compose(path))


def test_network_owned_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="external"):
        check_network_declaration(
            DOCKER_DIR / "server" / "docker-compose.yml",
            {"networks": {"derhost-net": {"external": True}}},
        )


def test_network_external_fixture_fails_for_agent() -> None:
    with pytest.raises(AssertionError, match="external"):
        check_network_declaration(
            DOCKER_DIR / "some-agent" / "docker-compose.yml",
            {"networks": {"derhost-net": {}}},
        )


# --- Rule 11: no secret-shaped key in an environment value. ------------------

_SECRET_KEY_MARKERS = ("SECRET", "PASSWORD", "TOKEN", "PRIVATE_KEY", "API_KEY", "CREDENTIAL")


def _looks_like_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in _SECRET_KEY_MARKERS)


def check_no_secret_env(name: str, service: dict[str, Any]) -> None:
    for key in _env_mapping(service):
        if _looks_like_secret_key(key):
            raise AssertionError(f"{name}: {key} must not be a literal environment value")


def test_no_secret_env() -> None:
    for _, name, service in _all_services():
        check_no_secret_env(name, service)


def test_no_secret_env_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="JWT_SECRET_KEY"):
        check_no_secret_env(SERVER_SERVICE, {"environment": {"JWT_SECRET_KEY": "not-a-real-secret"}})


def test_no_secret_env_fixture_fails_list_form() -> None:
    # Control: the list form (- KEY=VALUE) must be caught too, not just dict form.
    with pytest.raises(AssertionError, match="JWT_SECRET_KEY"):
        check_no_secret_env(SERVER_SERVICE, {"environment": ["JWT_SECRET_KEY=not-a-real-secret"]})


def test_no_secret_env_fixture_fails_password() -> None:
    # A differently named secret must be caught too, not only JWT_SECRET_KEY.
    with pytest.raises(AssertionError, match="DB_PASSWORD"):
        check_no_secret_env(SERVER_SERVICE, {"environment": {"DB_PASSWORD": "not-a-real-secret"}})


# --- Regression: the review's agent mutant (M11) must be rejected. ----------


def test_agent_mutant_m11_is_rejected() -> None:
    # Review finding F1 (#73): an agent compose file that weakens hardening
    # and reuses derhost-net without declaring it external must be caught by
    # the same rules the server file is held to, with no per-file wiring.
    path = DOCKER_DIR / "some-agent" / "docker-compose.yml"
    service: dict[str, Any] = {
        "container_name": "derhost-some-agent",
        "user": "0",
        "privileged": True,
        "restart": "unless-stopped",
        "security_opt": ["no-new-privileges:true"],
    }
    compose: dict[str, Any] = {
        "name": "derhost-some-agent",
        "services": {"derhost-some-agent": service},
        "networks": {"derhost-net": {}},
    }
    with pytest.raises(AssertionError, match="user"):
        check_user_is_1000("derhost-some-agent", service)
    with pytest.raises(AssertionError, match="privileged"):
        check_not_privileged("derhost-some-agent", service)
    with pytest.raises(AssertionError, match="cap_drop"):
        check_cap_drop_all("derhost-some-agent", service)
    with pytest.raises(AssertionError, match="external"):
        check_network_declaration(path, compose)


# --- Rule 12: smoke overlays (#95) are held to the hardening rules too. -----
#
# An overlay is partial by design (it inherits user, cap_drop and the rest
# from its base file), so only the rules that forbid a setting apply to it,
# plus the naming rules that keep a smoke run disjoint from a developer's
# stack-up objects.

SMOKE_NETWORK = "derhost-smoke-net"
SERVER_OVERLAY_PORTS = ["${DERHOST_PUBLISH_HOST:-127.0.0.1}::8000"]
_FORBIDDING_RULES = (
    check_no_docker_socket,
    check_no_var_run_mount,
    check_no_cap_add,
    check_not_privileged,
    check_no_host_namespace,
    check_no_env_file,
    check_no_secret_env,
)


# An overlay may set only the keys it needs to rename the project: `image`
# and `container_name`, and `ports` on the server. Any other service key, set
# or `!reset`, is refused. Listing the keys to forbid cannot keep up with what
# a base file hardens with (limits, tmpfs, healthcheck, build contexts), and an
# overlay has no reason to touch any of them. A `!reset []` loads as its plain
# value, so key presence is what is checked.
_OVERLAY_ALLOWED_KEYS = ("image", "container_name")
_SERVER_OVERLAY_ALLOWED_KEYS = (*_OVERLAY_ALLOWED_KEYS, "ports")


def check_overlay_sets_only_allowed_keys(name: str, service: dict[str, Any], allowed: tuple[str, ...]) -> None:
    for key in service:
        if key not in allowed:
            raise AssertionError(
                f"{name}: an overlay may not set or reset {key}, got {service[key]!r}; allowed keys: {', '.join(allowed)}"
            )


def discover_overlay_files() -> list[Path]:
    """Every docker/<name>/compose.smoke.yml under the repo root."""
    return sorted(DOCKER_DIR.glob("*/compose.smoke.yml"))


def check_overlay(path: Path, compose: dict[str, Any]) -> None:
    directory = path.parent.name
    is_server = directory == "server"
    for name, service in compose.get("services", {}).items():
        for rule in _FORBIDDING_RULES:
            rule(name, service)
        expected_container = "derhost-smoke-server" if is_server else f"derhost-smoke-{directory}"
        if service.get("container_name") != expected_container:
            raise AssertionError(
                f"{path}: expected container_name: {expected_container}, got {service.get('container_name')!r}"
            )
        if not str(service.get("image", "")).endswith(":smoke"):
            raise AssertionError(f"{path}: image must carry the :smoke tag, got {service.get('image')!r}")
        if is_server:
            if service.get("ports") != SERVER_OVERLAY_PORTS:
                raise AssertionError(
                    f"{name}: expected ports == {SERVER_OVERLAY_PORTS!r}, got {service.get('ports')!r}"
                )
        elif "ports" in service:
            raise AssertionError(f"{name}: publishes a port; only {SERVER_SERVICE} may")
        allowed = _SERVER_OVERLAY_ALLOWED_KEYS if is_server else _OVERLAY_ALLOWED_KEYS
        check_overlay_sets_only_allowed_keys(name, service, allowed)
    if not is_server and compose.get("name") != f"derhost-smoke-{directory}":
        raise AssertionError(f"{path}: expected name: derhost-smoke-{directory}, got {compose.get('name')!r}")
    network = (compose.get("networks") or {}).get("derhost-net") or {}
    if network.get("name") != SMOKE_NETWORK:
        raise AssertionError(f"{path}: derhost-net must be renamed to {SMOKE_NETWORK}, got {network.get('name')!r}")
    if not is_server and not network.get("external"):
        raise AssertionError(f"{path}: derhost-net must stay external: true outside the server overlay")
    if is_server and network.get("external"):
        raise AssertionError(f"{path}: the server overlay owns {SMOKE_NETWORK}; it must not be external")


def test_every_agent_has_a_smoke_overlay_and_the_rules_hold_on_each() -> None:
    overlays = discover_overlay_files()
    # Not vacuous: the server's overlay plus one per agent directory.
    assert [p.parent.name for p in overlays] == ["interoperability-service", "realtime-control-agent", "server"], (
        overlays
    )
    for path in overlays:
        check_overlay(path, load_compose(path))


def test_overlay_loader_reads_the_override_tag_as_its_plain_value() -> None:
    compose = load_compose(DOCKER_DIR / "server" / "compose.smoke.yml")
    assert compose["services"][SERVER_SERVICE]["ports"] == SERVER_OVERLAY_PORTS


def _agent_overlay_text() -> str:
    return (DOCKER_DIR / "realtime-control-agent" / "compose.smoke.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        ("{text}\n    privileged: true\n", "privileged"),
        ("{text}\n    cap_add: [NET_ADMIN]\n", "cap_add"),
        ("{text}\n    ports: ['127.0.0.1:9999:9999']\n", "publishes a port"),
        ("{text}\n    volumes: ['/var/run/docker.sock:/var/run/docker.sock']\n", "Docker socket"),
        ("{text}\n    network_mode: host\n", "network_mode"),
        ("{text}\n    user: '0'\n", "user"),
        ("{text}\n    cap_drop: !reset []\n", "cap_drop"),
        ("{text}\n    security_opt: !reset []\n", "security_opt"),
        ("{text}\n    read_only: false\n", "read_only"),
        ("{text}\n    devices: ['/dev/mem:/dev/mem']\n", "devices"),
        ("{text}\n    pids_limit: -1\n", "pids_limit"),
        ("{text}\n    mem_limit: !reset null\n", "mem_limit"),
        ("{text}\n    tmpfs: !reset []\n", "tmpfs"),
        ("{text}\n    restart: always\n", "restart"),
        ("{text}\n    healthcheck: {{disable: true}}\n", "healthcheck"),
        ("{text}\n    build: {{additional_contexts: {{agent_src: /srv/live}}}}\n", "build"),
    ],
)
def test_a_violating_overlay_file_fails_the_overlay_rules(tmp_path: Path, mutation: str, needle: str) -> None:
    # `{text}` ends inside the service block, so each mutation appends one
    # more key to the service.
    text = _agent_overlay_text().split("\nnetworks:")[0].rstrip("\n")
    overlay = tmp_path / "realtime-control-agent" / "compose.smoke.yml"
    overlay.parent.mkdir()
    network_block = "\n\nnetworks:" + _agent_overlay_text().split("\nnetworks:")[1]
    overlay.write_text(mutation.format(text=text) + network_block, encoding="utf-8")
    with pytest.raises(AssertionError, match=needle):
        check_overlay(overlay, load_compose(overlay))


def test_the_overlay_that_weakens_user_cap_drop_and_security_opt_together_fails(tmp_path: Path) -> None:
    # The overlay that merged to user 0 with no capability drop and no
    # no-new-privileges, which every per-key rule above let through.
    text = _agent_overlay_text().split("\nnetworks:")[0].rstrip("\n")
    network_block = "\n\nnetworks:" + _agent_overlay_text().split("\nnetworks:")[1]
    overlay = tmp_path / "realtime-control-agent" / "compose.smoke.yml"
    overlay.parent.mkdir()
    weakening = '\n    user: "0"\n    cap_drop: !reset []\n    security_opt: !reset []\n'
    overlay.write_text(text + weakening + network_block, encoding="utf-8")
    loaded = load_compose(overlay)["services"]["derhost-realtime-control-agent"]
    assert (loaded["user"], loaded["cap_drop"], loaded["security_opt"]) == ("0", [], [])
    with pytest.raises(AssertionError, match="may not set or reset user"):
        check_overlay(overlay, load_compose(overlay))


def test_the_overlay_that_weakens_pids_limit_mem_limit_and_tmpfs_together_fails(tmp_path: Path) -> None:
    # The overlay that merged to pids_limit -1, no memory limit and no tmpfs,
    # none of which the per-key hardening list named.
    text = _agent_overlay_text().split("\nnetworks:")[0].rstrip("\n")
    network_block = "\n\nnetworks:" + _agent_overlay_text().split("\nnetworks:")[1]
    overlay = tmp_path / "realtime-control-agent" / "compose.smoke.yml"
    overlay.parent.mkdir()
    weakening = "\n    pids_limit: -1\n    mem_limit: !reset null\n    tmpfs: !reset []\n"
    overlay.write_text(text + weakening + network_block, encoding="utf-8")
    with pytest.raises(AssertionError, match="may not set or reset pids_limit"):
        check_overlay(overlay, load_compose(overlay))


def test_the_server_overlay_may_set_ports_and_no_other_hardening_key(tmp_path: Path) -> None:
    # `ports` is the one key the server overlay adds to the two naming keys;
    # the same file with a limit reset on top of it is refused by that key.
    server_text = (DOCKER_DIR / "server" / "compose.smoke.yml").read_text(encoding="utf-8")
    overlay = tmp_path / "server" / "compose.smoke.yml"
    overlay.parent.mkdir()
    overlay.write_text(server_text, encoding="utf-8")
    check_overlay(overlay, load_compose(overlay))
    marker = "    container_name: derhost-smoke-server\n"
    assert marker in server_text
    overlay.write_text(server_text.replace(marker, marker + "    mem_limit: !reset null\n"), encoding="utf-8")
    with pytest.raises(AssertionError, match="may not set or reset mem_limit"):
        check_overlay(overlay, load_compose(overlay))


def test_overlay_that_drops_its_network_rename_fails_the_overlay_rules(tmp_path: Path) -> None:
    overlay = tmp_path / "realtime-control-agent" / "compose.smoke.yml"
    overlay.parent.mkdir()
    overlay.write_text(_agent_overlay_text().replace("name: derhost-smoke-net\n    ", ""), encoding="utf-8")
    with pytest.raises(AssertionError, match="renamed"):
        check_overlay(overlay, load_compose(overlay))


def test_overlay_naming_fixtures_fail() -> None:
    path = DOCKER_DIR / "realtime-control-agent" / "compose.smoke.yml"
    good = load_compose(path)
    service = good["services"]["derhost-realtime-control-agent"]
    renamed_container = {
        **good,
        "services": {"derhost-realtime-control-agent": {**service, "container_name": "derhost-realtime-control-agent"}},
    }
    with pytest.raises(AssertionError, match="container_name"):
        check_overlay(path, renamed_container)
    local_tag = {
        **good,
        "services": {"derhost-realtime-control-agent": {**service, "image": "derhost-realtime-control-agent:local"}},
    }
    with pytest.raises(AssertionError, match=":smoke"):
        check_overlay(path, local_tag)
    with pytest.raises(AssertionError, match="expected name"):
        check_overlay(path, {**good, "name": "derhost-realtime-control-agent"})
