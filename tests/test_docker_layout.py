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

import posixpath
import re
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


# Docker Compose reads compose.yaml, compose.yml, docker-compose.yaml,
# docker-compose.yml and an override sitting beside any of them, and `-f` can
# name any other file. Only the two names the rules below load are accepted;
# a compose-shaped file under any other name would be read by Docker and
# checked by nothing.
_ACCEPTED_COMPOSE_NAMES = ("docker-compose.yml", "compose.smoke.yml")


def _is_compose_shaped(name: str) -> bool:
    lowered = name.lower()
    if not lowered.endswith((".yml", ".yaml")):
        return False
    return "compose" in lowered or ".override." in lowered


def check_compose_file_names(root: Path) -> list[Path]:
    """Return the accepted compose files under root; refuse any other compose-shaped name."""
    accepted: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not _is_compose_shaped(path.name):
            continue
        if path.parent.parent == root and path.name in _ACCEPTED_COMPOSE_NAMES:
            accepted.append(path)
        else:
            raise AssertionError(
                f"{path}: compose file name not accepted; only docker/<name>/"
                f"{' and docker/<name>/'.join(_ACCEPTED_COMPOSE_NAMES)} are checked"
            )
    return accepted


# Docker reads the repo root's docker-compose.yml by default. The root file is
# the generated legacy stack, which mounts the Docker socket on purpose, so it
# is accepted by name only while it carries its generator's header, and no
# other rule runs over it. Retiring or hardening it is tracked in #145.
_GENERATED_ROOT_HEADER = "# Generated docker-compose.yml"


def check_root_compose_names(root: Path) -> list[Path]:
    """Return the root's accepted compose file; refuse any other compose-shaped name at its top level."""
    accepted: list[Path] = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or not _is_compose_shaped(path.name):
            continue
        if path.name != "docker-compose.yml":
            raise AssertionError(f"{path}: compose file name not accepted at the repo root; only docker-compose.yml is")
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0] != _GENERATED_ROOT_HEADER:
            raise AssertionError(
                f"{path}: the root docker-compose.yml must open with the generated header {_GENERATED_ROOT_HEADER!r}"
            )
        accepted.append(path)
    return accepted


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


# --- Rule 2: no Docker socket, and every mount source is a named volume or tmpfs. ---


def check_no_docker_socket(name: str, service: dict[str, Any]) -> None:
    for volume in service.get("volumes") or []:
        if "docker.sock" in str(volume):
            raise AssertionError(f"{name}: mounts the Docker socket: {volume!r}")


def test_no_docker_socket_mount() -> None:
    for _, name, service in _all_services():
        check_no_docker_socket(name, service)


# A compose volume name. Anything else in the source slot (a path, `.`, `~`,
# `$`) is a host path or an interpolation that could become one.
_VOLUME_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")


def _is_volume_name(source: Any) -> bool:
    return _VOLUME_NAME.fullmatch(str(source)) is not None


# `c:/etc:/x` splits at its first colon into the name `c`, but Compose reads
# a drive letter followed by a slash as a host path, and `c:etc:/y` as a bind
# of `<project>/c:etc`. A one-letter source followed by a colon is refused
# whatever follows it.
_DRIVE_PATH = re.compile(r"[a-zA-Z]:")


def check_named_volumes_only(name: str, service: dict[str, Any]) -> None:
    if service.get("volumes_from"):
        raise AssertionError(f"{name}: volumes_from inherits another service's mounts: {service['volumes_from']!r}")
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            allowed = volume.get("type") == "tmpfs" or (
                volume.get("type") == "volume" and _is_volume_name(volume.get("source") or "")
            )
        else:
            allowed = _DRIVE_PATH.match(str(volume)) is None and _is_volume_name(str(volume).split(":", 1)[0])
        if not allowed:
            raise AssertionError(f"{name}: mount source is not a named volume or tmpfs: {volume!r}")


