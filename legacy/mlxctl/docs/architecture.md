# mlxctl architecture

mlxctl is a per-user local inference manager for Apple silicon. It manages
runtimes, exact model revisions, named inference services, one stable loopback
Gateway, and the Supervisor that owns durable work and live processes.

## Dependency direction

Dependencies point inward:

```text
interfaces -> application -> domain
infrastructure ----^          ^
```

- `domain` defines immutable resource identities, desired and observed states,
  evidence, validation, and domain errors. It imports no adapters.
- `application` defines typed commands, queries, outcomes, progress events,
  policies, and one operation and capability catalogue. It depends only on
  domain types and declared ports.
- `infrastructure` implements application ports for TOML configuration, SQLite
  state, Hugging Face Hub access, uv runtime environments, process and launchd
  control, probes, logs, and the Gateway.
- `interfaces` adapts the application operations to the Typer CLI, Textual TUI,
  Unix-socket control protocol, and JSON or NDJSON output. Interfaces contain
  no product behavior.

The CLI and TUI derive help, availability, confirmation, progress,
remediation, and contextual actions from the same operation catalogue. Parity
is a tested application contract, not duplicated UI work.

## Process boundaries

`mlxctl` performs local read-only queries and configuration edits without
starting any process. It can inspect config, persisted observations, launchd,
the control socket, and process identity while the Supervisor is stopped.

`mlxd` owns:

- the Supervisor and durable operation runner;
- the Gateway and its routes;
- Service Runs and private upstream ports;
- model and runtime installation, update, verification, repair, move, and
  pruning jobs;
- live observations, logs, metrics, and operation progress.

An explicit mutating command may start `mlxd` when required and reports that
action. Read-only commands never start it. The Supervisor remains running until
the user explicitly stops it. Stopping it drains the Gateway and Service Runs,
persists terminal state, and ends bounded operations safely.

Long operations are durable Supervisor jobs. The invoking CLI or TUI may wait,
detach, reconnect, stream progress, or request cancellation. Operation journals
make supported work resumable after interruption.

## Persistence boundaries

- Strict round-trip TOML stores desired per-user, per-machine configuration.
  Semantic edits are locked, fully validated, backed up, and atomically
  replaced.
- SQLite in WAL mode stores operation journals, observed resource and run
  state, versioned snapshots, and metrics.
- Runtime logs are append-only and correlated to Service Run IDs.
- Runtime Installations are immutable side-by-side environments under the
  per-user data directory.
- Model bytes stay in official Hugging Face or declared local caches. mlxctl
  records Model Installations, aliases, provenance, and references without
  claiming ownership of shared blobs.

## Control protocol

The local control protocol uses a mode-0600 Unix socket and verifies the peer's
user identity. Framed JSON messages carry a protocol version, request and
operation IDs, typed parameters, progress events, results, stable error codes,
and cancellation requests.

Protocol data-transfer objects are separate from domain types. Version
negotiation fails clearly before an incompatible command runs. Human prose and
terminal layout are not protocol contracts.

## Runtime management

Runtime Definitions and mlxctl-tested lock assets ship as package data for
`mlx_lm`, `mlx_vlm`, and `optiq`. A default Runtime Installation uses an exact
tested lock for its platform and Python line. A user may request another
upstream version as a probed `custom` installation.

Each Runtime Installation is staged in a new uv virtual environment, populated
with `uv pip sync`, probed for executable identity and semantic capabilities,
and atomically registered. Updates install side by side and switch only after
referenced Inference Services validate. Rollback keeps the previous
installation until it is safely pruned.

Launch options are negotiated against the exact installation before process
creation. An option is never emitted merely because its Runtime Definition
recognizes the name. OptiQ and the `mlx_lm` installation it delegates to are
recorded and probed as one compatibility bundle.

## Model management

Catalog and model operations use `huggingface_hub` APIs for search,
exact-revision snapshots, offline lookup, cache inventory, and safe deletion.
Installations are resumable, verify complete snapshot provenance, and become
ready atomically.

Model Alias removal, Model Installation uninstall, and Cached Revision
eviction are separate operations. Shared bytes are removed only through the
official cache API after reference and ownership checks.

Compatibility Assessments bind an exact Model Revision, Runtime Installation,
launch-option set, and machine. Evidence remains reported, declared, derived,
validated, conflicting, or unknown. Trust grants bind the exact revision,
accepted risk set, and runtime installation and never override known security
findings or integrity failures.

## Gateway

The Gateway is a Starlette ASGI application served inside `mlxd`. A
lifespan-managed HTTPX client streams requests and responses without buffering
model output. The Gateway binds only to configured loopback addresses and
routes the OpenAI-compatible `model` field by Inference Service name.

Each Service Run receives a private dynamic Upstream Endpoint. The Gateway
stays healthy when an upstream fails, reports per-service readiness through
`/v1/models`, and returns stable actionable errors for stopped or unavailable
services. Requests never start services implicitly.

Request telemetry records identity, timing, token totals, outcome, and resource
correlation. It does not record prompt or response content by default.

## Resource admission and pressure

Before starting an Inference Service, mlxctl combines exact model weight and
auxiliary evidence, architecture-aware KV and runtime-state projections,
requested context and concurrency, current Service Run measurements, system
memory and pressure, and a configurable system reserve. The resulting fit is
likely, borderline, no-fit, or unknown and always includes its assumptions.

Likely fit starts normally. Borderline or unknown fit requires confirmation or
an exact noninteractive override. No-fit requires an operator-approved
transition plan that names affected services or settings; a generic force flag
cannot hide the risk. Per-service request queues are bounded and return a
stable retryable response when full.

Critical memory pressure immediately blocks new starts and sheds new Gateway
work while bounded in-flight requests finish. The Supervisor may then stop the
least-recently-used idle Service Runs until pressure recovers, but never stops a
Pinned Inference Service automatically. If only pinned or busy services remain,
it keeps shedding work and presents an exact operator stop plan. Every pressure
decision and lifecycle action is journaled and visible in CLI and TUI.

## User interfaces

The CLI uses Typer and Rich for nested resource commands, shell completion,
contextual help, TTY-aware human output, and deterministic versioned JSON or
NDJSON.

The TUI uses Textual screens, a command palette, reactive snapshots,
background workers, contextual actions, progress, confirmation, and
notifications. It calls the same application operations as the CLI.

Successful observation commands exit zero even when they report stopped,
degraded, or unhealthy state. Invalid invocation or failed observation exits
nonzero. Explicit `mlxctl check` requirements own readiness and health policy
pass, fail, and error exits.

## Verification seams

Tests exercise externally meaningful boundaries:

1. installed CLI subprocess stdout, stderr, and exit status;
2. the versioned Unix-socket protocol;
3. public TOML load and save behavior;
4. exact runtime argv against fake executables;
5. loopback Gateway HTTP and streaming behavior;
6. PTY-driven TUI flows and the pure screen-state model.

Unit tests may support these seams, but private helper structure is not a
compatibility contract.
