# Domain and adapter contracts

This reference defines the identities and responsibility seams that MASTIC
uses when planning, observing, and reconciling an inference stack. It defines
capabilities and ownership, not implementation class names or process topology.

## Identity graph

| Identity | Owns | Does not mean |
| --- | --- | --- |
| Blueprint | Portable intent and constraints | Exact host work |
| Plan | Exact purpose-bound work, dependencies, recovery, and fingerprints | Desired or Observed State |
| Plan Target | Exact subject selected by a Plan | Plan step or generic component |
| Inference Engine | Stable engine family and capability contract | Installed executable |
| Runtime Installation | Exact executable environment of an engine | Service or process |
| Model Revision | Immutable upstream model content | Host materialization |
| Model Installation | Host-accessible materialization of a revision | Alias or shared-cache ownership |
| Model Alias | Stable desired name selecting one installation | Model identity |
| Inference Service | Stable desired runtime, model, and workload binding | Route, running process, or endpoint |
| Service Run | One concrete activation of a service | Desired service declaration |
| Managed Process | One OS process MASTIC may control | Service Run or resource identity |
| External Application | Stable externally maintained product identity | Installation or configuration |
| External Application Installation | One owner-controlled lifecycle unit | Product-wide version state |
| Application Configuration Target | One independently selected configuration scope | Executable or account |
| Application Provider Binding | Application-native provider entry | Inference Provider or Installation Owner |
| Client Protocol | Exact application-visible behavioral contract | HTTP transport or compatibility slogan |
| Inference Provider | Logical capability and credential boundary | Endpoint or application provider ID |
| Provider Endpoint | Exact observed request-serving endpoint | Stable route identity |
| Inference Route | Stable desired protocol, workload, and destination selection | URL, port, endpoint, or service identity |
| Route Publication | Current observed route-to-endpoint association | Desired route |

## State ownership

Desired State contains the exact host-local declarations that MASTIC owns after
the Plan is Eligible for its purpose. A required Plan Approval is a separate
Planning Record and authorization prerequisite for applying or mutating those
declarations. A declaration may reference an externally owned installation,
endpoint, cache, or application setting only with its owner and authorized
convergence boundary recorded. Referencing it does not transfer ownership.

Observed State contains time-bound Evidence about exact subjects. An
observation can qualify a Claim but cannot itself authorize mutation. Stored
observations retain subject fingerprints, acquisition time, provenance, and
source identities so a new assessment can determine applicability.

The identity edges and cardinalities are:

- one External Application has zero or more Installations and Configuration
  Targets; their many-to-many consumption links are explicit;
- one Inference Provider has zero or more observed Provider Endpoints;
- one Inference Service binds one Runtime Installation, Model Alias, and
  Workload Profile and has zero or more successive Service Runs;
- one Service Run has zero or more Managed Processes, each observation binding
  a PID-reuse-safe Process Identity, and zero or more observed Provider
  Endpoints;
- one Application Provider Binding binds one Client Protocol and Inference
  Route, one Application Configuration Target, one opaque credential-reference
  identity, and its declared scope;
- one Inference Route binds one Client Protocol and Workload Profile and selects
  one Inference Service or Inference Provider, has one immutable route-owner
  identity and declared scope, and has zero or more successive Route
  Publications; and
- each Route Publication binds that route's exact identity and fingerprint,
  owning declared scope, route-owner identity, and publisher identity directly
  to a Provider Endpoint owned by a Service Run of its selected service or by
  its selected provider; a service-backed publication also retains the owning
  Service Run identity.

An Inference Service never references its routes. The Client Protocol on an
Application Provider Binding must equal its route's protocol. A route selecting
an Inference Service must use the same Workload Profile fingerprint as the
service. The binding's declared scope must equal the route's declared scope.
These equality checks are identity invariants, not adapter inference.

## Capability ports

