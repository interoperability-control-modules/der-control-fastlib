#!/usr/bin/env bash
# Smoke-tests the docker/server stack and every agent directory in
# DERHOST_SMOKE_AGENT_DIRS end to end (#68, #95): config, build, up, health
# wait, identity check, restart of one agent, down, teardown count. Every
# project here is a derhost-smoke project with its own compose overlay, so a
# run never names or removes a developer's `stack-up` objects. Run it as
# `make stack-smoke`, which passes AGENT_DIRS in. The author runs this
# locally and pastes its output into the PR body, since CI does not build or
# run containers here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/server/docker-compose.yml"
SMOKE_OVERLAY="$SCRIPT_DIR/server/compose.smoke.yml"
PROJECT_DIR="$SCRIPT_DIR/server"
PROJECT_NAME="derhost-smoke"
STEP_TIMEOUT="${DERHOST_SMOKE_STEP_TIMEOUT:-120}"
BUILD_TIMEOUT="${DERHOST_SMOKE_BUILD_TIMEOUT:-600}"
WAIT_TIMEOUT="${DERHOST_SMOKE_WAIT_TIMEOUT:-60}"
POLL_SECS="${DERHOST_SMOKE_POLL_SECS:-1}"
RESTART_WINDOW="${DERHOST_SMOKE_RESTART_WINDOW:-10}"
RESTART_DIR="realtime-control-agent"
CLEANED_UP=0
BASE_URL=""
AGENTS=()
PROJECTS=("$PROJECT_NAME")

log() {
    printf '[smoke] %s\n' "$1"
}

die() {
    log "error: $1"
    exit 1
}