def check_top_level_sources(path: Path, compose: dict[str, Any]) -> None:
    for section in ("secrets", "configs"):
        for key, entry in (compose.get(section) or {}).items():
            source = entry.get("file") if isinstance(entry, dict) else None
            if source is None:
                continue
            normalized = posixpath.normpath(str(source))
            if (
                "$" in str(source)
                or str(source).startswith("~")
                or posixpath.isabs(normalized)
                or (normalized == ".." or normalized.startswith("../"))
            ):
                raise AssertionError(
                    f"{path}: {section} {key}: file {source!r} must be a plain relative path inside the project"
                )
    for key, entry in (compose.get("volumes") or {}).items():
        if isinstance(entry, dict) and entry.get("driver_opts"):
            raise AssertionError(f"{path}: volume {key} sets driver_opts, which can bind a host path: {entry!r}")


def check_no_compose_references(path: Path, compose: dict[str, Any]) -> None:
    """Refuse `include` and `extends`: they pull in files this reading never sees."""
    if "include" in compose:
        raise AssertionError(f"{path}: include pulls in files the layout rules do not read: {compose['include']!r}")
    for name, service in (compose.get("services") or {}).items():
        if isinstance(service, dict) and "extends" in service:
            raise AssertionError(
                f"{path}: {name}: extends pulls in a service the layout rules do not read: {service['extends']!r}"
            )


def test_mount_sources_are_named_volumes_or_tmpfs() -> None:
    for _, name, service in _all_services():
        check_named_volumes_only(name, service)


def test_no_docker_socket_mount_fixture_fails() -> None:
    with pytest.raises(AssertionError, match="Docker socket"):
        check_no_docker_socket(SERVER_SERVICE, {"volumes": ["/var/run/docker.sock:/var/run/docker.sock:ro"]})


@pytest.mark.parametrize(
    "volume",
    [
        {"type": "bind", "source": "/var/run/docker.sock", "target": "/x"},
        "/var/./run/docker.sock:/s",
        "/run/../var/run/docker.sock:/s:ro",
        "/srv/docker.sock:/s",
    ],
)
def test_docker_socket_mounts_fail_in_both_volume_forms(volume: Any) -> None:
    with pytest.raises(AssertionError, match="Docker socket") as excinfo:
        check_no_docker_socket(SERVER_SERVICE, {"volumes": [volume]})
    assert repr(volume) in str(excinfo.value)


# Every mount source must be a compose volume name or a tmpfs. A path, a
# relative path, `~` or an interpolation is refused without the rule having to
# know what it resolves to, so a host path cannot slip past a deny-list.
_REFUSED_MOUNTS = [
    "/var/run:/var/run",
    {"type": "bind", "source": "/var/run", "target": "/x"},
    {"type": "bind", "source": "/run", "target": "/x"},
    {"type": "bind", "source": "/", "target": "/x"},
    "/:/host",
    "/var:/v:ro",
    "/var/run/../run:/r",
    "//var/run:/r",
    "/var/./run/sub:/r",
    "/run/containerd/containerd.sock:/x",
    "/proc/1/root:/h",
    "/proc/1/root/run:/h",
    "/etc:/e",
    "/sys:/s",
    "/dev:/d",
    "/root:/h",
    "~root:/x",
    "./data:/data:ro",
    "/var/lib/derhost:/data",
    "../../../../../../../../../../var/run:/a",
    "${D:-/var/run}:/a",
    "$D:/a",
    "derhost-home/x:/x",
    "derhost$D:/x",
    "c:/etc:/x",
    "c:etc:/y",
    "c:y:/z",
    "C:\\Windows:/x",
    {"type": "bind", "source": "/etc", "target": "/e"},
    {"type": "volume", "source": "/etc", "target": "/e"},
    {"type": "volume", "target": "/e"},
    {"type": "bind", "source": "derhost-home", "target": "/e"},
    {"type": "image", "source": "derhost-server:local", "target": "/e"},
]