| Port | Capability | Excludes |
| --- | --- | --- |
| Desired State Repository | Load, version, diff, and compare-and-swap exact MASTIC-owned declarations | Physical reconciliation and observations |
| Planning Record Repository | Append immutable Plans, Approvals, and Plan Assessments and compare-and-append current Plan pointers | Deriving policy, executing Plans, and operational observation |
| Operational Record Repository | Append operations, Step Evidence, Evidence, observations, and recovery records | Desired State, Plan selection, Approval, and policy derivation |
| Policy Registry | Resolve the independently authenticated Policy Selection and complete canonical policy for an exact declared scope and Plan Purpose | Policy evaluation, caller-selected policy, and Approval issuance |
| Plan Generator Registry | Resolve the authenticated versioned Plan generator selected for an exact Blueprint, declared scope, and Plan Purpose | Candidate generation, policy selection, and candidate ranking |
| Inference Engine Catalogue | Resolve engine definitions and declared capabilities | Installation or selection policy |
| Runtime Supply | Discover, install, adopt, verify, and prepare an exact Runtime Installation | Service lifecycle, process control, and routing |
| Model Supply | Discover, materialize, verify, and release exact Model Installations and shared-cache references | Alias and service selection policy |
| Service Lifecycle | Compare an Inference Service with Service Run observations and request start, drain, stop, or recovery | Process, route, and engine mechanisms |
| Managed Process Control | Spawn, attach, identify, signal, wait for, and probe an exact Managed Process | Desired service and restart policy |
| Route Registry | Store desired Inference Routes separately from Route Publications | Process launch and provider policy |
| Route Publisher | Enforce supplied scope-, route-, publisher-, and authorization-subject bindings while publishing, replacing, withdrawing, observing, and resolving exact Route Publications with expected-current compare-and-swap; evaluate destination allowlisting independently | Candidate selection, lifecycle ownership, authorization policy, credential resolution, and authentication mechanism |
| Client Protocol Adapter | Declare and perform bounded protocol transformations | Route selection, application configuration, and credential ownership |
| Credential Resolver | Authorize an exact Application Provider Binding credential reference and return an opaque invocation capability bound to its scope, Route Publication, and Provider Endpoint without exposing the secret value | Credential policy, resolver mechanism, secret storage or forwarding, and provider invocation |
| Provider Adapter | Declare and probe provider capabilities and invoke an exact Provider Endpoint through an exact Route Publication for an exact Application Provider Binding using a resolved opaque invocation capability | Application configuration, route policy, engine lifecycle, credential policy, secret storage, and credential resolution |
| Release Authority | Resolve an Installation Owner and Release Channel to exact time-bound release Evidence | Installation mutation and compatibility policy |
| External Application Installation Lifecycle | Discover every installation and active invocation and perform only owner-authorized lifecycle actions | Application configuration and release selection policy |
| Application Configuration | Observe, preview, apply, adopt, relinquish, and restore one complete Managed Configuration Closure | External-application installation lifecycle |
| Native Canary | Invoke a bounded application-native behavior and return content-free Evidence | Plan Disposition and application credentials |
| Typed Profile Registry | Resolve a profile by kind, identity, version, and subject scope | Candidate selection and policy reduction |
| Operational Contract Registry | Resolve exact functional behaviors, observations, and thresholds by identity and target scope | Claim policy, observations, and condition reduction |

Application policy composes these capabilities. It owns Blueprint evaluation,
candidate selection, Plan construction, Claim assessment, Plan Disposition,
Approval, Plan Assessment construction, and reconciliation sequencing. The
Planning Record Repository stores those outputs without deriving them. The
Operational Record Repository stores operational inputs and results without
promoting them to policy. Other infrastructure adapters perform I/O and return
exact observations or bounded mutation results; they do not infer policy from
operational success.

Every Route Publisher mutation carries an exact authorization-subject
reference; declared-scope identity and fingerprint; Inference Route identity
and fingerprint; mutation publisher identity; and expected current Route
Publication identity and fingerprint, or expected absence for an initial
publication. Initial publication also identifies its proposed publisher;
replacement separately identifies its successor publisher. The publisher
rejects a mismatch or an initial publication when a current publication exists.
For initial publication, it verifies the supplied authorization subject against
the route's declared scope, route owner, and proposed publisher. For replacement
or withdrawal, it verifies authorization against the expected publication's
bound scope, route owner, and publisher. Replacement separately verifies that
the successor publication retains the exact route, scope, and route owner and
that the subject is authorized for its successor publisher. Destination
allowlisting is a separate check and never grants mutation authority.

