#!/usr/bin/env bash
# Smoke-tests the docker/server stack and every agent directory in
# DERHOST_SMOKE_AGENT_DIRS end to end (#68, #95): config, build, up, health
# wait, identity check, job probes, restart of one agent, down, teardown count.
# Every project here is a derhost-smoke project with its own compose overlay, so a
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
# Seconds `timeout` waits after a forwarded signal before it kills the step.
KILL_GRACE=10
CLEANED_UP=0
CHILD_PID=""
TEARDOWN_FAILED=()
BASE_URL=""
AGENTS=()
PROJECTS=("$PROJECT_NAME")

log() {
    printf '[smoke] %s\n' "$1"
}

# stderr, not stdout: a die inside `$(...)` would otherwise be captured into
# the caller's variable and never reach the terminal.
die() {
    printf '[smoke] error: %s\n' "$1" >&2
    exit 1
}

# An array, not a function: `timeout` execs its argument directly and
# cannot see a shell function, so each server step below expands this array
# in place of a `compose` wrapper. -p and the overlay pin every object this
# run creates to its own project, disjoint by name from a developer's
# `stack-up` project (docker/server/compose.smoke.yml).
COMPOSE_ARGS=(docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" -f "$SMOKE_OVERLAY" --project-directory "$PROJECT_DIR")

# Runs a command as a child and waits for it with `wait`, which returns as
# soon as a trapped signal arrives. A foreground command would hold the trap
# until it ended, up to a whole build timeout. Teardown steps stay in the
# foreground on purpose: they must finish, and an early return from `wait`
# would start the next step while this one still ran.
run() {
    local rc=0
    "$@" &
    CHILD_PID=$!
    wait "$CHILD_PID" || rc=$?
    CHILD_PID=""
    return "$rc"
}

# Ends the step `run` is waiting on. `timeout` forwards the TERM and, with
# -k, kills the step after KILL_GRACE seconds if it ignores it.
stop_child() {
    [ -n "$CHILD_PID" ] || return 0
    # The step may have exited between the signal and here, and a killed step
    # reports a non-zero status; neither changes what happens next.
    kill "$CHILD_PID" 2>/dev/null || true
    wait "$CHILD_PID" 2>/dev/null || true
    CHILD_PID=""
}

# Fills AGENT_CMD with docker compose for one agent in its own smoke project,
# under `timeout`. DER_AGENT_SRC only has to be set for the compose file to
# load: every call here is `--no-build` or a teardown, and the image comes
# from build.sh.
AGENT_CMD=()
agent_compose_cmd() {
    local dir="$1" timeout_secs="$2"
    shift 2
    AGENT_CMD=(timeout -k "$KILL_GRACE" "$timeout_secs" env DER_AGENT_SRC=unused docker compose -p "derhost-smoke-$dir"
        -f "$SCRIPT_DIR/$dir/docker-compose.yml" -f "$SCRIPT_DIR/$dir/compose.smoke.yml"
        --project-directory "$SCRIPT_DIR/$dir" "$@")
}

# In the foreground: for teardown and quick steps.
agent_compose() {
    agent_compose_cmd "$@"
    "${AGENT_CMD[@]}"
}

# As a child `run` waits on: for steps a signal must not wait behind.
agent_compose_run() {
    agent_compose_cmd "$@"
    run "${AGENT_CMD[@]}"
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
    # `read -a` takes only the first line, so a newline would silently drop
    # every name after it and still print "smoke passed".
    if [[ "$DERHOST_SMOKE_AGENT_DIRS" == *[$'\n\r\t']* ]]; then
        die "DERHOST_SMOKE_AGENT_DIRS must be one line of space-separated names"
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

# Prints "<kind> <id>" per object the listing command (the arguments after
# the kind) returns. A listing that fails stops the run: an unreadable daemon
# must not read as "nothing left".
list_objects() {
    local kind="$1" out id
    shift
    out="$("$@")" || die "cannot list ${kind}s"
    while IFS= read -r id; do
        [ -z "$id" ] || printf '%s %s\n' "$kind" "$id"
    done <<<"$out"
}

# One line per container, network or volume that carries a smoke project
# label or a derhost-smoke name. An object matched by both filters prints
# twice; the caller that counts deduplicates.
smoke_objects() {
    local project filter
    for project in "${PROJECTS[@]}"; do
        filter="label=com.docker.compose.project=$project"
        list_objects container docker ps -aq --filter "$filter"
        list_objects network docker network ls -q --filter "$filter"
        list_objects volume docker volume ls -q --filter "$filter"
    done
    list_objects container docker ps -aq --filter name=derhost-smoke
    list_objects network docker network ls -q --filter name=derhost-smoke
    list_objects volume docker volume ls -q --filter name=derhost-smoke
}

smoke_images() {
    list_objects image docker image ls -q --filter 'reference=derhost*:smoke'
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
    leftover="$(smoke_objects | sort -u)"
    if [ -n "$leftover" ]; then
        log "refusing: a derhost-smoke object already exists (not started by this run)"
        printf '%s\n' "$leftover" | sed 's/^/[smoke]   /'
        exit 1
    fi
}

# Runs one teardown step and records its name when it fails, so teardown
# reports every failed step instead of stopping at the first.
run_teardown_step() {
    local name="$1"
    shift
    if ! "$@"; then
        log "teardown step failed: $name"
        TEARDOWN_FAILED+=("$name")
    fi
}

remove_smoke_image() {
    # An image that is already gone (`down --rmi local` may have taken it)
    # needs no removal; a daemon that cannot answer shows up in the leftover
    # listing that follows.
    docker image inspect "$1" >/dev/null 2>&1 || return 0
    docker image rm "$1" >/dev/null
}

# Every step runs even when an earlier one failed. Returns 1 when any failed.
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
        run_teardown_step "down $dir" agent_compose "$dir" "$STEP_TIMEOUT" down -v --rmi local
    done
    run_teardown_step "down server" timeout -k "$KILL_GRACE" "$STEP_TIMEOUT" "${COMPOSE_ARGS[@]}" down -v --rmi local
    # `down --rmi local` removed the server's :smoke image in a live run but
    # is not relied on for any :smoke tag, so each is removed by name. Only
    # :smoke tags: a developer's :local images are never smoke objects.
    run_teardown_step "image rm derhost-server:smoke" remove_smoke_image "derhost-server:smoke"
    for dir in "${AGENTS[@]+"${AGENTS[@]}"}"; do
        run_teardown_step "image rm derhost-$dir:smoke" remove_smoke_image "derhost-$dir:smoke"
    done
    [ "${#TEARDOWN_FAILED[@]}" -eq 0 ]
}

# Prints each smoke object still present once, and the count. Returns 1 when
# any remain or the listing fails.
report_leftovers() {
    local remaining count
    remaining="$(smoke_objects; smoke_images)" || {
        log "teardown: cannot list the remaining smoke objects"
        return 1
    }
    remaining="$(printf '%s\n' "$remaining" | awk 'NF' | sort -u)"
    count="$(printf '%s\n' "$remaining" | awk 'NF { n++ } END { print n + 0 }')"
    log "teardown: $count smoke objects remain"
    if [ "$count" -ne 0 ]; then
        printf '%s\n' "$remaining" | sed 's/^/[smoke]   /'
        return 1
    fi
}

# Down, then the leftover count; the count is printed even when a step failed.
teardown() {
    local rc=0
    step_down || rc=1
    report_leftovers || rc=1
    return "$rc"
}

# The EXIT trap, also reached through `exit` in on_signal. It keeps the
# original exit status; a teardown failure only turns a would-be 0 into 1.
cleanup() {
    local status=$?
    # A signal during teardown is noted and the step in flight finishes: ending
    # here would leave objects behind for the next run to refuse. The steps are
    # foreground commands, so the note is printed once the current one ends,
    # and each step is bounded by its own timeout.
    trap 'on_teardown_signal INT' INT
    trap 'on_teardown_signal TERM' TERM
    if [ "$CLEANED_UP" -eq 0 ]; then
        CLEANED_UP=1
        if ! teardown && [ "$status" -eq 0 ]; then
            status=1
        fi
    fi
    exit "$status"
}

# Exits instead of returning, so the run does not resume after the trap; the
# EXIT trap then tears down. `run` steps are interrupted at once, so this
# ends the step first; before that trap is armed there is nothing to tear down.
on_signal() {
    log "caught $1, stopping"
    stop_child
    exit "$2"
}

on_teardown_signal() {
    log "caught $1 during teardown, finishing teardown first"
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
    # build.sh keeps its clean-checkout rule and git archive; the overlay
    # makes it build the :smoke image directly, so a smoke run never writes
    # or retags the developer's :local image.
    run env "DERHOST_BUILD_OVERLAY=$SCRIPT_DIR/$dir/compose.smoke.yml" timeout -k "$KILL_GRACE" "$BUILD_TIMEOUT" \
        bash "$SCRIPT_DIR/$dir/build.sh" || die "build.sh failed for $dir"
    log "up $dir"
    agent_compose_run "$dir" "$STEP_TIMEOUT" up -d --no-build --wait --wait-timeout "$WAIT_TIMEOUT"
}

# One job probe per listed agent, once every identity is connected and before
# the restart. A directory without a probe file fails: an agent that is
# connected but never asked to do anything is not smoke-tested. The probe's
# identity must be the one the Dockerfile registers, so the file cannot drift.
step_job_probes() {
    local dir probe identity
    for dir in "${AGENTS[@]+"${AGENTS[@]}"}"; do
        probe="$SCRIPT_DIR/$dir/smoke-probe.json"
        [ -f "$probe" ] || die "docker/$dir/smoke-probe.json is missing"
        identity="$(agent_identity "$dir")"
        log "probe $dir"
        # Through `run`, like every other step, so a signal ends it at once.
        # The interpreter prints its own result line ("probe ok:" on stdout,
        # "probe failed:" on stderr).
        run timeout -k "$KILL_GRACE" "$STEP_TIMEOUT" python3 "$SCRIPT_DIR/lib/smoke_probe.py" "$probe" "$BASE_URL" \
            --identity "$identity" || die "job probe failed for $dir"
    done
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
    # Caught from before the first compose call: a signal at any later step,
    # the builds included, ends the run with its status.
    trap 'on_signal INT 130' INT
    trap 'on_signal TERM 143' TERM

    validate_agent_dirs

    log "config -q"
    run timeout -k "$KILL_GRACE" "$STEP_TIMEOUT" "${COMPOSE_ARGS[@]}" config -q
    local dir expected=""
    for dir in "${AGENTS[@]+"${AGENTS[@]}"}"; do
        agent_compose_run "$dir" "$STEP_TIMEOUT" config -q
        expected="$expected${expected:+,}$(agent_identity "$dir")"
    done

    refuse_if_leftover_exists

    # Armed before the first step that creates anything, not after it
    # returns: a stack this run never created must never be torn down (config
    # and the leftover check above both fail before this line), but a partial
    # failure or a signal inside `build` or `up` (an image, a network or a
    # volume created before the step ends) must still be cleaned up.
    trap cleanup EXIT

    log "build"
    run timeout -k "$KILL_GRACE" "$STEP_TIMEOUT" "${COMPOSE_ARGS[@]}" build

    log "up"
    run timeout -k "$KILL_GRACE" "$STEP_TIMEOUT" "${COMPOSE_ARGS[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT"

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

    # CLEANED_UP stays 0 until teardown returns, so a signal during it reruns
    # the (idempotent) teardown from the trap.
    local teardown_rc=0
    teardown || teardown_rc=1
    CLEANED_UP=1
    [ "$teardown_rc" -eq 0 ] || die "teardown did not complete cleanly"

    log "smoke passed"
}

main "$@"