@pytest.mark.parametrize("volume", _REFUSED_MOUNTS)
def test_a_mount_source_that_is_not_a_named_volume_or_tmpfs_fails(volume: Any) -> None:
    with pytest.raises(AssertionError, match="not a named volume") as excinfo:
        check_named_volumes_only(SERVER_SERVICE, {"volumes": [volume]})
    assert repr(volume) in str(excinfo.value)


def test_volumes_from_is_refused_because_it_inherits_another_services_mounts() -> None:
    with pytest.raises(AssertionError, match="volumes_from") as excinfo:
        check_named_volumes_only(SERVER_SERVICE, {"volumes_from": ["sidecar"]})
    assert "sidecar" in str(excinfo.value)


@pytest.mark.parametrize(
    "volume",
    [
        "derhost-home:/var/volttron",
        {"type": "volume", "source": "derhost-home", "target": "/var/volttron"},
        {"type": "tmpfs", "target": "/tmp"},
    ],
)
def test_ordinary_mounts_pass_the_mount_rules(volume: Any) -> None:
    # Control: the refusals above come from the source, not from a blanket
    # refusal of volumes.
    service = {"volumes": [volume], "volumes_from": []}
    check_named_volumes_only(SERVER_SERVICE, service)
    check_no_docker_socket(SERVER_SERVICE, service)


# --- Rule 2b: top-level secrets, configs and volumes name no host path. -----


@pytest.mark.parametrize(
    ("compose", "key", "needle"),
    [
        ({"secrets": {"s": {"file": "/var/run/docker.sock"}}}, "s", "/var/run/docker.sock"),
        ({"configs": {"c": {"file": "/etc/passwd"}}}, "c", "/etc/passwd"),
        ({"secrets": {"s": {"file": "../../x"}}}, "s", "../../x"),
        ({"secrets": {"s": {"file": "./a/../../x"}}}, "s", "./a/../../x"),
        ({"secrets": {"s": {"file": ".."}}}, "s", ".."),
        ({"secrets": {"s": {"file": "${P}"}}}, "s", "${P}"),
        ({"secrets": {"s": {"file": "./${P}/x"}}}, "s", "./${P}/x"),
        ({"configs": {"c": {"file": "~root/c"}}}, "c", "~root/c"),
        ({"volumes": {"v": {"driver_opts": {"type": "none", "o": "bind", "device": "/var/run"}}}}, "v", "driver_opts"),
    ],
)
def test_top_level_sources_that_reach_the_host_fail(compose: dict[str, Any], key: str, needle: str) -> None:
    path = DOCKER_DIR / "some-agent" / "docker-compose.yml"
    with pytest.raises(AssertionError) as excinfo:
        check_top_level_sources(path, compose)
    message = str(excinfo.value)
    assert key in message and needle in message and str(path) in message


@pytest.mark.parametrize(
    "compose",
    [
        {"secrets": {"jwt": {"file": "./secrets/jwt"}}},
        {"secrets": {"jwt": {"file": "secrets/../secrets/jwt"}}},
        {"secrets": {"jwt": {"environment": "JWT"}}},
        {"configs": {"c": {"content": "x"}}},
        {"volumes": {"derhost-home": {"driver": "local"}}},
        {"volumes": {"derhost-home": None}},
        {},
    ],
)
def test_top_level_sources_inside_the_project_pass(compose: dict[str, Any]) -> None:
    # Control: the refusals above come from the file or driver_opts value.
    check_top_level_sources(DOCKER_DIR / "some-agent" / "docker-compose.yml", compose)


def test_top_level_sources_of_every_real_compose_file_pass() -> None:
    paths = [*discover_compose_files(), *discover_overlay_files()]
    assert len(paths) == 6, paths
    for path in paths:
        check_top_level_sources(path, load_compose(path))


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


_UNCONFINED_SECURITY_OPTS = tuple(
    f"{kind}{sep}{value}"
    for kind, value in (
        ("seccomp", "unconfined"),
        ("apparmor", "unconfined"),
        ("label", "disable"),
        ("systempaths", "unconfined"),
    )
    for sep in ("=", ":")
)


