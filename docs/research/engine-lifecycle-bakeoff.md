# Engine lifecycle bake-off: imported Supervisor and llama-swap

Research date: 2026-07-17

## Question

How do the imported `mlxctl` Supervisor and current `llama-swap` compare behind
a MASTIC-owned engine lifecycle contract for readiness, load and unload
behavior, TTL, grouping, eviction, host safety, observability, failure recovery,
cross-platform fit, and replaceability?

This note answers that factual question. It does not select the Phase 1
lifecycle implementation; that human decision remains in
[issue 9](https://github.com/nisavid/mastic/issues/9).

## Baselines and method

- Imported implementation: MASTIC
  [`67e9aa43624f48026e46c93d317332676cd009d2`](https://github.com/nisavid/mastic/tree/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl),
  preserving the accepted `mlxctl` source and behavior under `legacy/mlxctl`.
- External implementation: `llama-swap`
  [v240](https://github.com/mostlygeek/llama-swap/releases/tag/v240), commit
  [`6b5320de5dc7f6f9f3266afd37a707279be271ca`](https://github.com/mostlygeek/llama-swap/tree/6b5320de5dc7f6f9f3266afd37a707279be271ca),
  published 2026-07-15. The tag and upstream `main` were the same commit when
  inspected.
- Evidence: commit-pinned source and documentation, tagged release artifacts,
  maintainer issue statements where source alone did not express the operating
  boundary, and scoped test execution.
- Validation: 42 imported lifecycle/admission/gateway/control/launchd tests
  passed; scoped `llama-swap` process, router, server, config, and performance
  package tests passed. The exact commands were:

  ```sh
  cd legacy/mlxctl
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run --no-project --python 3.13 python -m unittest tests.test_supervisor_v1 tests.test_domain_admission_v1 tests.test_gateway_runtime_v1 tests.test_control_protocol_v1 tests.test_launchd_v1
  ```

  ```sh
  go test ./internal/process ./internal/router/... ./internal/server ./internal/config ./internal/perf
  ```

No target-host endurance, memory-pressure, model-load latency, or throughput
benchmark was run. Claims below describe shipped contracts and exercised test
surfaces, not comparative performance.

## Factual result

The two implementations are not interchangeable lifecycle engines today.

The imported Supervisor is an explicit, control-plane-oriented orchestrator.
It validates an exact runtime launch, starts a named service only through a
lifecycle mutation, publishes a loopback route only after readiness, applies
live memory-pressure policy, records durable operations and run identity, and
can reattach a verified child after controller restart. It has no TTL or static
compatibility-group solver, targets Apple-silicon macOS, and currently owns an
embedded Gateway as well as process lifecycle.

`llama-swap` is a request-driven proxy, scheduler, and process manager in one
binary. An inference request selects a model, computes evictions, starts the
target, waits for readiness, and then proxies the request. It has idle TTL,
legacy groups, a compatibility matrix with eviction costs, request-aware swap
queuing, multi-platform releases, a web UI, Prometheus metrics, and detailed
activity telemetry. It has no host-memory admission policy, exact runtime
capability attestation, persistent child adoption, or lifecycle-only public
activation/load API. It does expose lifecycle-only running and unload APIs.
Its default listener, authentication, and request-capture settings are also not
MASTIC's loopback-private defaults.

The central seam mismatch is activation semantics: route resolution in the
imported Supervisor deliberately never starts a stopped service, while
`llama-swap` normally loads as a side effect of the first routed request.
MASTIC therefore cannot treat either implementation as a drop-in component
without an adapter that declares its capabilities and preserves one explicit
product contract.

## Comparison matrix

| Dimension | Imported `mlxctl` Supervisor | `llama-swap` v240 | Measured implication |
| --- | --- | --- | --- |
| Activation | Explicit `service.start`; status and route resolution never activate | First matching request normally triggers eviction, start, readiness wait, and proxy; startup preload internally issues a routed request | The same route lookup has different activation effects unless an adapter shields the difference |
| Readiness | Exact launch is validated first; polls a literal-loopback `/v1/models` endpoint for any 2xx; route stays unavailable until ready | Per-model `checkEndpoint`, default `/health`; `none` disables probing; only HTTP 200 is ready; startup exit/timeout kills the process | Probe endpoint, accepted status, disabled-probe behavior, and route-publication point are not equivalent |
| Stop and drain | Has separate bounded `drain_service` and `stop_service`; remove drains then stops. Current stop/restart operations do not invoke drain despite help text saying they do | Scheduler defers ordinary evictions while a conflicting model is serving. Explicit unload stops immediately without waiting for in-flight requests; callers may see proxy errors | A common `stop` name would hide different in-flight behavior |
| TTL | None in the Supervisor | Global/per-model idle TTL; zero disables; a one-second loop skips unload while requests are in flight and records last use when a routed request handler returns | Only one candidate provides idle unload |
| Concurrency groups | Concurrent named services; pinned/busy state affects pressure eviction, but no declared compatibility groups | Legacy `swap`/`exclusive`/`persistent` groups or a newer matrix, never both as active policy | Declared compatibility and observed host capacity are different inputs |
| Eviction | At critical host memory pressure, sheds new work and stops LRU idle unpinned services; busy/pinned survivors become an operator stop plan | Request-driven group or matrix planner; matrix chooses a valid set with lowest configured eviction cost; no live-memory fit check | One evicts from observed pressure; the other evicts from declared combinability and cost |
| Host safety | Literal loopback ports, allowlisted environment, exact argv without a shell, exact-install capability validation, private logs, owner-authenticated control socket, inactive per-user LaunchAgent | Sanitized argv without a shell, process-group/tree teardown, optional `cmdStop`, API keys, and configurable listener; child processes inherit the manager environment and configured additions are printed at debug; defaults are `:8080`, no required key, and 5 MiB request/response capture | Using `llama-swap` under MASTIC's existing private loopback profile would require explicit bind/auth/capture/environment/logging configuration |
| Observability | Durable operations, events, run snapshots, process state, pressure metrics, bounded private service logs, CLI/TUI status and diagnostics | Web UI, running-state API, buffered/streaming logs, activity metrics, request cancellation, system/GPU performance, and Prometheus metrics | Detail, retention, export, and content-capture defaults differ |
| Failure recovery | Detects unexpected exit and marks failed; manual start can create a new run. Persists PID plus birth token and reattaches only the same live process, but restores its route without rerunning readiness. No automatic crash restart | Failed start returns to stopped; unexpected exit returns to stopped; a later request can try again. No persisted run identity or adoption after manager restart | Retry-on-demand and verified process adoption are different guarantees |
| Process-tree teardown | Starts a new session but current terminate/kill adapter signals only the direct child | Uses Unix process groups, Windows Job Objects/task termination, graceful timeout, and force-kill; an intentionally escaped process group needs wrapper cooperation or `cmdStop` | Direct-child, process-group, and external-stop ownership differ |
| Platform fit | Product and dependencies target Apple-silicon macOS; per-user activation is launchd-specific | v240 ships Darwin amd64/arm64, Linux amd64/arm64, Windows amd64, and FreeBSD amd64 artifacts; Homebrew supports macOS and Linux | Phase 1 host activation may be macOS-specific while the lifecycle contract remains portable |
| Replaceability | Lifecycle policy uses injected ports and a versioned local control protocol, but the concrete Supervisor also owns the Gateway | External MIT-licensed single binary, but lifecycle is coupled to its HTTP proxy/router, config schema, and request-triggered load path | Replacement requires an adapter boundary above both implementations, not shared internal types or borrowed HTTP routes |

## Detailed measurements

### Readiness and activation

The imported Supervisor injects desired-state, runtime-supply, operational-
state, Gateway, process, probe, pressure, and clock ports. Starting a service
rejects critical pressure, allocates a literal loopback port, asks runtime
supply for a capability-validated exact argv, launches the process, records its
birth identity, and only marks the route ready after probing succeeds.
[Supervisor ports and launch](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/supervisor_v1.py#L124-L205) ·
[start and readiness](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/supervisor_v1.py#L344-L455) ·
[production readiness probe](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/system_adapters.py#L237-L276)

Readiness is concretely `GET /v1/models` at a literal-loopback HTTP origin,
without redirects or proxy-environment inheritance, accepting a 2xx response.
This is stronger than mere port-open liveness but assumes every engine adapter
can expose that endpoint. Route inspection explicitly does not activate a
stopped service.
[readiness URL](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/system_adapters.py#L636-L657) ·
[non-activating resolution](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/supervisor_v1.py#L697-L732)

`llama-swap` constructs a process per configured model and uses a single-writer
state machine for stopped, starting, ready, stopping, and shutdown. The first
routed request enters the scheduler; a swap stops planned evictees, starts the
target if stopped, waits for `WaitReady`, and only then grants the proxy handler.
Readiness defaults to a per-model `/health`, may be disabled with `none`, and
requires HTTP 200. The default health-check budget is 120 seconds and values
below 15 seconds are raised to 15.
[request-driven routing](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/router/base.go#L452-L506) ·
[swap and readiness](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/router/base.go#L243-L272) ·
[process health check](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/process/process_command.go#L357-L513) ·
[configuration defaults](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/config/load.go#L28-L73)

There is no lifecycle-only public load endpoint in the registered routes.
Startup preload calls the local router with a synthetic request. `/running`
and the unload APIs are explicit control surfaces, but load remains coupled to
request routing.
[preload path](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/server/api.go#L264-L295)

### Stop, drain, TTL, and failure recovery

The imported Supervisor exposes separate drain and stop primitives. Drain
marks the Gateway route unavailable and waits boundedly for it to become idle;
stop marks it unavailable and terminates the process, escalating to kill.
Remove composes drain then stop. Restart composes stop then start without
drain. The user-facing catalogue currently describes both stop and restart as
draining, but the operation port calls `stop_service` and `restart_service`
directly. This is a current contract discrepancy, not a proposed behavior.
[lifecycle methods](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/supervisor_v1.py#L457-L540) ·
[operation dispatch](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/operation_ports.py#L75-L106) ·
[catalogue wording](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/application/catalogue.py#L267-L277)

`llama-swap` distinguishes request-driven swaps from explicit unloads. The FIFO
scheduler queues a swap if its planned evictee has an in-flight request. An
explicit unload instead stops the selected processes synchronously but does not
wait for their in-flight requests; the code documents that those callers may
see a reverse-proxy error and retry. TTL is safer than explicit unload on this
specific axis: its loop skips while the process-local in-flight count is
nonzero.
[swap admission and busy eviction](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/router/scheduler/fifo.go#L75-L141) ·
[explicit unload semantics](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/router/base.go#L375-L437) ·
[TTL loop](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/process/process_command.go#L267-L288)

On controller restart, the imported Supervisor can inspect durable run
snapshots and reattach only when PID and process birth token still match,
avoiding PID-reuse adoption. It restores the route as ready based on process
identity and existence; it does not rerun the readiness probe in that recovery
path. Unexpected exits become a durable failed state and are not automatically
restarted by maintenance.
[recovery and exit detection](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/supervisor_v1.py#L779-L852)

`llama-swap` returns a failed start or unexpected exit to stopped, so a later
request can attempt a fresh start. That is automatic retry-on-demand rather
than a background restart policy. Its process lifetime is tied to the router;
it does not persist PID identity or adopt a pre-existing child after a manager
restart.
[process state transitions](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/process/process_command.go#L177-L353)

### Grouping, eviction, and host pressure

The imported Supervisor can keep several named services running concurrently,
but it has no group or compatibility matrix. Under critical live memory
pressure it sheds new work, orders idle unpinned services by least recent use,
stops them until pressure clears, and presents an operator stop plan if only
busy or pinned runs remain. A start is rejected while pressure is critical.
[pressure reconciliation](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/supervisor_v1.py#L555-L625) ·
[pressure policy](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/domain/admission.py#L55-L96)

`llama-swap` offers mutually exclusive active group and matrix policies. Legacy
groups control same-group swapping, cross-group exclusivity, and persistence;
for backward compatibility a non-exclusive target does not unload a running
exclusive group. The matrix declares valid concurrent model sets. Its solver
chooses a set containing the requested model with the lowest total configured
cost of evicting currently running models, with definition order as the tie
breaker. Other members of the chosen set are not proactively loaded.
[group planner](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/router/group.go#L59-L106) ·
[matrix solver](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/router/matrix_solver.go#L41-L105) ·
[matrix validation](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/config/matrix.go#L59-L170)

Those declarations are operator-provided scheduling policy, not live resource
safety. The maintainer's group design explicitly makes users responsible for
configuration conflicts and notes that a requested model may simply fail when
resources are insufficient.
[group design constraint](https://github.com/mostlygeek/llama-swap/issues/107) ·
[maintainer resource-limit example](https://github.com/mostlygeek/llama-swap/issues/107#issuecomment-2845331341)

### Host safety and observability

The imported production adapter allowlists child environment variables,
requires literal loopback allocation, executes exact argv with `shell=False`,
starts a new session, records bounded owner-private logs, and checks process
birth identity. Its current terminate and kill methods signal only the direct
child, however; starting a new session does not itself make those methods
process-group signals.
[process adapter](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/system_adapters.py#L61-L276)

The surrounding controller uses a versioned, bounded, mode-0600 Unix control
protocol with peer-UID checks when the platform exposes peer credentials and
safe stale-socket handling. Its per-user LaunchAgent is installed privately
with `RunAtLoad=false` and `KeepAlive=false` and requires explicit activation.
[control protocol](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/control_protocol.py#L18-L20) ·
[control socket safety](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/control_protocol.py#L130-L248) ·
[launchd adapter](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/launchd.py#L62-L228)

`llama-swap` sanitizes its configured command rather than invoking a shell. On
Unix it manages a process group and escalates from `cmdStop` or SIGTERM to
SIGKILL; Windows uses Job Object and process-tree support. A deliberately
escaped child process group is outside the parent's direct group signal and
needs wrapper cooperation or an external `cmdStop`. The common forking-wrapper
path and a zero-timeout unload regression were repaired before v240.
[teardown implementation](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/process/process_command.go#L516-L610) ·
[upstream process-tree investigation](https://github.com/mostlygeek/llama-swap/issues/804) ·
[zero-timeout unload fix](https://github.com/mostlygeek/llama-swap/issues/804#issuecomment-4666290787) ·
[fix confirmation](https://github.com/mostlygeek/llama-swap/issues/804#issuecomment-4667501971)

Unlike the imported adapter's allowlist, the `llama-swap` child begins with the
manager's complete inherited environment and appends model-configured entries.
At debug level it logs the configured additions verbatim. An adapter that uses
environment variables for engine credentials or other secrets would therefore
need an explicit environment and logging policy.
[child environment and debug logging](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/process/process_command.go#L416-L425)

Its per-model reverse proxy also uses Go's ambient proxy-environment resolution
for upstream connections. The manager's `HTTP_PROXY`, `HTTPS_PROXY`, and
`NO_PROXY` environment can therefore affect transport that might otherwise be
assumed to use direct loopback.
[proxy environment behavior](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/process/process_command.go#L367-L385)

MASTIC cannot accept the binary's defaults unchanged for a private per-user
profile. `llama-swap` defaults to `:8080`, which listens beyond loopback, and
only logs a suggestion to restrict the address. Authentication is pass-through
when no API keys are configured. Even when keys are configured, `/health` and
`/wol-health` bypass the authentication chain and return controller health.
Its default activity configuration allocates a 5 MiB in-memory cache that
captures request and response headers and bodies for ordinary JSON endpoints.
Known sensitive header values are redacted, but JSON bodies are not; setting
`captureBuffer: 0` disables captures.
[listener default](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/llama-swap.go#L68-L98) ·
[listener warning](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/llama-swap.go#L317-L332) ·
[optional authentication](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/server/auth.go#L12-L40) ·
[unauthenticated health routes](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/server/server.go#L230-L249) ·
[capture default](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/config/load.go#L28-L43) ·
[capture content](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/server/captures.go#L13-L57) ·
[sensitive-header redaction](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/server/captures.go#L149-L175)

The imported stack records durable operations, events, snapshots, process and
pressure metrics, and private rotating service logs for its CLI/TUI. It does
not expose a Prometheus endpoint. `llama-swap` exposes running process state,
logs and log streams, an event feed, activity/token metrics, in-flight request
inspection and cancellation, Prometheus system/GPU metrics, and platform-
specific performance collectors. Its richer activity UI includes optional
content capture, so observability and privacy are inseparable unless MASTIC
enforces capture-off configuration.
[llama-swap routes](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/server/server.go#L201-L272) ·
[metrics and capture toggle](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/internal/server/metrics.go#L36-L60)

### Cross-platform fit and replaceability

The imported package declares its process dependency only for Darwin arm64 and
documents Apple-silicon macOS as a requirement. Its host activation adapter is
launchd-specific. Its architecture is internally replaceable in useful places:
the Supervisor is built from injected ports and the controller protocol is
versioned. The boundary is still wider than engine lifecycle because the
Supervisor directly owns Gateway routes, busy state, and request activity.
[package platform constraint](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/pyproject.toml#L13-L23) ·
[Supervisor ownership](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/mlxctl/src/mlxctl/infrastructure/supervisor_v1.py#L217-L267)

`llama-swap` is an MIT-licensed Go project with v240 release artifacts for
Darwin arm64/amd64, Linux arm64/amd64, Windows amd64, and FreeBSD amd64. Its
process machinery is broadly portable, and Homebrew supports macOS and Linux.
That operational replaceability does not create a clean lifecycle contract:
the external API exposes running and unload state, while activation, readiness,
in-flight-aware swaps, and eviction live inside its proxy/router path and YAML
model.
[v240 artifacts](https://github.com/mostlygeek/llama-swap/releases/tag/v240) ·
[license](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/LICENSE) ·
[installation targets](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/README.md#L86-L164) ·
[documented operating model](https://github.com/mostlygeek/llama-swap/blob/6b5320de5dc7f6f9f3266afd37a707279be271ca/README.md#L229-L233)

## Seam dimensions exposed by the measurements

The comparison exposes the dimensions below. Issue 9 decides which become
Phase 1 requirements and how unsupported features are represented; this ticket
does not prescribe that policy.

| Surface | Measured divergence that needs an explicit later decision |
| --- | --- |
| Capabilities | Activation, probes, drain, TTL, grouping, live pressure, adoption, platforms, and tree-stop behavior differ independently rather than as one feature flag |
| Desired instance | The Supervisor uses exact runtime identity, launch provenance, and service identity; `llama-swap` uses its native model command/proxy configuration |
| Activate | One path is an explicit correlated operation and run identity; the other is normally a request-triggered swap |
| Observe | Desired state, liveness, readiness, route state, busy state, and last-use state are available in different combinations |
| Drain and deactivate | Drain, graceful stop, force escalation, and process-tree outcome are not the same operation in either implementation |
| Reconcile | Config drift, unexpected exit, retry-on-demand, adoption proof, readiness revalidation, and orphan handling have different guarantees |
| Schedule | Static compatibility, eviction preference, concurrency admission, TTL, and live host pressure are separate inputs, and neither implementation covers all of them |
| Telemetry | Durable operation history and rich activity/performance telemetry differ; one candidate also captures request content by default |
| Safety | Bind, authentication, argv, inherited environment, debug logging, filesystem ownership, runtime attestation, and tree teardown differ |

Neither implementation can currently be replaced by changing only an
executable name or endpoint: the Supervisor's lifecycle types are coupled to
its Gateway port, while `llama-swap` activation and scheduling are coupled to
its proxy and YAML schema. Whether Phase 1 requires a shared conformance suite,
state migration, or only a narrower adapter boundary remains an issue 9 choice.

## Inputs for issue 9, not a selection

Issue 9 must choose among these policy questions with the measurements above
in hand:

1. Does Phase 1 use explicit activation, request-triggered activation, or
   explicitly support both modes?
2. May the selected lifecycle implementation also own the data-plane proxy, or
   must the MASTIC gateway and lifecycle supervisor remain independently
   replaceable?
3. Is idle TTL a Phase 1 requirement or an optional adapter capability?
4. Does safe scheduling require static compatibility declarations, live host
   pressure, or both?
5. Must every user-facing stop and restart drain, and what is the separate
   operator force path?
6. Which recovery guarantee is required: retry on the next request, background
   restart, persisted child adoption, or a declared subset?
7. Does Phase 1 accept a macOS-specific adapter behind a portable contract, or
   require the first implementation itself to cover Linux?
8. Which defaults are non-negotiable for MASTIC: loopback-only bind,
   authentication, metadata-only telemetry, request-content capture disabled,
   exact runtime capability validation, and process-tree teardown?

No answer to those questions is implied by this research ticket.
