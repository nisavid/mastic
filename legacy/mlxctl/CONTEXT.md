# Local MLX Inference Management

This context describes the resources that mlxctl manages for per-user local
inference. Keep desired state, physical artifacts, running processes, and
network addresses distinct.

## Language

**Runtime Definition**:
Built-in knowledge of a supported inference runtime family, including how to
discover, install, launch, and probe its versioned capabilities.
_Avoid_: Installed runtime, backend, provider

**Runtime Installation**:
One exact locally installed runtime version and provenance, with capabilities
observed from that installation.
_Avoid_: Runtime Definition, Server Type

**Catalog Candidate**:
A discoverable remote repository or local path that may contain a compatible
model. A candidate is not proof of compatibility or local availability.
_Avoid_: Model Installation, available model

**Model Revision**:
An immutable model-content identity, normally a repository plus commit SHA.
Local content uses an equivalent provenance manifest.
_Avoid_: Model Alias, mutable branch or tag

**Cached Revision**:
The physical local availability of some or all files for a Model Revision in a
shared cache or declared local path. Cache presence is not user intent.
_Avoid_: Model Installation, Model Alias

**Model Installation**:
A durable mlxctl-managed pin and provenance record for one exact Model
Revision. Its files may be supplied by a shared Cached Revision.
_Avoid_: Cached Revision, download directory

**Model Alias**:
A stable user-facing name that selects one Model Installation.
_Avoid_: Repository name, Model Revision

**Inference Service**:
Named desired state combining one Model Alias, one Runtime Installation,
launch options, activation policy, and Gateway route.
_Avoid_: Server Definition, process, port

**Service Run**:
One concrete activation of an Inference Service, identified by a run ID for
lifecycle, diagnostics, and metrics correlation.
_Avoid_: Inference Service, instance

**Gateway**:
The stable loopback client endpoint that routes requests by service or model
identity to private runtime upstreams.
_Avoid_: Supervisor, runtime server

**Gateway Route**:
The stable identity clients use to select an Inference Service through the
Gateway.
_Avoid_: Upstream Endpoint, runtime port

**Upstream Endpoint**:
A private loopback address allocated to one Service Run. It is not part of the
user-facing configuration contract.
_Avoid_: Gateway Route, client endpoint

**Supervisor**:
The explicitly managed per-user daemon that reconciles Inference Services,
Service Runs, the Gateway, and operational state.
_Avoid_: Gateway, runtime server, worker

**Compatibility Assessment**:
Evidence about one exact Model Revision, Runtime Installation, launch-option
set, and machine. Its state is reported, declared, derived, validated,
conflicting, or unknown.
_Avoid_: Boolean support flag, cache presence

**Probe**:
A liveness, readiness, capability, or model-introspection observation of a
running component.
_Avoid_: Ping, status request

**Request Metric Event**:
An immutable observation of one inference request's identity, timing, outcome,
and available usage totals.
_Avoid_: Request log, trace

**Process Sample**:
An immutable point-in-time observation of one Service Run's resource use.
_Avoid_: Request Metric Event

**Metric Query**:
A selection of Request Metric Events and Process Samples for grouped service,
model, runtime, and run summaries.
_Avoid_: Database query
