# MASTIC

MASTIC describes a host-tailored inference stack as portable user intent, an
exact machine-bound plan, and independently evidenced application targets.

## Language

**Blueprint**:
Portable desired application targets, workload intents, constraints, and
preferences that MASTIC evaluates for a particular machine.
_Avoid_: Stack Plan, executable plan

**Plan**:
An exact machine-bound set of mutations, dependencies, evidence, approvals,
and recovery data produced by evaluating a Blueprint.
_Avoid_: Blueprint, Stack Plan

**Plan Target**:
An exact identity-bound subject selected by a Plan for observation, validation,
mutation, reconciliation, rollback, or removal.
_Avoid_: component, step, application target

**Bootstrap Plan**:
A Plan limited to installing MASTIC and dependencies shared by every
Blueprint.
_Avoid_: setup plan, Stack Plan

**Desired State**:
The durable exact host-local declarations MASTIC owns after the Plan is
Eligible for its purpose. A required Plan Approval is a separate authorization
prerequisite for applying or mutating this state. It may reference externally
owned resources only when their external owner and MASTIC's authorized
convergence boundary are recorded; referencing them does not transfer lifecycle
ownership.
_Avoid_: Blueprint, Plan, configuration file, Observed State

**Observed State**:
Time-bound observations of exact subjects, retained as Evidence without
granting mutation authority.
_Avoid_: Desired State, cached truth, Plan outcome

**External Application**:
An externally maintained product identity. MASTIC coordinates its installations
and configuration targets without owning the application's lifecycle.
_Avoid_: client, application binary, installation

**External Application Installation**:
One concrete installable unit of an External Application on a host, with its
own Installation Owner, Release Intent, observed state, and recovery boundary.
_Avoid_: application, client, component

**Installation Owner**:
The application-native installer, package manager, or deployment system that
controls one External Application Installation's lifecycle.
_Avoid_: MASTIC owner, executable path, inferred owner

**Installation Observation**:
Time-bound Observed State for one exact External Application Installation,
binding its MASTIC identity and fingerprint to the owner-native installation
identity, Installation Owner, effective Release Channel, platform,
architecture, installed artifact, and reachable invocations. It does not
select an owner or authorize mutation.
_Avoid_: External Application Installation, inventory row, lifecycle authority

**Release Channel**:
An Installation Owner's native release stream, selected independently for one
External Application Installation and inherited from its Blueprint when
unspecified.
_Avoid_: version range, release pin

**Release Intent**:
The portable choice to track the current release of a selected Release Channel
or explicitly select one exact release.
_Avoid_: resolved version, dependency pin

**Current Release Resolution**:
Time-bounded Plan evidence that proves the selected Release Channel's
authority mapped current Release Intent to one exact release at an observed
time, bound to the exact Installation Observation used to construct the
authority query. Direct online resolution is Observed Evidence. Offline use
requires a Signed Current Release Resolution whose envelope authenticates the
complete canonical resolution payload. Current Release Resolution makes no
compatibility or validation claim. An exact Release Intent bypasses
current-release lookup, verifies that exact release through its selected owner
and channel, and makes no Currency Claim.
_Avoid_: desired version, permanent pin, validation fixture

**Signed Current Release Resolution**:
One Current Release Resolution plus exactly one authentic signature path and
its signature. It is required for offline use and never inferred from an
unsigned online observation.
_Avoid_: Current Release Resolution, artifact signature, cached lookup

**Unattended Upgrade Policy**:
A durable subject-bound rule that may require MASTIC to construct and assess a
new exact same-owner, same-channel Current upgrade Plan. It never acts as Plan
Approval and never authorizes a downgrade, owner switch, or channel switch.
_Avoid_: auto-update setting, standing approval, upgrade Plan

**Validated Backup**:
A fresh content-addressed backup bound to one exact Installation Observation,
Current Release Resolution, and data-bearing upgrade candidate.
_Avoid_: ownership backup, rollback point, archive path

**Recovery Qualification**:
Profile-specific Evidence that one exact Validated Backup restored in isolation
and passed representative recall for one exact data-bearing upgrade candidate.
It cannot be reused after the candidate or source observation changes.
_Avoid_: backup success, application health check, rollback

