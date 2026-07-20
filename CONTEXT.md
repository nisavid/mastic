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

**Bootstrap Plan**:
A Plan limited to installing MASTIC and dependencies shared by every
Blueprint.
_Avoid_: setup plan, Stack Plan

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
time. It makes no compatibility or validation claim. An exact Release Intent
bypasses current-release lookup, verifies that exact release through its
selected owner and channel, and makes no Currency Claim.
_Avoid_: desired version, permanent pin, validation fixture

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
completion Evidence for its exact fingerprint, expressed as Partial or
Complete independently of lifecycle state and operational condition. Reused
and newly produced Evidence count equally; Failed or Blocked attempts do not.
Per-target Completion is derived from that target's required steps, and Plan
Completion requires every target to be Complete.
_Avoid_: lifecycle state, operational condition, success

**Target Lifecycle State**:
The observed installation or activation state of an exact target, independent
of Completion and Operational Condition. Its exact vocabulary is defined by
the target lifecycle contract.
_Avoid_: operational condition, Plan Disposition, execution-step state

**Operational Condition**:
The observed functional state of an exact target whose lifecycle makes that
observation meaningful, independent of Completion, Claim Qualification,
Support Position, Permission Position, and Plan Disposition.
_Avoid_: lifecycle state, readiness, support, actionability

**Public Validation Registry**:
A shared source of Evidence and Claim assessments derived from public,
reproducible artifacts for evaluating Blueprints.
_Avoid_: private telemetry store, user-content database