def check_no_host_namespace(name: str, service: dict[str, Any]) -> None:
    # A value built by interpolation (`${USER:+host}`) is whatever the deploy
    # shell makes it, so it is refused as the mount rule refuses `$` in a source.
    for key in ("network_mode", "pid", "ipc", "uts", "userns_mode", "cgroup"):
        if "$" in str(service.get(key) or ""):
            raise AssertionError(f"{name}: {key} must not be interpolated: {service[key]!r}")
    for option in service.get("security_opt") or []:
        if "$" in str(option):
            raise AssertionError(f"{name}: security_opt must not be interpolated: {option!r}")
    if service.get("network_mode") == "host":
        raise AssertionError(f"{name}: network_mode: host must not be set")
    if service.get("pid") == "host":
        raise AssertionError(f"{name}: pid: host must not be set")
    for key in ("ipc", "userns_mode", "uts", "cgroup"):
        if service.get(key) == "host":
            raise AssertionError(f"{name}: {key}: host must not be set")
    for key in ("network_mode", "pid", "ipc"):
        if str(service.get(key) or "").startswith(("container:", "service:")):
            raise AssertionError(f"{name}: {key}: {service[key]} joins another container's namespace")
    for option in service.get("security_opt") or []:
        if str(option).strip().lower() in _UNCONFINED_SECURITY_OPTS:
            raise AssertionError(f"{name}: security_opt {option} disables a confinement profile")
    for key in ("devices", "device_cgroup_rules"):
        if service.get(key):
            raise AssertionError(f"{name}: {key} must not be set, got {service[key]!r}")


def test_no_host_namespace() -> None:
    for _, name, service in _all_services():
        check_no_host_namespace(name, service)


def test_no_host_namespace_fixture_fails_network() -> None:
    with pytest.raises(AssertionError, match="network_mode"):
        check_no_host_namespace(SERVER_SERVICE, {"network_mode": "host"})


def test_no_host_namespace_fixture_fails_pid() -> None:
    with pytest.raises(AssertionError, match="pid"):
        check_no_host_namespace(SERVER_SERVICE, {"pid": "host"})


@pytest.mark.parametrize(
    ("service", "needle"),
    [
        ({"security_opt": ["seccomp=unconfined"]}, "seccomp=unconfined"),
        ({"security_opt": ["seccomp:unconfined"]}, "seccomp:unconfined"),
        ({"security_opt": ["apparmor=unconfined"]}, "apparmor=unconfined"),
        ({"security_opt": ["apparmor:unconfined"]}, "apparmor:unconfined"),
        ({"security_opt": ["label=disable"]}, "label=disable"),
        ({"security_opt": ["label:disable"]}, "label:disable"),
        ({"security_opt": [" Seccomp=Unconfined "]}, "Seccomp=Unconfined"),
        ({"security_opt": ["no-new-privileges:true", "systempaths=unconfined"]}, "systempaths=unconfined"),
        ({"ipc": "host"}, "ipc"),
        ({"userns_mode": "host"}, "userns_mode"),
        ({"uts": "host"}, "uts"),
        ({"cgroup": "host"}, "cgroup"),
        ({"pid": "${USER:+host}"}, "pid must not be interpolated"),
        ({"network_mode": "${USER:+host}"}, "network_mode must not be interpolated"),
        ({"ipc": "${X:-host}"}, "ipc must not be interpolated"),
        ({"uts": "$X"}, "uts must not be interpolated"),
        ({"userns_mode": "${X:-host}"}, "userns_mode must not be interpolated"),
        ({"cgroup": "${X:-host}"}, "cgroup must not be interpolated"),
        ({"network_mode": "${X:-container:other}"}, "network_mode must not be interpolated"),
        ({"security_opt": ["${USER:+seccomp=unconfined}"]}, "security_opt must not be interpolated"),
        ({"security_opt": ["no-new-privileges:true", "label=${X:-disable}"]}, "security_opt must not be interpolated"),
        ({"network_mode": "container:other"}, "network_mode"),
        ({"network_mode": "service:other"}, "network_mode"),
        ({"pid": "container:other"}, "pid"),
        ({"pid": "service:other"}, "pid"),
        ({"ipc": "service:other"}, "ipc"),
        ({"devices": ["/dev/mem:/dev/mem"]}, "devices"),
        ({"device_cgroup_rules": ["c 1:3 mr"]}, "device_cgroup_rules"),
    ],
)
def test_no_host_namespace_fixture_fails_every_confinement_escape(service: dict[str, Any], needle: str) -> None:
    with pytest.raises(AssertionError, match=needle):
        check_no_host_namespace(SERVER_SERVICE, service)