**Removal Plan**:
An exact Plan naming each External Application Installation, Installation
Owner, owner-native installation identity, and Installation Observation
fingerprint authorized for removal. Ordinary product removal is not a Removal
Plan and retains every external application.
_Avoid_: uninstall list, cleanup, mastic remove

**Currency Claim**:
The time-bounded statement that an exact release was the latest release
reported by one selected Release Channel's authority at an observed time.
_Avoid_: latest version, validated release

**Attestation Issuer**:
A validation-profile-authorized signer that seals current-release observations
for offline verification when no profile-trusted upstream signature covers the
entire canonical Current Release Resolution payload.
_Avoid_: Installation Owner, release authority, closure builder

**Application Configuration Target**:
One independently selected and evidenced configuration scope for an External
Application, linked explicitly to each installation that consumes, mutates,
obtains credentials from, supplies credentials to, or probes it.
_Avoid_: client, account, global configuration

**Managed Configuration Closure**:
The smallest complete set of interdependent application settings whose
explicit values, defaults, or absence establish one selected behavior and that
MASTIC owns and restores together.
_Avoid_: touched keys, written fields, whole configuration

**Inference Engine**:
The stable software family and capability contract that can execute local
model inference.
_Avoid_: Runtime Installation, process, model, service

**Runtime Installation**:
One exact executable environment of an Inference Engine on a host, identified
by version, provenance, launcher, and observed capabilities.
_Avoid_: engine, Service Run, virtual environment path

**Model Revision**:
One immutable upstream model-content identity.
_Avoid_: model name, Model Installation, Model Alias

**Model Installation**:
One host-accessible materialization of an exact Model Revision with recorded
provenance and ownership boundaries.
_Avoid_: model cache, Model Revision, Model Alias

**Model Alias**:
A stable MASTIC-owned name that selects one Model Installation without changing
the installation's identity.
_Avoid_: model slug, repository, Inference Route

**Inference Service**:
A stable desired serving declaration that binds a Runtime Installation, Model
Alias, and Workload Profile.
_Avoid_: Service Run, process, endpoint

**Service Run**:
One concrete activation instance of an Inference Service with its own identity
and lifecycle observations.
_Avoid_: Inference Service, Managed Process, operation

**Managed Process**:
An operating-system process whose lifecycle MASTIC is explicitly authorized to
control as a physical realization of a Service Run or another lifecycle unit.
_Avoid_: Inference Service, Service Run, arbitrary process

**Process Identity**:
The operating-system identity used to distinguish one process instance from PID
reuse. It is never a resource, service, or run identity.
_Avoid_: PID, Service Run identity, operation identity

**Client Protocol**:
One exact application-facing request, response, tool, streaming, continuation,
and error contract.
_Avoid_: HTTP, SSE, OpenAI-compatible, provider

**Inference Provider**:
A logical inference capability and credential boundary, local or remote, that
may expose one or more Provider Endpoints.
_Avoid_: Application Provider Binding, Installation Owner, endpoint

**Provider Endpoint**:
One exact observed request-serving endpoint, owned by a Service Run when
locally served or by an Inference Provider when externally supplied.
_Avoid_: Inference Provider, Inference Route, application base URL

**Application Provider Binding**:
An External Application's provider entry that binds one Application
Configuration Target to a Client Protocol, Inference Route, and opaque
credential-reference identity within the binding's declared scope.
_Avoid_: Inference Provider, provider string, Installation Owner

**Inference Route**:
A stable desired selection identity, owned by one exact route-owner identity
within one declared scope, that binds a Client Protocol and Workload Profile to
one Inference Service or Inference Provider. An Application Provider Binding
and its route have the same declared scope. The current Route Publication
resolves directly to an exact Provider Endpoint and also retains the owning
Service Run identity when the route selects an Inference Service.
_Avoid_: URL, port, Inference Service, Route Publication

**Route Publication**:
A time-bound observed association between one Inference Route and a currently
usable exact Provider Endpoint, bound to the route's owning declared scope and
route-owner identity and to the exact publisher identity that materialized it.
A service-backed publication also binds the endpoint's owning Service Run.
_Avoid_: Inference Route, desired route, Gateway Route

