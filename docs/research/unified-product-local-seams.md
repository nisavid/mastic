# Historical unification research: local seams

## Scope and source baseline

This note preserves the pre-unification audit of the two source repositories.
It does not describe the current MASTIC source tree or choose a final process
architecture.

- [`systools` source](https://github.com/nisavid/systools/tree/662567b47987b615eefb0c7f12dbeb82aad8deff): `662567b47987b615eefb0c7f12dbeb82aad8deff`
  (`origin/main`, `feat(mastic/clients): apply Qwen generation profiles`).
- [`agents` source](https://github.com/nisavid/agents/tree/d7a4b62bc835012873aca95f2463b1e4e8c02c00): `d7a4b62bc835012873aca95f2463b1e4e8c02c00`
  (`origin/main`, `fix(responses-adapter): reject ambiguous history calls`).
- `responses-adapter` is owned by `tooling/responses-adapter/` in `agents`; its
  implementation, README, and tests are all inside that directory.

All `tools/mastic/...` and `tooling/responses-adapter/...` references below are
historical coordinates within those linked commits, not paths in this repository.
Historical identifiers such as `ClientSettings` and `client` are quoted only
when they name the audited source; current MASTIC vocabulary is defined in
`CONTEXT.md`.

## Executive finding

The implementations are neither two complete competing products nor cleanly
disjoint. `mastic` already contains the broad product shell and local-inference
control plane: one CLI/TUI operation catalogue, guided desired-state planning,
runtime and model supply, a per-user Supervisor, local service routing,
admission and pressure policy, reversible Application Configuration Target
state, and macOS
activation. `responses-adapter` is a narrower data-plane component: a single-
upstream authenticated Responses gateway with a distinct upstream credential,
provider transport support, exact namespace-tool adaptation, bounded
continuation state, SSE heartbeats, and fail-closed request handling.

Their largest overlap is HTTP gateway plumbing. Their semantic transforms are
complementary: `mastic` injects selected workload/model parameters and routes
by service, while `responses-adapter` adapts a client capability to a provider
capability and reconstructs the response. The current source therefore leaves
room for one user-facing entrypoint while retaining several processes and
adapter stages behind it; it does not force the Supervisor, router, engine
processes, and provider compatibility adapter into one executable.

## Current process and ownership graph

```text
human / automation
        |
        v
mastic CLI or TUI                     responses-adapter.py (direct execution)
        |                                      |
 local queries/config edits                     | one authenticated HTTP seam
        |                                      v
        +-- Unix socket --> masticd              one HTTP(S) provider upstream
                              |
                              +-- Supervisor policy and lifecycle
                              +-- in-process Gateway thread
                              +-- runtime/model physical operations
                              +-- child inference-engine processes
                                      |
                                      +-- private dynamic loopback endpoints
```

`mastic` and `masticd` are already separate installed entry points, with no-arg
`mastic` opening the TUI (`tools/mastic/pyproject.toml:25-27`;
`tools/mastic/src/mastic/entrypoints.py:15-55`). The architecture explicitly
assigns local reads and configuration edits to `mastic`, while `masticd` owns the
Supervisor, Gateway, child processes, physical work, observations, logs, and
metrics (`tools/mastic/docs/architecture.md:32-55`). `responses-adapter` currently
has no control-plane or installer entrypoint: its documented operator surface
is environment variables plus direct Python execution
(`tooling/responses-adapter/README.md:40-118`).

## Responsibility matrix

| Surface | `mastic` today | `responses-adapter` today | Relationship |
| --- | --- | --- | --- |
| User-facing CLI/TUI | Full CLI and Textual TUI from one operation catalogue | None | Complementary |
| Desired-state configurator | Strict TOML for Gateway, runtimes, models, aliases, services, and Application Configuration Targets; guided plans and confirmations | Process-local environment configuration for one run | Overlapping configuration concern, different maturity and scope |
| Controller/Supervisor | `masticd` reconciles desired services, runs, Gateway state, pressure, durable operations, and observations | No controller; only one server process and bounded in-memory transform state | Complementary |
| Inference-engine adapters | Built-in runtime definitions, exact installs, capability probes, runtime-specific argv, and local child-process lifecycle | None; upstream is assumed already available | Complementary |
| Gateway/router | Multi-service local route table, readiness, admission, request profiles, `/v1/models`, Chat Completions, and Responses | Single configured upstream, `/v1/models` and Responses only, provider transport, namespace transform | HTTP plumbing duplicated; routing and transforms complementary |
| Application Configuration Target adapters | Reversible Codex and Hindsight config plus owned model metadata and profile endpoints | Codex wire-shape namespace adapter, not application configuration | Complementary adapter categories |
| Provider adapters | Only inference-runtime launch adapters and generic loopback OpenAI-compatible forwarding | Identity or `codex-namespace` provider-compatibility mode, remote HTTP(S), distinct upstream bearer | Missing/general on `mastic`; narrow/specific on proxy |
| Credentials | One persistent owner-only local Gateway credential; no upstream provider credential is forwarded | Per-run inbound bearer separated from optional upstream bearer | Same trust-boundary concern, distinct contracts |
| Protocol/capability transforms | Workload sampling and chat-template projection; route/model selection | Namespace-tool flatten/reconstruction and continuation mapping | Complementary |
| Host activation | Generated inactive per-user LaunchAgent, explicit activation, Unix control socket | None in this repository | Complementary |

## Factual seams

### 1. User-facing shell and desired-state configurator

The `mastic` operation catalogue is the explicit CLI/TUI parity contract. It
contains setup, diagnostics, Supervisor, Gateway, runtime, model, service, the
historical `client` target, operation, and configuration surfaces, and classifies whether each
operation may start the Supervisor (`tools/mastic/src/mastic/application/catalogue.py:52-216`).
The CLI and TUI call the same dispatcher; the TUI exposes every catalogue
operation in its command palette (`tools/mastic/src/mastic/interfaces/tui.py:48-78`).

Desired state is already wider than “MLX launch flags.” The immutable schema
has Gateway settings, exact Runtime Installations, Model Installations and
aliases, Inference Services, and the historical `ClientSettings` target model with sampling profiles
(`tools/mastic/src/mastic/application/config_schema.py:29-87`). The setup planner
selects a machine-fitting recommendation or explicit exact selection, applies a
capacity profile, creates fingerprinted ordered steps, and returns a complete
editable preview before execution (`tools/mastic/src/mastic/application/setup.py:372-528`).
The production setup currently hard-codes one OptiQ/Qwen recommendation and
Codex/Hindsight Application Configuration Target set, showing that the configurator is architecturally
general inside the current domain but its first-party recommendations are not
yet multi-host or arbitrary-adapter configuration
(`tools/mastic/src/mastic/infrastructure/production.py:681-780`).

`responses-adapter` has no equivalent desired-state store or user workflow.
`ProxyConfig` validates one upstream, one listener, two credentials, transport
bounds, one adapter choice, bounded mapping capacity, and debug mode from
`MASTIC_RESPONSES_ADAPTER_*` environment variables (`tooling/responses-adapter/responses-adapter.py:86-165`).
That is a useful process configuration contract, but not yet a resource model,
adapter catalogue, or persistent configurator.

### 2. Supervisor/controller

The `mastic` Supervisor is an orchestration-policy object behind injected
desired-state, runtime-supply, persistence, Gateway, process, probe, pressure,
and clock ports (`tools/mastic/src/mastic/infrastructure/supervisor_v1.py:1-6,124-205`).
It owns exactly one Gateway and concurrent named Service Runs
(`tools/mastic/src/mastic/infrastructure/supervisor_v1.py:217-267`). Starting the
Supervisor starts the Gateway, describes every desired route, recovers verified
children, and activates services whose policy is `supervisor`; starting a
service allocates a loopback port, asks the runtime-supply adapter for exact
argv, launches and probes the process, then publishes the route only after
readiness (`tools/mastic/src/mastic/infrastructure/supervisor_v1.py:276-316,344-449`).

The foreground daemon routes only daemon-owned runtime, model, Supervisor,
Gateway, and service operations, persists physical-operation/lifecycle state,
and drains active physical work before stopping
(`tools/mastic/src/mastic/infrastructure/daemon_service.py:30-204`). It exposes
that owner through a private Unix control service and a periodic maintenance
loop (`tools/mastic/src/mastic/infrastructure/daemon_service.py:299-405`).

`responses-adapter` does not reconcile anything external. Its only durable-like
state is a bounded, process-local LRU mapping from Responses IDs to namespace
reconstruction maps (`tooling/responses-adapter/responses-adapter.py:335-358`). It can
therefore be supervised by another controller without duplicating the current
Supervisor’s desired/live-state policy.

### 3. Inference-engine adapters

`mastic` already separates generic lifecycle policy from runtime-specific
knowledge. Built-in `RuntimeDefinition` objects name the package, launcher, and
semantic-to-flag option map; exact `RuntimeInstallation` objects carry version,
provenance, launcher, and observed capabilities
(`tools/mastic/src/mastic/infrastructure/runtime_supply.py:26-80`). The catalogue
loads packaged definitions and exact tested bundles, while the launch builder
refuses an option that is not both defined and observed on the exact install
(`tools/mastic/src/mastic/infrastructure/runtime_supply.py:197-307`). Production
composition injects those adapters into `ExactRuntimeLaunchSupply` and then the
Supervisor (`tools/mastic/src/mastic/infrastructure/production.py:531-650`).

The engine process boundary is already real and should not be confused with
the Gateway process boundary: each service receives its own validated argv and
private dynamic loopback port. The stable Gateway routes to that endpoint only
after readiness. `responses-adapter` contains no engine install, launch, probe,
memory-pressure, or model-cache logic.

### 4. Gateway and router

The `mastic` Gateway owns a dynamic route table keyed by stable service name,
but only permits private upstream endpoints that are literal HTTP loopback
origins (`tools/mastic/src/mastic/infrastructure/gateway.py:276-311,344-377,499-518`).
It exposes `/v1/models`, `/v1/chat/completions`, `/v1/responses`, and the same
routes under consuming-application workload-profile prefixes
(`tools/mastic/src/mastic/infrastructure/gateway.py:410-432`). It also owns
per-service admission, pressure shedding, activity accounting, and stream
closure at Responses or `[DONE]` terminal events
(`tools/mastic/src/mastic/infrastructure/gateway_runtime.py:29-69,154-219`;
`tools/mastic/src/mastic/infrastructure/gateway.py:71-161`).

`responses-adapter` routes only `GET /v1/models` and `POST /v1/responses` to one
configured upstream (`tooling/responses-adapter/responses-adapter.py:27-31`). Unlike
the current `mastic` Gateway, that upstream may be remote HTTP or HTTPS and may
receive a separate bearer (`tooling/responses-adapter/responses-adapter.py:86-124,710-799`).
It has no model/service route table, readiness model, lifecycle action, or
admission policy.

Both implementations authenticate a loopback listener, filter forwarded
headers, bound input/transport behavior, proxy JSON and SSE, recognize terminal
stream conditions, and avoid forwarding the inbound credential. This is the
main duplicated implementation surface. The implementations differ in detail:
`mastic` uses Starlette/HTTPX/Uvicorn and its Gateway is a thread inside `masticd`,
whereas `responses-adapter` is a standard-library threaded HTTP server and a
standalone process (`tools/mastic/src/mastic/infrastructure/gateway.py:178-205`;
`tools/mastic/src/mastic/infrastructure/gateway_runtime.py:75-103`;
`tooling/responses-adapter/responses-adapter.py:1021-1092`).

### 5. Application-target, provider, and protocol/capability adapters

The audited `mastic` “Client Integration” adapter configures a consuming
application. For Codex it sets the service model, local provider, workload-
profile base URL, Responses wire API, credential-reader command, and owned
model metadata; for Hindsight it writes per-operation Gateway base URLs and a
credential value through a reversible ownership manifest
(`tools/mastic/src/mastic/infrastructure/client_integrations.py:962-984,1056-1094`).
It is not a wire-protocol transform.

The current Gateway’s wire transform is deliberately small: on a profiled
endpoint it replaces supported sampling fields and merges
`enable_thinking`/`preserve_thinking` into `chat_template_kwargs`. Responses and
Chat Completions have different supported parameter sets
(`tools/mastic/src/mastic/infrastructure/gateway.py:312-343,443-476`). It otherwise
forwards the same OpenAI-compatible operation to the selected local engine.

The proxy’s `codex-namespace` adapter is a true client/provider capability
adapter. It flattens namespace function definitions and history, rejects
ambiguous or unsupported shapes, and reconstructs only exact names it mapped
(`tooling/responses-adapter/responses-adapter.py:168-318`). It inherits a bounded map
through `previous_response_id` and fails before forwarding if state is missing
or a name collides (`tooling/responses-adapter/responses-adapter.py:598-646`). It
performs the reverse transform in plain JSON and SSE and emits keep-alive
comments for silent streams (`tooling/responses-adapter/responses-adapter.py:801-905`).

These are complementary adapter categories that should remain named
separately during design:

- **engine adapter**: install/probe/launch one inference runtime family;
- **application configuration adapter**: safely configure Codex, Hindsight, or
  another consuming application;
- **client-protocol adapter**: normalize a client’s API request/response shape;
- **provider adapter**: authenticate and project the normalized request onto a
  provider/engine capability surface.

The current implementations sometimes combine the last two in one mode, but
the local source does not require that combination as a permanent abstraction.

### 6. Credential boundaries

`mastic` creates one persistent random Gateway token in an owner-only regular
file, validates owner and modes on every read, and uses constant-time bearer
comparison (`tools/mastic/src/mastic/infrastructure/gateway_credential.py:15-72,86-133`).
Production composition injects that credential into the Gateway and points
managed Application Configuration Targets to it (`tools/mastic/src/mastic/infrastructure/production.py:400-455,625-631`).
The local engine upstream receives no separate bearer: the Gateway request
header allowlist excludes authorization
(`tools/mastic/src/mastic/infrastructure/gateway.py:21-27,366-377`).

`responses-adapter` instead requires a per-run inbound token and optionally accepts
an independent upstream token. It rejects equal credentials, strips inbound
authorization, and injects only the configured upstream bearer
(`tooling/responses-adapter/responses-adapter.py:86-124,648-653,781-799`). This trust
boundary is additive if the unified product supports authenticated or remote
providers; it is not represented in current `mastic` desired state.

### 7. Host activation

The deployment boundary is already explicit. A deployment owner may install
the package and inactive LaunchAgent, but `mastic` owns desired state, runtimes,
models, services, Application Configuration Target state, operational state, and lifecycle
(`tools/mastic/docs/deployment-contract.md:1-18,55-75`). The LaunchAgent is
registered with `RunAtLoad=false` and `KeepAlive=false`; a mutation that needs
`masticd` activates it, while reads never do
(`tools/mastic/docs/deployment-contract.md:20-36`). Production composition
generates the LaunchAgent target and controls activation through an injected
adapter (`tools/mastic/src/mastic/infrastructure/production.py:400-424,668-678`).

No activation artifact or lifecycle integration for `responses-adapter` exists in
the audited `agents` tree. A unified user experience would therefore need to
decide whether provider/gateway adapter processes are owned by the existing
Supervisor, an external service manager, or a different controller; this is a
decision, not a fact resolved by current source.

## Duplicated and complementary responsibilities

### Duplicated enough to demand an explicit architecture decision

1. Authenticated loopback HTTP server and bearer validation.
2. `/v1/models` and `/v1/responses` allowlisting and forwarding.
3. Header and credential filtering.
4. JSON-body sizing and transport timeouts.
5. HTTP/SSE streaming, terminal-event recognition, and downstream disconnect
   cleanup.
6. Safe operational diagnostics that avoid prompts, outputs, tool arguments,
   and credentials.

Keeping both HTTP implementations is possible, but it creates two places to
maintain the same security and stream-lifecycle properties. Unifying the
transport core is also possible, but must preserve the proxy’s remote HTTPS,
upstream-auth, heartbeat, and continuation guarantees as well as the current
Gateway’s route/admission/pressure contracts.

### Complementary and currently owned by only one side

- `mastic`: machine-aware recommendation, exact runtime/model supply, desired
  state, operation planning, Supervisor lifecycle, private dynamic engine
  endpoints, route readiness, concurrency/pressure, TUI/CLI, reversible application-target
  configuration, persistent local credential, host activation, logs, metrics,
  and diagnostics.
- `responses-adapter`: remote/provider upstream support, strict inbound/upstream
  credential separation, namespace-tool compatibility, response-ID continuation
  mapping, silent-SSE heartbeats, and fail-closed transform ambiguity handling.

## Process separation allowed by the current seams

The source supports, but does not select among, these boundaries:

1. **User entrypoint separate from controller.** This is already the supported
   shape: `mastic` talks to `masticd` over a private versioned Unix socket.
2. **Inference engines as separate child processes.** This is already required
   by per-installation argv, per-run identity, private ports, and readiness
   probing.
3. **Gateway in or out of the controller process.** It is currently a thread in
   `masticd`, but the Supervisor depends on a `GatewayRunner` protocol rather than
   directly on Starlette/Uvicorn
   (`tools/mastic/src/mastic/infrastructure/supervisor_v1.py:176-194,217-260`).
   That seam can represent an in-process object or an IPC-backed process.
4. **Provider compatibility as a separate adapter stage.** The proxy already has
   a complete HTTP downstream/upstream seam and no lifecycle dependency. It can
   remain a supervised process behind one user entrypoint. Current `mastic`
   cannot wire that topology unchanged, because its Supervisor publishes the
   runtime endpoint directly and its Gateway only accepts loopback upstreams;
   desired state and reconciliation would need an explicit adapter-chain model.
5. **One router with per-route adapters.** The existing Gateway already resolves
   service and workload profile before forwarding. Moving namespace/provider
   transforms behind injected per-route adapters would remove duplicated HTTP
   hops, but the current source has no such adapter interface and therefore does
   not prove this is the correct final boundary.

Controller, Gateway/router, and adapter topology should therefore be decided
independently. A single user-facing entrypoint does not imply a single daemon,
and a generic controller does not imply generic engine or protocol adapters.

## Repository placement facts

### `systools`

Repository policy treats every `tools/<tool>/` directory as an independent
product boundary and requires its source, tests, package metadata, locks,
documentation, and license to stay inside that directory. An ordinary new tool
belongs under `tools/<tool>/`; a nested repository or submodule is explicitly a
separate project (`AGENTS.md:3-18`). The root README describes `systools` as
small, focused system/infrastructure tools with per-tool package, tests,
documentation, and release boundaries (`README.md:1-8`).

`mastic` already satisfies that physical boundary: it has its own package,
entrypoints, lock, docs, tests, and license. Its current stated product is an
Apple-silicon local inference manager (`tools/mastic/README.md:1-18`), so a
cross-host, multi-engine, multi-client product would require a deliberate
change to that product context even if it remains physically in `systools`.

### `agents`

The `agents` README defines that repository as the source of truth for reusable
agent assets shared across local harnesses, with plugins and agent-supporting
tooling (`README.md:1-24`). `responses-adapter` is currently a self-contained
`tooling/` leaf with one script, README, and test module; it is not listed as a
top-level product in that README and has no package or release metadata of its
own at `d7a4b62`.

This placement fits an agent-specific compatibility experiment. The repository
statement does not establish ownership of general inference lifecycle,
machine-aware recommendations, non-agent clients, or system service management.
Those broader responsibilities would require an explicit repository-scope
decision rather than following automatically from the current proxy location.

### Dedicated repository

The local policies neither require nor forbid graduating the product to a
dedicated repository. Two facts make extraction mechanically plausible:

- `tools/mastic/` is already an independent product/release boundary inside
  `systools` (`AGENTS.md:10-14`; `README.md:6-8`).
- `tooling/responses-adapter/` is already a self-contained leaf in `agents`.

A dedicated repository would have to adopt the owning policies now supplied by
`systools` (context mapping, docs contracts, validation, packaging) and preserve
the external deployment contract. Conversely, keeping the project in either
existing repository requires that repository’s stated purpose to match the
settled destination. The source audit cannot decide that product-identity
question for the operator.

## Decisions still genuinely open

The local code resolves implementation facts but not these product decisions:

1. Whether the durable product identity is local inference operations, a
   portable inference fabric, agent infrastructure, or a broader client-to-
   provider compatibility system.
2. Whether provider compatibility runs as one generic adapter pipeline,
   per-provider processes, per-engine sidecars, or route-local in-process
   adapters.
3. Whether the Gateway remains inside the Supervisor, becomes its own supervised
   process, or is delegated to an existing router/gateway product.
4. Whether remote providers and local engines share one route model and
   credential model.
5. Whether `responses-adapter` remains a named compatibility component, becomes a
   Responses adapter, or is absorbed into a generic provider-adapter layer.
6. Which repository purpose best matches the settled product identity.

Those should be resolved through research and grilling before migration,
renaming, or committed architecture documentation.

## Verification

The following focused contract suites passed against the source baselines:

- `mastic`: Gateway, Supervisor, application-target integrations, application dispatch,
  and TUI — 80 tests.
- `responses-adapter`: the complete proxy test module, covering authentication,
  request bounds, credential separation, namespace transforms and collisions,
  continuation state, plain/SSE reconstruction, heartbeats, disconnects, and
  shutdown behavior.
