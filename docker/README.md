# docker/

One container per agent and one for the derhost server (#68), separate from
the root `Dockerfile` / `docker-compose.yml` / `docker-helper.sh`, which stay
as they are and run a different set of services (see the root `docker-*` and
`compose-*` make targets).

Layout: `docker/<name>/` holds that service's `Dockerfile`, `docker-compose.yml`
and `.env.example`; an agent directory also holds `requirements.txt`,
`config.example.json`, `build.sh` and `check-clean.sh`. `docker/lib/` holds
the archive and dirty-checkout logic shared by every agent's `build.sh` and
`check-clean.sh` wrapper. `docker/server/` and `docker/interoperability-service/`
land in earlier PRs; `docker/realtime-control-agent/` runs native control
modes only, with no Julia in the image (operator decision, #68): its baked-in
`config.example.json` sets no mode's `use_julia` to true.

With that config the image runs one mode, `ConstantVar`, at 10 percent of
maximum reactive power (priority 1); `use_cases` is empty.

A configured mode that needs Julia fails in one of two ways, and in both the
container stays healthy, because its healthcheck only reads PID 1 (#132): a
mode with `use_julia: true` loads but raises `ModuleNotFoundError` on every
tick, and a Julia-only mode (anything under `rt_control/modes/novel`) fails
at configuration, so the control loop never starts.

If a configured mode ever sets `use_julia: true` with no `julia` package
installed, the agent does not crash or refuse to start: the import happens
lazily, on the first control tick that mode runs. `derhost`'s scheduler runs
each periodic tick in its own greenlet (`gevent.spawn`, uncaught by the
scheduler's own error handling), so the `ModuleNotFoundError` is logged by
gevent's default handler and the tick is lost, but the process keeps running
and reschedules the next tick regardless.

## Commands

Run from the repository root.

- `make stack-up C=server` - build and start the server.
- `make stack-up C=all` - start the server, then every directory in
  `AGENT_DIRS` (`docker/docker.mk`): interoperability-service and
  realtime-control-agent. Each agent is built by its own `build.sh` (dirty
  checkout refused) and started with `--no-build`; the first failure stops
  the run. `C=all` refuses to start while `DER_AGENT_SRC` is set, since it
  would apply to every agent; each uses its sibling checkout by default. It
  also refuses while `DERHOST_BUILD_OVERLAY` is set: only `stack-smoke` sets
  it, and a `build.sh` accepts no file but its own `compose.smoke.yml`.
- `make stack-down` - stop everything under `docker/` (each agent first, then
  the server, since agents join the server's network and it must be free of
  attached containers before the server's own `down` can remove it). A failed
  agent `down` does not stop the rest; the command exits non-zero at the end
  naming each agent whose `down` failed.
- `make stack-status` - `docker compose ps` for each stack.
- `make stack-smoke` - smoke-test the server and every `AGENT_DIRS` agent in
  their own `derhost-smoke` projects (see "Smoke test").
- `make stack-check` - `GET /connections` from the host and confirm the
  identities in `EXPECTED` (comma-separated) are connected; `EXPECTED=`
  (the default) trivially passes.

`stack-up` refuses before touching Docker when `DERHOST_PUBLISH_HOST` is not
a usable value, or when host port 5410 is already bound.

`AGENT_DIRS` may be set on the command line (`make stack-smoke AGENT_DIRS=...`).
Every target checks its text before anything runs, and stops at parse time,
before any docker call, when it holds a character other than `a-z`, `0-9`,
`-` or a space, an entry that starts with `-`, a repeated entry, or the
reserved names `server` and `smoke`. `SERVER_PROJECT_NAME` cannot be set: a
value on the command line is ignored and the project stays `derhost-server`.

## Only one stack on port 5410 at a time

The root `docker-compose.yml` stack and `docker/server/docker-compose.yml`
both publish host port 5410, under different container and network names
(`aems-fastlib-server` / `aems-network` vs `derhost-server` / `derhost-net`).
Only one of the two stacks can be up at once. `stack-up` refuses with a
named error, rather than a Docker port-conflict error, when the port is
already taken by the other stack (or anything else).

## Publishing on a different loopback address

Every compose file under `docker/` defaults the published port to
`127.0.0.1`. If that address does not answer on a given host, copy
`docker/server/.env.example` to `docker/server/.env` (gitignored) and set:

```
DERHOST_PUBLISH_HOST=127.0.0.2
```

still loopback-only, just a different address. `stack-up` and `stack-check`
both read this file when `DERHOST_PUBLISH_HOST` is not already set in the
environment; an environment value always wins over the `.env` file.

## Smoke test

`make stack-smoke` runs `docker/smoke.sh` over the server and every directory
in `AGENT_DIRS`. It proves the server is healthy, each agent registers its
identity (`platform.presentation`, `der.rtc`), each agent answers its job
probe (below), the `der.rtc` agent leaves and rejoins within 10 s of a stop
and start with a new `StartedAt`, and nothing labelled or named
`derhost-smoke` is left afterwards (the count is printed).

Each agent directory has a `compose.smoke.yml` overlay: project, container
and image tag (`:smoke`) differ from its base file, and the agent joins the
server's `derhost-smoke-net`. The run is disjoint by name from `stack-up`, so
it may run while a developer's stack is up. It refuses before building when a
`derhost-smoke*` object already exists (a killed or concurrent run), and
`server` or `smoke` in `AGENT_DIRS` is refused before any docker call, and so
is an empty `AGENT_DIRS`. Agents are built by their own `build.sh` with the
overlay, so the `:smoke` image is built directly and `derhost-<dir>:local` is
never written, retagged or removed. `derhost-base:local` is the exception: each
agent image builds on it, so a run builds it when it is missing and leaves it
(and any `:local` image) in place; only the `:smoke` tags are removed.

Only one smoke run goes at a time on a host: `smoke.sh` takes an exclusive
`flock` on `${XDG_RUNTIME_DIR:-/tmp}/derhost-smoke.lock` right after it checks
its inputs and before its first docker call, and a second run stops with
"another smoke run holds <path>". Set `DERHOST_SMOKE_LOCK` to use another
path. The lock is released when the run ends by any route, a kill included. A
host without `flock` refuses to run rather than running unlocked.

`INT` and `TERM` run the teardown and end the run with status 130 and 143. They
are caught from before the first compose call. The config, build and up steps,
the agent builds included, are interrupted at once; the check, port and
teardown steps finish first, each within its own timeout. A second signal
during teardown is noted and ignored: the teardown finishes first. A teardown
step that fails is named, the remaining objects are still counted, and the run
exits non-zero (with the original status when it had one).

### Job probes

Each agent directory holds a `smoke-probe.json`; a listed directory without
one fails the run. `docker/lib/smoke_probe.py` (standard library, run by host
`python3`) reads it after every identity has connected and before the restart:

```
{"identity": "der.rtc", "kind": "rpc", "method": "list_modes", "args": [],
 "expect": {"type": "list", "min_length": 1}}
```

`identity` must equal the `--identity` in the agent's Dockerfile. `kind: rpc`
calls `POST /api/rpc/call` with `agent_id`, `method`, and `args` and
`kwargs`, both optional (default `[]` and `{}`). Keys other than `identity`,
`kind`, `method`, `args`, `kwargs` and `expect` are refused. A failed call
still answers HTTP 200 with `success: false` and an `error`, so the probe
passes only on `success: true` and a `result` that meets `expect` (`type`:
list, dict, str, int, float or bool; `min_length`: list, dict or str only).
`kind: topic` is refused with a named error: it needs a WebSocket client and
neither agent publishes.

The interpreter ignores proxy environment variables, refuses a redirect
rather than following it, and bounds the whole call (not each read) by
`--timeout`, which defaults to 35 s, above the server's own 30 s RPC timeout.
A directory without `smoke-probe.json` is refused with the other directory
checks, before anything is built.

Timeouts, all in seconds and all set through the environment:

- `DERHOST_SMOKE_STEP_TIMEOUT` (120): each compose step, each check and each
  job probe.
- `DERHOST_SMOKE_BUILD_TIMEOUT` (600): each agent's `build.sh`. The server
  build uses the step timeout.
- `DERHOST_SMOKE_WAIT_TIMEOUT` (60): the health wait on `up` and the wait for
  every identity to connect.
- `DERHOST_SMOKE_RESTART_WINDOW` (10): how long the restarted agent has to
  show as absent after the stop, and again to reconnect after the start.
- `DERHOST_SMOKE_POLL_SECS` (1): the pause between checks while waiting.

CI does not build or run containers here, so
the author runs it locally and pastes its output into the PR. If
`127.0.0.1` does not answer on the host, publish on another loopback address:

```
DERHOST_PUBLISH_HOST=127.0.0.2 make stack-smoke
```
