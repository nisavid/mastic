---
status: accepted
---

# Separate declarations, lifecycle units, and adapter capabilities

MASTIC coordinates desired resources, physical lifecycle units, application
configuration, routing, and protocol adaptation. Treating a service as its
process, a route as its endpoint, or a provider as an application-native string
would make ownership and recovery depend on the current implementation.

## Decision

The canonical resource relationships are:

```text
Blueprint -> Plan -> proposed declarations
Plan + applicable Claims + selected policy + Plan Approval (when required)
  -> Policy Assessment -> Plan Disposition
Eligible Policy Assessment -> Desired State

Inference Engine -> Runtime Installation
Model Revision -> Model Installation -> Model Alias
Runtime Installation + Model Alias + Workload Profile -> Inference Service
Inference Service -> Service Run -> Managed Process
Service Run -> Provider Endpoint
Managed Process -> Process Identity

External Application -> External Application Installation
External Application -> Application Configuration Target
External Application Installation --consumed by--> Application Configuration Target
Application Configuration Target --consumes--> External Application Installation
Application Configuration Target -> Application Provider Binding
Application Provider Binding -> Client Protocol + Inference Route
Inference Route -> Client Protocol + Workload Profile
Inference Route -> Inference Service or Inference Provider
Inference Provider -> Provider Endpoint
Route Publication -> Inference Route + Provider Endpoint
service-backed Route Publication -> Service Run
```

The ownership and selection edges are cycle-free: an Inference Service never
owns or references an Inference Route. The two named consumption associations
record a many-to-many relationship and do not participate in ownership,
selection, or lifecycle-dependency traversal. A route's Client Protocol must
equal the Application Provider Binding's protocol, and a route to an Inference
Service must name the same Workload Profile fingerprint as that service. A
Route Publication may select only a Provider Endpoint owned by a Service Run
of the route's Inference Service or by the route's Inference Provider. A
service-backed publication also binds that owning Service Run. Managed Process
observations bind a PID-reuse-safe Process Identity rather than a PID alone.

An Inference Route binds one immutable route-owner identity and declared scope.
An Application Provider Binding and its route must have the same declared
scope. A Route Publication derives its owning scope and route owner from that
route rather than accepting either as endpoint-supplied metadata.

Every Route Publisher mutation identifies its exact authorization-subject
reference, declared-scope reference with identity and fingerprint, exact
Inference Route identity and fingerprint, and mutation publisher identity. An
initial publication also identifies its proposed publisher, declares expected
absence, rejects an existing current publication, and verifies the supplied
authorization subject against the route's declared scope, route owner, and
proposed publisher. Replacement and withdrawal identify the expected current
Route Publication identity and fingerprint and reject a mismatch. The publisher
verifies authorization against the expected publication's bound scope, route
owner, and publisher. Replacement separately verifies that the successor
publication retains the exact route, scope, and route owner and that the
subject is authorized for its successor publisher. Destination allowlisting is
evaluated independently and never grants mutation authority. Initial and
replacement publication also verify the proposed Provider Endpoint identity,
fingerprint, and owner. A service-backed endpoint must belong to the bound
Service Run of the route's selected Inference Service; an externally supplied
endpoint must belong to the route's selected Inference Provider.

Desired State contains exact declarations MASTIC owns after the Plan is
Eligible for its purpose. A required Plan Approval remains a separate Planning
Record and authorization prerequisite for applying or mutating those
declarations.
Proposed declarations remain part of the Plan before eligibility and are not
Desired State. Policy Assessment considers a Plan Approval only when required
and must produce an Eligible disposition before Desired State is applied.
Observed State is time-bound Evidence and never grants mutation authority. A
resource identity, lifecycle identity, process identity, route identity, and
endpoint identity never substitute for one another.

Every selected lifecycle unit is an exact Plan Target. Its current Target
Lifecycle State is `absent`, `present`, `transitioning`, or `active`.
The `transitioning` state records source state, destination state, and operation
identity. Installed, started, stopped, removed, rolled back, rejected, and
failed are events or operation results rather than lifecycle states.

Operational Condition is `functional`, `degraded`, or `nonfunctional` only
when an applicable current observation establishes that result. A missing,
stale, failed, or unauthorized observation leaves the affected axis without a
current value and records a machine-readable issue. Failure to observe one axis
does not erase an applicable current observation on the other, and observation
failure never implies `absent` or `nonfunctional`. An axis that does not apply
to a target is explicitly `not_applicable` rather than unobserved. Performance
affects Operational Condition only when the exact target contract declares that
threshold as functional behavior.

Plan Operational Assessment preserves every target identity and groups targets
by Completion, lifecycle state, and condition. It separately groups lifecycle
and condition axes under `not_observed` or `not_applicable`. It has no scalar
status, severity order, or worst-state reduction. Any headline or execution
gate is a separately named policy projection.

Profiles are always qualified. Workload Profile, Validation Profile, and
Application Profile are distinct domain concepts; capacity, sampling,
generation, and performance profiles remain explicitly qualified contracts.

Adapters expose capability boundaries rather than product policy. Desired-state
storage, engine supply, model supply, service lifecycle, process control, route
publication, protocol adaptation, credential resolution, provider access,
external-application lifecycle, application configuration, native canaries, and
evidence/profile lookup remain separate ports. Application policy composes
those ports and owns
candidate selection, Plan Disposition, Approval, and reconciliation decisions.

The exact version-2 records and compatibility rules are defined in
[`assessment-schema-v2.md`](../reference/assessment-schema-v2.md). The entity
and port responsibilities are defined in
[`domain-and-adapter-contracts.md`](../reference/domain-and-adapter-contracts.md).

## Consequences

The current duplicated runtime, model, run, route, provider, profile, and
evidence types are provisional implementation shapes, not parallel domain
entities. Schema version 2 migrates them toward one canonical identity graph.
Schema version 1 remains a lossy compatibility projection and cannot authorize
new work.

This decision does not select a gateway, supervisor, daemon topology, credential
scheme, protocol transformation policy, or legacy-retirement schedule. Those
remain owned by their downstream decisions.
