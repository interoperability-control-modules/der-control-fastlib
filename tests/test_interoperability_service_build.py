"""docker/interoperability-service build gating: check-clean.sh's dirty-
checkout refusal, build.sh's archive isolation and labeling, and
docker-compose.yml's required DER_AGENT_SRC (#68)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_CLEAN = REPO_ROOT / "docker" / "interoperability-service" / "check-clean.sh"
BUILD_SH = REPO_ROOT / "docker" / "interoperability-service" / "build.sh"


def _run_check_clean(checkout: Path, allow_dirty: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("ALLOW_DIRTY", None)
    if allow_dirty is not None:
        env["ALLOW_DIRTY"] = allow_dirty
    return subprocess.run(
        ["bash", str(CHECK_CLEAN), str(checkout)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def scratch_checkout(tmp_path: Path) -> Path:
    """A throwaway git repo with one committed file."""
    repo = tmp_path / "agent-src"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "agent.py").write_text("# placeholder\n")
    _git(repo, "add", "agent.py")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_clean_checkout_passes(scratch_checkout: Path) -> None:
    result = _run_check_clean(scratch_checkout)
    assert result.returncode == 0, result.stderr


def test_dirty_checkout_is_refused(scratch_checkout: Path) -> None:
    # Control: the check must actually be able to fail on a dirty checkout.
    (scratch_checkout / "agent.py").write_text("# changed\n")
    result = _run_check_clean(scratch_checkout)
    assert result.returncode == 1
    assert "uncommitted changes" in result.stderr


def test_allow_dirty_bypasses_the_refusal(scratch_checkout: Path) -> None:
    (scratch_checkout / "agent.py").write_text("# changed\n")
    result = _run_check_clean(scratch_checkout, allow_dirty="1")
    assert result.returncode == 0, result.stderr


def test_missing_checkout_path_is_named_as_missing(tmp_path: Path) -> None:
    # A path that does not exist is a different mistake from a directory that
    # is not a repository, and the message must not blur the two (#86).
    result = _run_check_clean(tmp_path / "no-such-checkout")
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert "not a git checkout" not in result.stderr


def test_non_git_checkout_is_refused(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    result = _run_check_clean(not_a_repo)
    assert result.returncode == 1
    assert "not a git checkout" in result.stderr


def test_allow_dirty_still_refuses_a_missing_path(tmp_path: Path) -> None:
    # ALLOW_DIRTY bypasses only the dirtiness check (#148).
    result = _run_check_clean(tmp_path / "no-such-checkout", allow_dirty="1")
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert "skipping" not in result.stderr


def test_allow_dirty_still_refuses_a_non_git_path(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    result = _run_check_clean(not_a_repo, allow_dirty="1")
    assert result.returncode == 1
    assert "not a git checkout" in result.stderr
    assert "skipping" not in result.stderr


def test_subdirectory_of_a_work_tree_is_refused(scratch_checkout: Path) -> None:
    # build-agent.sh archives HEAD of the path it is given; from a
    # subdirectory that would silently archive only the subtree (#148).
    sub = scratch_checkout / "pkg"
    sub.mkdir()
    (sub / "mod.py").write_text("# placeholder\n")
    _git(scratch_checkout, "add", "pkg/mod.py")
    _git(scratch_checkout, "commit", "-q", "-m", "add pkg")
    result = _run_check_clean(sub)
    assert result.returncode == 1
    assert "subdirectory" in result.stderr
    assert str(scratch_checkout.resolve()) in result.stderr


def test_allow_dirty_still_refuses_a_subdirectory(scratch_checkout: Path) -> None:
    sub = scratch_checkout / "pkg"
    sub.mkdir()
    result = _run_check_clean(sub, allow_dirty="1")
    assert result.returncode == 1
    assert "subdirectory" in result.stderr
    assert "skipping" not in result.stderr


def test_symlink_to_the_top_of_a_work_tree_passes(scratch_checkout: Path, tmp_path: Path) -> None:
    # The top is compared after `pwd -P`, so a symlink to it is still its top.
    link = tmp_path / "link-to-checkout"
    link.symlink_to(scratch_checkout)
    result = _run_check_clean(link)
    assert result.returncode == 0, result.stderr


def test_allow_dirty_on_a_clean_top_level_checkout_still_skips(scratch_checkout: Path) -> None:
    result = _run_check_clean(scratch_checkout, allow_dirty="1")
    assert result.returncode == 0, result.stderr
    assert "skipping" in result.stderr


# --- build.sh: archive isolation, committed-content-only, dirty labeling ---
#
# build.sh's last step is "docker compose ... build"; these tests stub
# `docker` on PATH so no image is ever built. The stub still runs for real
# for "image inspect" (answers yes, so build.sh never tries the base image)
# and, for "compose", records what build.sh actually resolved and exported
# before it would have handed off to the real build: DER_AGENT_SRC,
# AGENT_REVISION, and whether two known files are present in the directory
# build.sh exported as DER_AGENT_SRC. That directory is a mktemp -d build.sh
# removes via its own EXIT trap, so the check has to happen here, inside the
# stub, while build.sh is still running.


@pytest.fixture
def agent_checkout(tmp_path: Path) -> Path:
    """A throwaway git repo shaped like interoperability-service: a
    committed src/interoperability/agent.py, matching what build.sh
    archives and the Dockerfile copies."""
    repo = tmp_path / "agent-src"
    (repo / "src" / "interoperability").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "src" / "interoperability" / "agent.py").write_text("# placeholder\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _stub_docker_bin(bin_dir: Path, log_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "docker"
    stub.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  exit 0
fi
if [ "$1" = "compose" ]; then
  printf '%s' "${{DER_AGENT_SRC:-}}" > "{log_dir}/der_agent_src.txt"
  printf '%s' "${{AGENT_REVISION:-}}" > "{log_dir}/agent_revision.txt"
  if [ -f "${{DER_AGENT_SRC:-/nonexistent}}/src/interoperability/agent.py" ]; then
    echo present > "{log_dir}/agent_py.txt"
  else
    echo absent > "{log_dir}/agent_py.txt"
  fi
  if [ -f "${{DER_AGENT_SRC:-/nonexistent}}/src/interoperability/leaked-secret.txt" ]; then
    echo present > "{log_dir}/leaked.txt"
  else
    echo absent > "{log_dir}/leaked.txt"
  fi
  exit 0
fi
echo "stub docker: unexpected invocation: $*" >&2
exit 1
""")
    stub.chmod(0o755)