def test_no_host_namespace_fixture_passes_a_confined_service() -> None:
    # Control: the parametrized refusals above are not a blanket refusal.
    check_no_host_namespace(
        SERVER_SERVICE,
        {
            "security_opt": ["no-new-privileges:true"],
            "network_mode": "bridge",
            "ipc": "private",
            "devices": [],
        },
    )


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


_SECRETS_DIR = "/run/secrets/"
_NUMERIC_SUFFIXES = ("MINUTES", "SECONDS", "HOURS", "DAYS", "TTL", "COUNT", "LENGTH")
_BOOLEAN_SUFFIXES = ("ENABLED", "REQUIRED")


def _declared_secret_names(service: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for entry in service.get("secrets") or []:
        declared = (entry.get("target") or entry.get("source")) if isinstance(entry, dict) else entry
        if declared:
            # Compose prints a long-form target as the absolute path it mounts at.
            names.add(str(declared).removeprefix(_SECRETS_DIR))
    return names


def _is_declared_secret_file(key: str, value: str, service: dict[str, Any]) -> bool:
    if not key.upper().endswith("_FILE") or "$" in value or posixpath.normpath(value) != value:
        return False
    if not value.startswith(_SECRETS_DIR):
        return False
    secret = value[len(_SECRETS_DIR) :]
    return bool(secret) and "/" not in secret and secret in _declared_secret_names(service)


def _is_config_shaped(key: str, value: str) -> bool:
    suffix = key.upper().rsplit("_", 1)[-1]
    if suffix in _NUMERIC_SUFFIXES:
        # Six digits cover a real TTL or count; a longer run is a secret in disguise.
        return re.fullmatch(r"[0-9]{1,6}", value) is not None
    if suffix in _BOOLEAN_SUFFIXES:
        return value.lower() in ("true", "false", "0", "1")
    return False


def check_no_secret_env(name: str, service: dict[str, Any]) -> None:
    for key, value in _env_mapping(service).items():
        if not _looks_like_secret_key(key):
            continue
        if _is_declared_secret_file(key, value, service) or _is_config_shaped(key, value):
            continue
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


@pytest.mark.parametrize(
    ("environment", "secrets"),
    [
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/jwt_secret_key"}, ["jwt_secret_key"]),
        (["JWT_SECRET_KEY_FILE=/run/secrets/jwt_secret_key"], ["jwt_secret_key"]),
        ({"DB_PASSWORD_FILE": "/run/secrets/db_password"}, [{"source": "db", "target": "db_password"}]),
        ({"DB_PASSWORD_FILE": "/run/secrets/db"}, [{"source": "db"}]),
        ({"ACCESS_TOKEN_EXPIRE_MINUTES": "30"}, []),
        (["ACCESS_TOKEN_EXPIRE_MINUTES=30"], []),
        ({"ACCESS_TOKEN_EXPIRE_MINUTES": 30}, []),
        ({"TOKEN_CACHE_ENABLED": "true"}, []),
        ({"TOKEN_CACHE_ENABLED": True}, []),
        ({"TOKEN_CACHE_REQUIRED": "false"}, []),
        ({"SESSION_TOKEN_SECONDS": "60"}, []),
        ({"SESSION_TOKEN_HOURS": "8"}, []),
        ({"SESSION_TOKEN_DAYS": "7"}, []),
        ({"ACCESS_TOKEN_TTL": "123456"}, []),
        ({"RETRY_TOKEN_COUNT": "3"}, []),
        ({"ADMIN_PASSWORD_LENGTH": "32"}, []),
        ({"VOLTTRON_HOME": "/var/volttron"}, []),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/jwt"}, [{"source": "jwt", "target": "/run/secrets/jwt"}]),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/jwt"}, [{"source": "other", "target": "jwt"}]),
    ],
)
def test_secret_env_allowances_pass(environment: Any, secrets: list[Any]) -> None:
    check_no_secret_env(SERVER_SERVICE, {"environment": environment, "secrets": secrets})