# An array, not a function: `timeout` execs its argument directly and
# cannot see a shell function, so each server step below expands this array
# in place of a `compose` wrapper. -p and the overlay pin every object this
# run creates to its own project, disjoint by name from a developer's
# `stack-up` project (docker/server/compose.smoke.yml).
COMPOSE_ARGS=(docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" -f "$SMOKE_OVERLAY" --project-directory "$PROJECT_DIR")

# Runs docker compose for one agent in its own smoke project. DER_AGENT_SRC
# only has to be set for the compose file to load: every call here is
# `--no-build` or a teardown, and the image comes from build.sh.
agent_compose() {
    local dir="$1" timeout_secs="$2"
    shift 2
    timeout "$timeout_secs" env DER_AGENT_SRC=unused docker compose -p "derhost-smoke-$dir" \
        -f "$SCRIPT_DIR/$dir/docker-compose.yml" -f "$SCRIPT_DIR/$dir/compose.smoke.yml" \
        --project-directory "$SCRIPT_DIR/$dir" "$@"
}

validate_agent_dirs() {
    # Unset is a refusal and set-but-empty is "server only": an environment
    # that silently lost the list must not read as a pass over zero agents.
    if [ -z "${DERHOST_SMOKE_AGENT_DIRS+x}" ]; then
        die "DERHOST_SMOKE_AGENT_DIRS is not set; run this through 'make stack-smoke'"
    fi
    if [ -n "${DER_AGENT_SRC:-}" ]; then
        die "DER_AGENT_SRC is set; it would apply to every agent build, unset it"
    fi
    local dir seen=" "
    local -a requested
    read -r -a requested <<<"$DERHOST_SMOKE_AGENT_DIRS"
    for dir in "${requested[@]+"${requested[@]}"}"; do
        if [ "$dir" = "server" ] || [ "$dir" = "smoke" ]; then
            die "AGENT_DIRS entry '$dir' is reserved: it would collide with a server project name"
        fi
        if ! [[ "$dir" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
            die "AGENT_DIRS entry '$dir' is invalid: lowercase letters, digits and hyphens only"
        fi
        if [[ "$seen" == *" $dir "* ]]; then
            die "AGENT_DIRS entry '$dir' is a duplicate"
        fi
        seen="$seen$dir "
        local file
        for file in docker-compose.yml compose.smoke.yml build.sh Dockerfile; do
            [ -f "$SCRIPT_DIR/$dir/$file" ] || die "docker/$dir/$file is missing"
        done
        AGENTS+=("$dir")
        PROJECTS+=("derhost-smoke-$dir")
    done
}

# The identity an agent registers is the one its Dockerfile CMD passes, so
# the expectation tracks the image rather than a second list that could drift.
agent_identity() {
    local identity
    identity="$(sed -n 's/.*"--identity", "\([^"]*\)".*/\1/p' "$SCRIPT_DIR/$1/Dockerfile")"
    [ -n "$identity" ] || die "docker/$1/Dockerfile passes no --identity"
    printf '%s\n' "$identity"
}

# Prints one line per container, network or volume that carries a smoke
# project label or a derhost-smoke name. A listing that fails stops the run:
# an unreadable daemon must not read as "nothing left".
smoke_objects() {
    local project filter out
    for project in "${PROJECTS[@]}"; do
        filter="label=com.docker.compose.project=$project"
        out="$(docker ps -aq --filter "$filter")" || die "cannot list containers"
        [ -z "$out" ] || printf 'container %s\n' "$out"
        out="$(docker network ls -q --filter "$filter")" || die "cannot list networks"
        [ -z "$out" ] || printf 'network %s\n' "$out"
        out="$(docker volume ls -q --filter "$filter")" || die "cannot list volumes"
        [ -z "$out" ] || printf 'volume %s\n' "$out"
    done
    out="$(docker ps -aq --filter name=derhost-smoke)" || die "cannot list containers"
    [ -z "$out" ] || printf 'container %s\n' "$out"
    out="$(docker network ls -q --filter name=derhost-smoke)" || die "cannot list networks"
    [ -z "$out" ] || printf 'network %s\n' "$out"
    out="$(docker volume ls -q --filter name=derhost-smoke)" || die "cannot list volumes"
    [ -z "$out" ] || printf 'volume %s\n' "$out"
}

smoke_images() {
    local out
    out="$(docker image ls -q --filter 'reference=derhost*:smoke')" || die "cannot list images"
    [ -z "$out" ] || printf 'image %s\n' "$out"
}

refuse_if_leftover_exists() {
    # A killed or concurrent smoke run leaves objects still carrying a smoke
    # project's label; `build`/`up` would then attach to that older run's
    # objects instead of ones this run created, and this run's `down` would
    # then remove them. Compose labels containers, networks and volumes
    # alike with com.docker.compose.project, so all three are checked, by
    # exact label for every project this run uses and by name for any other
    # derhost-smoke* object.
    local leftover
    leftover="$(smoke_objects)"
    if [ -n "$leftover" ]; then
        log "refusing: a derhost-smoke object already exists (not started by this run)"
        printf '%s\n' "$leftover" | sed 's/^/[smoke]   /'
        exit 1
    fi
}

step_down() {
    local i dir
    log "down"
    # Agents first: they sit on the server's smoke network, and the server's
    # `down` cannot remove a network that still has endpoints. Reverse of
    # start order.
    for ((i = ${#AGENTS[@]} - 1; i >= 0; i--)); do
        dir="${AGENTS[$i]}"
        # -v and --rmi local remove the volumes and the untagged images a
        # project built. Safe unconditionally: every name in a smoke project
        # is unique to it, so there is nothing of a developer's stack to reach.
        agent_compose "$dir" "$STEP_TIMEOUT" down -v --rmi local
    done
    timeout "$STEP_TIMEOUT" "${COMPOSE_ARGS[@]}" down -v --rmi local
    # `--rmi local` skips an image whose service sets `image:`, which every
    # smoke overlay does, so the :smoke tags are removed by name. Only tags
    # this run created: build.sh's :local images are not smoke objects.
    remove_smoke_image "derhost-server:smoke"
    for dir in "${AGENTS[@]+"${AGENTS[@]}"}"; do
        remove_smoke_image "derhost-$dir:smoke"
    done
}

remove_smoke_image() {
    if docker image inspect "$1" >/dev/null 2>&1; then
        docker image rm "$1" >/dev/null
    fi
}

assert_teardown_clean() {
    local remaining count
    remaining="$(smoke_objects; smoke_images)"
    count="$(printf '%s\n' "$remaining" | awk 'NF { n++ } END { print n + 0 }')"
    log "teardown: $count smoke objects remain"
    if [ "$count" -ne 0 ]; then
        printf '%s\n' "$remaining" | sed 's/^/[smoke]   /'
        die "teardown left $count smoke objects"
    fi
}

cleanup() {
    if [ "$CLEANED_UP" -eq 0 ]; then
        CLEANED_UP=1
        # Best-effort: this runs from the EXIT trap, including after a
        # failed step, so a teardown failure here must not mask the
        # original exit status or abort the trap handler itself.
        step_down || true
    fi
}

# Calls stack-check once and leaves its text in CHECK_OUT. Its exit status is
# the answer: 0 when every named identity is connected.
CHECK_OUT=""
run_check() {
    local rc=0
    CHECK_OUT="$(timeout "$STEP_TIMEOUT" make -C "$REPO_ROOT" --no-print-directory stack-check "DERHOST_CHECK_BASE_URL=$BASE_URL" "EXPECTED=$1" 2>&1)" || rc=$?
    return "$rc"
}

# Polls until every identity in $1 is connected, or $2 seconds pass.
wait_connected() {
    local deadline=$((SECONDS + $2))
    while ! run_check "$1"; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            printf '%s\n' "$CHECK_OUT"
            return 1
        fi
        sleep "$POLL_SECS"
    done
    printf '%s\n' "$CHECK_OUT"
}

# Polls until $1 is reported MISSING, or $2 seconds pass. Any other failure
# of the check (server unreachable, bad status) is not "absent" and fails.
wait_absent() {
    local deadline=$((SECONDS + $2))
    while true; do
        if ! run_check "$1"; then
            printf '%s\n' "$CHECK_OUT"
            [[ "$CHECK_OUT" == *"$1: MISSING"* ]]
            return
        fi
        [ "$SECONDS" -lt "$deadline" ] || return 1
        sleep "$POLL_SECS"
    done
}

step_build_and_start_agent() {
    local dir="$1"
    log "build $dir"
    # build.sh keeps its clean-checkout rule and builds the :local image;
    # the smoke overlay runs the :smoke tag, so the built image is tagged
    # rather than the agent's build logic being forked here.
    timeout "$BUILD_TIMEOUT" bash "$SCRIPT_DIR/$dir/build.sh" || die "build.sh failed for $dir"
    docker tag "derhost-$dir:local" "derhost-$dir:smoke"
    log "up $dir"
    agent_compose "$dir" "$STEP_TIMEOUT" up -d --no-build --wait --wait-timeout "$WAIT_TIMEOUT"
}

# Issue 96's job probes run here, once both agents are connected and before
# the restart. Nothing runs yet.
step_job_probes() {
    log "job probes: none yet (issue 96)"
}

step_restart() {
    local dir="$RESTART_DIR" container identity before after
    local listed=0 candidate
    for candidate in "${AGENTS[@]+"${AGENTS[@]}"}"; do
        [ "$candidate" != "$dir" ] || listed=1
    done
    if [ "$listed" -eq 0 ]; then
        log "restart: $dir is not in AGENT_DIRS, skipped"
        return 0
    fi
    container="derhost-smoke-$dir"
    identity="$(agent_identity "$dir")"
    log "restart $container"
    before="$(docker container inspect --format '{{.State.StartedAt}}' "$container")"
    # Stop then start, not `docker restart`: the stop gives the server a
    # window in which the identity is observably absent, which a restart
    # that finishes in a second would not.
    docker stop "$container" >/dev/null
    wait_absent "$identity" "$RESTART_WINDOW" || die "$identity is still connected after $container stopped"
    docker start "$container" >/dev/null
    wait_connected "$identity" "$RESTART_WINDOW" || die "$identity did not reconnect within ${RESTART_WINDOW}s"
    after="$(docker container inspect --format '{{.State.StartedAt}}' "$container")"
    [ "$after" != "$before" ] || die "$container StartedAt did not change ($before)"
    log "StartedAt changed: $before -> $after"
}

main() {
    validate_agent_dirs

    log "config -q"
    timeout "$STEP_TIMEOUT" "${COMPOSE_ARGS[@]}" config -q
    local dir expected=""
    for dir in "${AGENTS[@]+"${AGENTS[@]}"}"; do
        agent_compose "$dir" "$STEP_TIMEOUT" config -q
        expected="$expected${expected:+,}$(agent_identity "$dir")"
    done

    refuse_if_leftover_exists

    log "build"
    timeout "$STEP_TIMEOUT" "${COMPOSE_ARGS[@]}" build

    log "up"
    # Armed before `up` is invoked, not after it returns: a stack this run
    # never created must never be torn down (config and the leftover check
    # above both fail before this line), but a partial failure inside `up`
    # itself (a network or volume created before the container fails to
    # start) must still be cleaned up.
    trap cleanup EXIT INT TERM
    timeout "$STEP_TIMEOUT" "${COMPOSE_ARGS[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT"

    log "check"
    local port
    port="$(timeout "$STEP_TIMEOUT" "${COMPOSE_ARGS[@]}" port derhost-server 8000)"
    BASE_URL="http://${port}"
    run_check "" || { printf '%s\n' "$CHECK_OUT"; die "server check failed"; }

    for dir in "${AGENTS[@]+"${AGENTS[@]}"}"; do
        step_build_and_start_agent "$dir"
    done

    if [ -n "$expected" ]; then
        log "check identities: $expected"
        wait_connected "$expected" "$WAIT_TIMEOUT" || die "not every identity connected within ${WAIT_TIMEOUT}s"
    fi

    step_job_probes
    step_restart

    step_down
    CLEANED_UP=1
    assert_teardown_clean

    log "smoke passed"
}

main "$@"
