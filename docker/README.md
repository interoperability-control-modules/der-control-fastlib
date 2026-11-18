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

With that config the image runs no control mode: `modes` and `use_cases` are
both empty.

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
identity (`platform.presentation`, `der.rtc`), the `der.rtc` agent leaves and
rejoins within 10 s of a stop and start with a new `StartedAt`, and nothing
labelled or named `derhost-smoke` is left afterwards (the count is printed).
Job probes are not part of it yet.

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

`INT` and `TERM` run the teardown and end the run with status 130 and 143. They
are caught from before the first compose call and act at once, a build in
progress included. A second signal during teardown is noted and ignored: the
teardown finishes first. A teardown step that fails is named, the remaining
objects are still counted, and the run exits non-zero (with the original status
when it had one).

Timeouts, all in seconds and all set through the environment:

- `DERHOST_SMOKE_STEP_TIMEOUT` (120): each compose step and each check.
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