@pytest.mark.parametrize(
    ("environment", "secrets", "key"),
    [
        ({"JWT_SECRET_KEY": "not-a-real-secret"}, ["jwt_secret_key"], "JWT_SECRET_KEY"),
        ({"JWT_SECRET_KEY_FILE": "/etc/jwt"}, ["jwt"], "JWT_SECRET_KEY_FILE"),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/../etc/shadow"}, ["shadow"], "JWT_SECRET_KEY_FILE"),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/"}, [""], "JWT_SECRET_KEY_FILE"),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/a/b"}, ["a/b"], "JWT_SECRET_KEY_FILE"),
        ({"JWT_SECRET_KEY_FILE": "${JWT_PATH}"}, ["jwt"], "JWT_SECRET_KEY_FILE"),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/jwt"}, [], "JWT_SECRET_KEY_FILE"),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/jwt"}, ["other"], "JWT_SECRET_KEY_FILE"),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/.."}, [".."], "JWT_SECRET_KEY_FILE"),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/jwt"}, [{"source": "jwt", "target": "/etc/jwt"}], "JWT_SECRET_KEY_FILE"),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/a/b"}, [{"source": "x", "target": "/run/secrets/a/b"}], "JWT_SECRET_KEY_FILE"),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/$X"}, ["$X"], "JWT_SECRET_KEY_FILE"),
        ({"JWT_SECRET_KEY_FILE": "/run/secrets/None"}, [{}], "JWT_SECRET_KEY_FILE"),
        ({"ACCESS_TOKEN_TTL": ""}, [], "ACCESS_TOKEN_TTL"),
        ({"ACCESS_TOKEN_TTL": "1234567"}, [], "ACCESS_TOKEN_TTL"),
        ({"ACCESS_TOKEN_TTL": "9" * 40}, [], "ACCESS_TOKEN_TTL"),
        ({"ADMIN_PASSWORD_LENGTH": "73914562"}, [], "ADMIN_PASSWORD_LENGTH"),
        ({"ACCESS_TOKEN_EXPIRE_MINUTES": "abcdef"}, [], "ACCESS_TOKEN_EXPIRE_MINUTES"),
        ({"ACCESS_TOKEN_EXPIRE_MINUTES": "${MINUTES}"}, [], "ACCESS_TOKEN_EXPIRE_MINUTES"),
        ({"TOKEN_CACHE_ENABLED": "maybe"}, [], "TOKEN_CACHE_ENABLED"),
        ({"SECRET_FILE_PATH": "/run/secrets/x"}, ["x"], "SECRET_FILE_PATH"),
        ({"DB_PASSWORD": "${DB_PASSWORD}"}, [], "DB_PASSWORD"),
        (["DB_PASSWORD"], [], "DB_PASSWORD"),
    ],
)
def test_secret_env_violations_fail(environment: Any, secrets: list[Any], key: str) -> None:
    # Control for the allowances: none of them opens the rule for a value that
    # is not a declared secret file or a number or boolean.
    with pytest.raises(AssertionError, match=key):
        check_no_secret_env(SERVER_SERVICE, {"environment": environment, "secrets": secrets})


# --- Rule 13: every compose file name Docker would read is checked (#75). ----


def _touch(root: Path, *relative: str) -> None:
    for entry in relative:
        target = root / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("services: {}\n", encoding="utf-8")