**Workload Profile**:
A named desired request behavior covering the applicable context, concurrency,
generation, and template choices for a target or route.
_Avoid_: Profile, Validation Profile, Application Profile

**Validation Profile**:
A versioned set of evidence, authority, freshness, and policy requirements used
to assess Claims and Plan Disposition.
_Avoid_: Profile, Workload Profile, performance result

**Operational Contract**:
A versioned declaration of the functional behavior, observations, and any
thresholds used to assess one exact target's Operational Condition.
_Avoid_: Validation Profile, Workload Profile, health result

**Application Profile**:
An External Application's own named configuration namespace when that namespace
is part of an Application Configuration Target's identity.
_Avoid_: Profile, Workload Profile, MASTIC profile

**Evidence**:
A source-attributed record used to assess a Claim, including its producer,
observer, or issuer identities, acquisition method, scope, subject identity
bindings, and observation time.
_Avoid_: claim, policy decision, operational result

**Evidence Provenance**:
How Evidence arose and who produced, observed, or issued it, expressed as
Reported, Declared, Observed, or Derived without ranking those methods by
strength or conferring Claim Authority.
_Avoid_: evidence state, confidence, validation level

**Claim Authority**:
The explicitly bound entity whose assertion or position a Claim represents.
Evidence custody, observation, or transmission does not confer this authority.
_Avoid_: evidence source, observer, policy authority

**Claim**:
A scope-bound assertion about one exact subject, predicate, and result that is
assessed from Evidence.
_Avoid_: evidence, Plan status, guarantee

**Claim Qualification**:
The confidence warranted for a Claim by its Evidence under the applicable
evidence policy, expressed as Unknown, Provisional, or Verified.
_Avoid_: Evidence Provenance, Claim Conflict, applicability

**Claim Conflict**:
A relation or aggregate assessment showing that two or more independently
qualified Claims cannot all hold for the same subject, predicate, scope, and
effective time.
_Avoid_: Claim Qualification, supersession, policy disagreement

**Claim Applicability Assessment**:
The composite evaluation of a Claim for an exact candidate, Plan Purpose, and
evaluation time. It retains four independent facets: scope is In Scope or Out
of Scope; time is Not Yet Effective, Effective, or Expired; lineage is Current
in Series or Superseded; and revocation is Standing or Revoked. A Claim is
Applicable only when it is In Scope, Effective, Current in Series, and Standing;
every non-applicable reason remains available to policy and audit. Superseded
requires an explicit successor from the same authority and Claim series whose
effective window covers the evaluation time. Revoked means the Claim or its
attestation was explicitly withdrawn by its Claim Authority or a recognized
revocation authority, not that a separate safety Claim revoked an artifact.
_Avoid_: Claim Qualification, Claim Conflict, intrinsic Claim state

**Support Position**:
An authority-scoped Claim that an exact arrangement is Supported or
Unsupported. Positions from different authorities coexist and do not
substitute for compatibility Evidence.
_Avoid_: Permission Position, compatibility, Plan Disposition

**Permission Position**:
An authority-scoped Claim that a covered activity is Permitted or Prohibited.
A Verified, Applicable prohibition from an authority that the selected policy
recognizes as binding requires a non-overridable Blocked disposition for that
activity. Other qualifications and conflicts remain policy inputs and may fail
closed without establishing that the prohibition exists.
_Avoid_: Support Position, compatibility, operational condition

**No Published Position Found**:
A bounded, time-stamped search result stating that no support or permission
position was found in the searched authority sources. It is not itself the
authority's position.
_Avoid_: Unsupported, Prohibited, undocumented position

**Plan Purpose**:
The single bounded activity a Plan is intended to authorize, such as
validation, activation, reconciliation, rollback, or removal. A validation
purpose may gather Evidence only within its declared safety envelope and does
not authorize normal activation; activation requires a successor Plan.
_Avoid_: Exploratory Plan, Plan classification, Release Intent

**Policy Assessment**:
A time-bound evaluation of one immutable Plan, its applicable Claims, selected
policy, and Plan Approvals that produces Plan Disposition.
_Avoid_: Plan Operational Assessment, policy result, Plan outcome