Initial and replacement publication also verify the proposed Provider Endpoint
identity, fingerprint, and owner. A service-backed endpoint must belong to the
bound Service Run of the route's selected Inference Service; an externally
supplied endpoint must belong to the route's selected Inference Provider.

The Route Publisher enforces those supplied bindings and rejects mismatches. It
does not derive authorization policy, resolve credentials, or choose an
authentication mechanism.

The Credential Resolver is the only port that dereferences an opaque credential
reference. It validates the exact Application Provider Binding and declared
scope, then returns an opaque invocation capability bound to that binding, its
Application Configuration Target, current Route Publication identity and
fingerprint, and selected Provider Endpoint identity and fingerprint. The
capability exposes no credential value to the Provider Adapter; issue #7 owns
its concrete identity, lifetime, resolver mechanism, storage, injection, and
forwarding decisions.

Provider invocation is never endpoint-only. Each invocation supplies that
resolved capability with the exact binding, endpoint, and current Route
Publication. The Provider Adapter validates all capability bindings and that
the publication selects the binding's exact Inference Route, scope, and
endpoint. It rejects a missing, stale, or mismatched capability, publication,
binding, or scope and never resolves, infers, selects, or substitutes
credentials from ambient provider or process state.

## Lifecycle and operation boundaries

Target Lifecycle State uses one non-ordinal cross-target vocabulary:

| State | Meaning |
| --- | --- |
| `absent` | Authoritative observation established that no current target instance exists. |
| `present` | The target exists but is not the effective realization in its target-specific operational relationship and does not participate in live execution. |
| `transitioning` | A declared operation is moving the target between states; source, destination, and operation identity are required. |
| `active` | The target exists and is the effective realization in its target-specific operational relationship or participates in live execution, whether or not it functions correctly. Plan selection alone is insufficient. |

Target-specific events explain how the state changed. Installation, activation,
publication, configuration, start, stop, removal, rollback, rejection, and
failure are events or operation results, not additional lifecycle states.

Operational Condition uses:

| Condition | Meaning |
| --- | --- |
| `functional` | The target satisfies its declared operational contract. |
| `degraded` | The target provides useful function but violates part of that contract. |
| `nonfunctional` | The target was observed not to provide its declared function. |

A missing, stale, failed, or unauthorized observation is `not_observed` in the
affected projection and records a machine-readable issue; it is not a domain
state or condition. Lifecycle or condition is `not_applicable` when the target
kind, lifecycle, or contract makes that axis meaningless. Failure to observe
one axis does not erase an applicable current observation on the other.
Historical Evidence is retained but cannot populate a current projection when
its applicability has expired.

## Qualified profiles

The unqualified term “profile” is not a domain identity. Every profile contract
declares its kind, stable identity, version, subject scope, and fingerprint.

| Profile kind | Governs |
| --- | --- |
| Workload Profile | Desired context, concurrency, generation, and template behavior for a target or route |
| Validation Profile | Evidence, authority, freshness, and policy requirements for Claims and Plan Disposition |
| Application Profile | An External Application's own named configuration namespace |

Capacity, sampling, generation, and performance profiles remain separately
qualified subcontracts. A profile of one kind never satisfies a requirement for
another kind merely because their names match.

## Deferred decisions

This contract deliberately leaves these choices to their owning tickets:

- protocol fidelity, transformations, and conformance suites;
- gateway selection, routing algorithm, and local-versus-remote topology;
- credential schemes, policy, resolver mechanisms and capability identities,
  secret storage, injection, forwarding, trust roots, and privacy;
- supervisor strategy and engine-specific lifecycle behavior;
- controller reconciliation cadence and process placement;
- upgrade policy, currency enforcement, and legacy-sidecar retirement.