def test_every_compose_file_name_under_docker_is_accepted() -> None:
    accepted = check_compose_file_names(DOCKER_DIR)
    assert sorted(p.name for p in accepted) == ["compose.smoke.yml"] * 3 + ["docker-compose.yml"] * 3, accepted
    assert [p for p in accepted if p.name == "docker-compose.yml"] == discover_compose_files()
    assert [p for p in accepted if p.name == "compose.smoke.yml"] == discover_overlay_files()


@pytest.mark.parametrize(
    "relative",
    [
        "x/compose.yaml",
        "x/compose.yml",
        "x/docker-compose.yaml",
        "x/docker-compose.override.yml",
        "x/docker-compose.override.yaml",
        "x/prod.override.yml",
        "x/Compose.YML",
        "x/compose.prod.yml",
        "x/deeper/docker-compose.yml",
        "compose.yml",
        "docker-compose.yml",
    ],
)
def test_a_compose_file_under_another_name_or_place_is_refused(tmp_path: Path, relative: str) -> None:
    _touch(tmp_path, "x/docker-compose.yml", "x/compose.smoke.yml", relative)
    with pytest.raises(AssertionError, match="not accepted") as excinfo:
        check_compose_file_names(tmp_path)
    assert str(tmp_path / relative) in str(excinfo.value)


def test_a_tree_of_accepted_names_passes_discovery(tmp_path: Path) -> None:
    # Control: the refusals above come from the names, not from the fixture tree.
    _touch(tmp_path, "x/docker-compose.yml", "x/compose.smoke.yml", "x/Dockerfile", "x/notes.yml")
    assert [p.name for p in check_compose_file_names(tmp_path)] == ["compose.smoke.yml", "docker-compose.yml"]


# --- Rule 14: no file is pulled in through include or extends (#75). ---------


@pytest.mark.parametrize(
    ("compose", "key", "needle"),
    [
        ({"include": ["other.yml"], "services": {}}, "include", "other.yml"),
        ({"include": [{"path": "../x.yml"}], "services": {}}, "include", "../x.yml"),
        ({"services": {"x": {"extends": {"file": "base.yml", "service": "x"}}}}, "extends", "base.yml"),
        ({"services": {"x": {"extends": {"service": "y"}}}}, "extends", "'y'"),
    ],
)
def test_a_compose_file_that_includes_or_extends_is_refused(compose: dict[str, Any], key: str, needle: str) -> None:
    path = DOCKER_DIR / "some-agent" / "docker-compose.yml"
    with pytest.raises(AssertionError) as excinfo:
        check_no_compose_references(path, compose)
    message = str(excinfo.value)
    assert key in message and needle in message and str(path) in message


def test_a_compose_file_without_references_passes() -> None:
    # Control: the refusals above come from the keys, not from the file shape.
    check_no_compose_references(
        DOCKER_DIR / "some-agent" / "docker-compose.yml",
        {"services": {"x": {"image": "i", "depends_on": ["y"]}}},
    )


def test_no_real_compose_file_includes_or_extends() -> None:
    paths = [*discover_compose_files(), *discover_overlay_files()]
    assert len(paths) == 6, paths
    for path in paths:
        check_no_compose_references(path, load_compose(path))


# --- Rule 15: the repo root's compose file is a named exception (#75, #145). -


def _write_root_file(root: Path, name: str, first_line: str) -> None:
    (root / name).write_text(f"{first_line}\nservices: {{}}\n", encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.override.yml",
        "compose.smoke.yml",
        "Docker-Compose.YML",
    ],
)
def test_a_root_compose_file_under_another_name_is_refused(tmp_path: Path, name: str) -> None:
    _write_root_file(tmp_path, "docker-compose.yml", _GENERATED_ROOT_HEADER)
    _write_root_file(tmp_path, name, _GENERATED_ROOT_HEADER)
    with pytest.raises(AssertionError, match="not accepted") as excinfo:
        check_root_compose_names(tmp_path)
    assert str(tmp_path / name) in str(excinfo.value)