**Plan Disposition**:
The time-bound, policy-derived actionability assessment of one exact Plan and
Plan Purpose under the applicable Claims, policy, and approvals, expressed as
Eligible, Approval Required, or Blocked. Approval Required means no sufficient
bound Plan Approval applies at assessment time; attaching one recomputes the
disposition without rewriting the Plan. Relevant Evidence, Claim, purpose, or
policy drift requires reassessment.
_Avoid_: Validated Plan, Known-Risk Plan, readiness

**Plan Approval**:
The explicit user authorization that satisfies the approval requirement for an
Approval Required Plan after review of its applicable Claims and consequences.
It binds the exact Plan fingerprint, Plan Purpose, policy rule, Evidence set,
and applicable Claims.
_Avoid_: Plan Disposition, readiness, blanket consent

**Plan Operational Assessment**:
A time-bound evaluation of Completion and Target Operational Snapshots for one
exact Plan and Plan Purpose.
_Avoid_: Policy Assessment, readiness, Plan outcome

**Plan Assessment**:
A versioned record that composes sibling Policy Assessment and Plan Operational
Assessment projections over the same Plan, Plan Purpose, Evidence set, policy,
and evaluation time without collapsing their results.
_Avoid_: Plan outcome, readiness, status

**Override**:
A Plan Approval that explicitly supersedes one overridable default policy rule
within its declared scope.
_Avoid_: Plan Approval, warning acceptance, expert mode

**No Candidate**:
A completed discovery outcome in which discovery cannot construct a candidate
Plan for the Blueprint and intended Plan Purpose. When candidates exist, each
has its own Plan Disposition; No Eligible Candidate may summarize a set in
which none is Eligible.
_Avoid_: Blocked Plan, No Eligible Candidate, No Validated Fit

**Completion**:
Whether every required step for the exact Plan Purpose has admissible terminal
completion Evidence for its exact fingerprint, expressed as `partial` or
`complete` independently of lifecycle state and operational condition. Reused
and newly produced Evidence count equally; `failed` or `blocked` attempts do
not. Per-target Completion is derived from that target's required steps, and
Plan Completion requires every target to be `complete`.
_Avoid_: lifecycle state, operational condition, success

**Target Lifecycle State**:
The current materialization and participation state of an exact Plan Target,
expressed as `absent`, `present`, `transitioning`, or `active`. The
`transitioning` state records its source state, destination state, and
operation identity. `active` means the target exists and is the current
effective realization in its target-specific
operational relationship or participates in live execution; Plan selection or
a Desired State reference alone does not make it `active`.
_Avoid_: operational condition, Plan Disposition, execution-step state

**Operational Condition**:
The current observed functional condition of an exact Plan Target whose
lifecycle makes observation meaningful, expressed as `functional`, `degraded`,
or `nonfunctional`. It is independent of Completion, lifecycle, Claim
Qualification, Support Position, Permission Position, and Plan Disposition.
`not_observed` and `not_applicable` are observation-axis states, not Operational
Condition values.
_Avoid_: lifecycle state, readiness, support, actionability

**Target Operational Snapshot**:
A time-bound, identity-bound projection containing one Plan Target's lifecycle
observation, Operational Condition when applicable and observed, Evidence, and
issues.
_Avoid_: target status, readiness, Plan Disposition

**Operational Summary**:
Independent identity-preserving groupings of selected Plan Targets: one by
Completion, one by Target Lifecycle State, and one by Operational Condition.
The lifecycle and condition axes separately retain unobserved and
not-applicable observation states. Missing, stale, failed, or unauthorized
applicable Evidence maps to the affected `lifecycle_not_observed` or
`condition_not_observed` bucket and cannot populate a current observation. An
axis made meaningless by the target kind, lifecycle, or contract maps to
`lifecycle_not_applicable` or `condition_not_applicable`. These are distinct
summary buckets rather than lifecycle or condition values. The axes never form
composite buckets, a scalar status, or a severity order.
_Avoid_: overall readiness, worst state, Plan outcome

**Public Validation Registry**:
A shared source of Evidence and Claim assessments derived from public,
reproducible artifacts for evaluating Blueprints.
_Avoid_: private telemetry store, user-content database