def _run_build_sh(
    tmp_path: Path,
    *,
    cwd: Path,
    der_agent_src: str,
    allow_dirty: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    log_dir = tmp_path / "stub-log"
    log_dir.mkdir()
    bin_dir = tmp_path / "stub-bin"
    _stub_docker_bin(bin_dir, log_dir)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DER_AGENT_SRC"] = der_agent_src
    env.pop("ALLOW_DIRTY", None)
    if allow_dirty is not None:
        env["ALLOW_DIRTY"] = allow_dirty

    result = subprocess.run(
        ["bash", str(BUILD_SH)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
        check=False,
    )
    return result, log_dir


def test_build_exports_the_archive_dir_not_the_checkout(agent_checkout: Path, tmp_path: Path) -> None:
    """DER_AGENT_SRC must reach compose as the git-archive export directory,
    never agent_src itself, whether DER_AGENT_SRC was given as a relative
    path or an absolute one (#68)."""
    caller_cwd = tmp_path / "somewhere-else"
    caller_cwd.mkdir()
    relative = os.path.relpath(agent_checkout, start=caller_cwd)

    result, log_dir = _run_build_sh(tmp_path, cwd=caller_cwd, der_agent_src=relative)
    assert result.returncode == 0, result.stderr

    exported = (log_dir / "der_agent_src.txt").read_text()
    assert Path(exported).is_absolute(), exported
    # It must not be agent_checkout itself: build.sh exports the git-archive
    # export directory, never the raw checkout.
    assert Path(exported) != agent_checkout


def test_build_never_ships_an_uncommitted_file(agent_checkout: Path, tmp_path: Path) -> None:
    (agent_checkout / "src" / "interoperability" / "leaked-secret.txt").write_text("JWT_SECRET_KEY=MARKER\n")

    result, log_dir = _run_build_sh(tmp_path, cwd=tmp_path, der_agent_src=str(agent_checkout), allow_dirty="1")
    assert result.returncode == 0, result.stderr
    assert (log_dir / "agent_py.txt").read_text().strip() == "present"
    assert (log_dir / "leaked.txt").read_text().strip() == "absent"


def test_build_label_never_claims_dirty_content_it_does_not_ship(agent_checkout: Path, tmp_path: Path) -> None:
    """git archive always exports HEAD, dirty checkout or not (#68 F2), so an
    ALLOW_DIRTY=1 label must not claim dirty content the image never ships."""
    clean_result, clean_log = _run_build_sh(tmp_path / "clean-run", cwd=tmp_path, der_agent_src=str(agent_checkout))
    assert clean_result.returncode == 0, clean_result.stderr
    clean_revision = (clean_log / "agent_revision.txt").read_text()
    assert not clean_revision.endswith("-dirty"), clean_revision

    (agent_checkout / "src" / "interoperability" / "agent.py").write_text("# changed\n")
    dirty_result, dirty_log = _run_build_sh(
        tmp_path / "dirty-run", cwd=tmp_path, der_agent_src=str(agent_checkout), allow_dirty="1"
    )
    assert dirty_result.returncode == 0, dirty_result.stderr
    dirty_revision = (dirty_log / "agent_revision.txt").read_text()
    assert dirty_revision == clean_revision, dirty_revision


# --- docker-compose.yml: DER_AGENT_SRC has no default (#68 F4) -------------
#
# These call the real docker CLI's "compose ... config", which only resolves
# and prints the file; it builds, creates, and starts nothing.

COMPOSE_FILE = REPO_ROOT / "docker" / "interoperability-service" / "docker-compose.yml"


def _run_compose_config(der_agent_src: str | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if der_agent_src is None:
        env.pop("DER_AGENT_SRC", None)
    else:
        env["DER_AGENT_SRC"] = der_agent_src
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker CLI")
def test_compose_refuses_to_resolve_without_der_agent_src() -> None:
    # A developer running compose directly, without build.sh, must not
    # silently resolve a raw agent_src default (#68 F4): compose fails
    # before any build, create, or start is attempted.
    result = _run_compose_config(None)
    assert result.returncode != 0
    assert "DER_AGENT_SRC" in result.stderr


@pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker CLI")
def test_compose_resolves_once_der_agent_src_is_set() -> None:
    # Control: the refusal above is a missing-variable check, not a broken
    # compose file; setting the variable must let resolution proceed.
    result = _run_compose_config("/dev/null/no-build")
    assert result.returncode == 0, result.stderr
    assert "agent_src:" in result.stdout


_DOCKER_DIR = REPO_ROOT / "docker"
_PLACEHOLDER_ASSIGNMENT = re.compile(r"DER_AGENT_SRC=([^\s\"\\]+)")
_PLACEHOLDER_SITES = ["env-example", "docker.mk", "smoke.sh"]


def _placeholders_set_at(site: str, agent: str) -> list[str]:
    """Every placeholder value the named file sets, read from the file."""
    if site == "env-example":
        path = _DOCKER_DIR / agent / ".env.example"
        prefix = "#DER_AGENT_SRC="
        values = [ln.removeprefix(prefix) for ln in path.read_text().splitlines() if ln.startswith(prefix)]
    else:
        path = _DOCKER_DIR / site
        values = [
            m.group(1)
            for ln in path.read_text().splitlines()
            if not ln.lstrip().startswith("#")
            for m in _PLACEHOLDER_ASSIGNMENT.finditer(ln)
        ]
    assert values, f"no DER_AGENT_SRC= assignment in {path}"
    return values


@pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker CLI")
@pytest.mark.parametrize("site", _PLACEHOLDER_SITES)
@pytest.mark.parametrize("agent", ["interoperability-service", "realtime-control-agent"])
def test_the_der_agent_src_placeholder_never_resolves_to_a_directory(site: str, agent: str, tmp_path: Path) -> None:
    # Compose resolves a relative additional_contexts value against the
    # project directory, so a placeholder like "unused" becomes a real build
    # context when a directory of that name exists there (#86). The copy
    # carries such a directory when the placeholder is relative; the placeholder
    # must not resolve to a directory either way.
    for index, placeholder in enumerate(_placeholders_set_at(site, agent)):
        _assert_placeholder_is_not_a_directory(agent, placeholder, tmp_path / str(index))


def _assert_placeholder_is_not_a_directory(agent: str, placeholder: str, base: Path) -> None:
    project = base / agent
    project.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "docker" / agent / "docker-compose.yml", project / "docker-compose.yml")
    if not os.path.isabs(placeholder):
        (project / placeholder).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, DER_AGENT_SRC=placeholder)
    result = subprocess.run(
        ["docker", "compose", "-f", str(project / "docker-compose.yml"), "config"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    resolved = [ln.split("agent_src:", 1)[1].strip() for ln in result.stdout.splitlines() if "agent_src:" in ln]
    assert len(resolved) == 1, result.stdout
    assert not Path(resolved[0]).is_dir(), (placeholder, resolved[0])