@pytest.mark.parametrize("first_line", ["services:", "", "# Hand written", "x\n# Generated docker-compose.yml"])
def test_a_root_docker_compose_without_the_generated_header_is_refused(tmp_path: Path, first_line: str) -> None:
    _write_root_file(tmp_path, "docker-compose.yml", first_line)
    with pytest.raises(AssertionError, match="generated header") as excinfo:
        check_root_compose_names(tmp_path)
    assert str(tmp_path / "docker-compose.yml") in str(excinfo.value)


def test_an_empty_root_docker_compose_is_refused(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("", encoding="utf-8")
    with pytest.raises(AssertionError, match="generated header"):
        check_root_compose_names(tmp_path)


def test_a_root_with_the_generated_file_and_other_files_passes(tmp_path: Path) -> None:
    # Control: the refusals above come from the name and the header. A nested
    # file is the docker/ check's concern, not this one's.
    _write_root_file(tmp_path, "docker-compose.yml", _GENERATED_ROOT_HEADER)
    _touch(tmp_path, "notes.yml", "x/compose.yaml")
    (tmp_path / "generate-docker-compose.py").write_text("x = 1\n", encoding="utf-8")
    assert check_root_compose_names(tmp_path) == [tmp_path / "docker-compose.yml"]


def test_the_real_repo_root_has_exactly_the_generated_compose_file() -> None:
    assert check_root_compose_names(REPO_ROOT) == [REPO_ROOT / "docker-compose.yml"]


def test_the_generated_root_compose_file_includes_and_extends_nothing() -> None:
    path = REPO_ROOT / "docker-compose.yml"
    check_no_compose_references(path, load_compose(path))


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
    check_named_volumes_only,
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


# The same allow-list one level up: a top-level `volumes:` or a network
# `driver:` would rename or retarget an object the smoke run must keep apart
# from the developer's stack-up objects.
_OVERLAY_TOP_LEVEL_KEYS = ("name", "services", "networks")
_OVERLAY_NETWORK_KEYS = ("name", "external")


def check_overlay_top_level_keys(path: Path, compose: dict[str, Any]) -> None:
    for key in compose:
        if key not in _OVERLAY_TOP_LEVEL_KEYS:
            raise AssertionError(
                f"{path}: an overlay may not set top-level {key}; allowed keys: {', '.join(_OVERLAY_TOP_LEVEL_KEYS)}"
            )
    for network, entry in (compose.get("networks") or {}).items():
        for key in entry or {}:
            if key not in _OVERLAY_NETWORK_KEYS:
                raise AssertionError(
                    f"{path}: network {network} may not set {key}; allowed keys: {', '.join(_OVERLAY_NETWORK_KEYS)}"
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
    check_overlay_top_level_keys(path, compose)
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


def test_a_server_overlay_with_a_top_level_volume_rename_fails(tmp_path: Path) -> None:
    # The overlay that merged the smoke volume onto the developer's stack-up
    # volume, which no service-key rule can see.
    server_text = (DOCKER_DIR / "server" / "compose.smoke.yml").read_text(encoding="utf-8")
    overlay = tmp_path / "server" / "compose.smoke.yml"
    overlay.parent.mkdir()
    overlay.write_text(
        server_text + "\nvolumes:\n  derhost-home:\n    name: derhost-server_derhost-home\n", encoding="utf-8"
    )
    assert load_compose(overlay)["volumes"] == {"derhost-home": {"name": "derhost-server_derhost-home"}}
    with pytest.raises(AssertionError, match="may not set top-level volumes"):
        check_overlay(overlay, load_compose(overlay))


def test_an_overlay_network_with_a_driver_fails(tmp_path: Path) -> None:
    server_text = (DOCKER_DIR / "server" / "compose.smoke.yml").read_text(encoding="utf-8")
    marker = "    name: derhost-smoke-net\n"
    assert marker in server_text
    overlay = tmp_path / "server" / "compose.smoke.yml"
    overlay.parent.mkdir()
    overlay.write_text(server_text.replace(marker, marker + "    driver: host\n"), encoding="utf-8")
    with pytest.raises(AssertionError, match="network derhost-net may not set driver"):
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
