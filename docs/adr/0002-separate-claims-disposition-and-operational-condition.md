---
status: accepted
---

# Separate Claims, Plan Disposition, and Operational Condition

A technically verified arrangement can remain unsupported by one publisher,
supported by MASTIC, operationally functional, and approval-required. A binding
authority may independently prohibit activation. MASTIC therefore models these
as independent facts instead of forcing them into mutually exclusive Validated,
Exploratory, Known-Risk, and Blocked Plan classes.

## Decision

Evidence records provenance, producer, observer, or issuer identities,
acquisition method, scope, subject identity bindings, and time. Those identities
do not confer Claim Authority, which explicitly identifies the entity whose
assertion or position a Claim represents. Reported, Declared, Observed, and
Derived describe how Evidence arose; they are not an ordered confidence scale.
Evidence supports a typed Claim whose domain-specific result is assessed along
two independent axes. Claim Qualification is Unknown, Provisional, or Verified.
Claim Applicability Assessment is relative to an exact candidate, Plan Purpose,
and evaluation time and retains four independent facets: scope is In Scope or
Out of Scope; time is Not Yet Effective, Effective, or Expired; lineage is
Current in Series or Superseded; and revocation is Standing or Revoked. A Claim
is Applicable only when it is In Scope, Effective, Current in Series, and
Standing, while every non-applicable reason remains available to policy and
audit. Superseded requires an explicit successor from the same authority and
Claim series whose effective window covers the evaluation time. Revoked means
the Claim or its attestation was explicitly withdrawn by its Claim Authority or
a recognized revocation authority. An artifact security revocation is a
separate safety Claim. A Claim can therefore remain historically Verified while
no longer applying to the current release, configuration, host, time, or Plan
Purpose. Claim Conflict is a relation or aggregate assessment among
independently qualified Claims, not a qualification value.

Support Position is an authority-scoped Claim with a result of Supported or
Unsupported. Positions from different authorities coexist: a publisher may
mark an arrangement Unsupported while MASTIC supports the same exact
arrangement under its own validation policy. Permission Position is a separate
authority-scoped Claim with a result of Permitted or Prohibited. A bounded,
time-stamped No Published Position Found result records the search that found
no support or permission position; it does not invent one for the authority.
Support does not prove compatibility, and observed compatibility does not imply
publisher support. The selected policy derives consequences from all applicable
Claims; no global rule maps Unsupported directly to a disposition. A Verified,
Applicable prohibition from an authority recognized as binding by the selected
policy requires a non-overridable Blocked disposition for its covered activity.
Other qualifications and conflicts remain policy inputs and may fail closed
without representing the prohibition as established.

Every exact Plan has one bounded Plan Purpose, such as validation, activation,
reconciliation, rollback, or removal. Validation replaces Exploratory as the
purpose for gathering missing Evidence under a declared safety envelope;
eligibility for validation never authorizes normal activation. Successful
validation produces Evidence for a successor activation Plan. Policy reduces
applicable Claims and non-overridable rules to one exclusive, time-bound Plan
Disposition for that exact purpose: Eligible, Approval Required, or Blocked.
Approval Required means no sufficient bound Plan Approval applies at assessment
time. Plan Approval binds the exact Plan fingerprint, purpose, applicable
Claims, policy rule, and Evidence set. Attaching one recomputes the disposition
without rewriting the immutable Plan. Relevant Evidence, Claim, purpose, or
policy drift makes the approval inapplicable and requires reassessment. Override
is reserved for a Plan Approval that explicitly supersedes an overridable
default policy rule. No Candidate remains a discovery outcome because no Plan
exists to classify. When candidates exist, each retains its own disposition;
No Eligible Candidate may summarize the selection result.

Completion is Complete only when every required step for the exact Plan Purpose
has admissible terminal completion Evidence for its exact fingerprint. Reused
and newly produced Evidence count equally; Failed or Blocked attempts do not.
Per-target Completion is derived from that target's required steps, and Plan
Completion requires every target to be Complete. Target Lifecycle State owns
the observed installation or activation state of an exact target. Operational
Condition owns the observed functional state of a target whose lifecycle makes
that observation meaningful. Claim Qualification owns the confidence warranted
for each assertion. Support Position, Permission Position, and Plan Disposition
remain separate. A Plan-level operational summary is a separately named
aggregate projection rather than the target's condition. The legacy Readiness
projection mixes progress (`Pending`), qualification (`Unverified`), and
operational results (`Ready` and `Degraded`) and must not remain the canonical
domain representation.

## Existing decision and implementation boundaries

This decision supersedes the Plan-classification and Readiness language in
ADR 0001 without changing its current-release, offline-freshness, owner,
channel, or downgrade decisions.

The open Plan-outcome refactor remains valuable for exact evidence matching,
evidence reuse, and live/durable convergence, but it stays unmerged until its
surface is narrowed to Plan Operational Assessment and its result follows this
model. It may own Completion; mechanical step-Evidence identity, shape, and
terminal-state reuse eligibility; per-target lifecycle observations; per-target
Operational Condition; operational issues; and live/durable convergence. It
may emit observations or Evidence but does not own support, permission,
currency, compatibility, integrity, Claim Qualification, Claim Conflict, Claim
Applicability Assessment, Plan Disposition, Plan Approval, performance policy,
or discovery selection. Target Lifecycle State, Operational Condition, and the
public and persisted migration from legacy `readiness` follow
[ADR 0003](0003-separate-declarations-lifecycle-and-adapter-capabilities.md) and
the [assessment schema version 2](../reference/assessment-schema-v2.md).
Execution-step `Blocked` remains distinct from Plan Disposition `Blocked` and
must not be projected as it.

Every persisted Plan identity, preview fingerprint, and approval fingerprint
includes Plan Purpose. Confirmation cannot change purpose. A discovery or
validation Plan can only produce Evidence and a distinct successor activation
Plan with its own fingerprint and disposition.

The new persisted assessment envelope and public projection use schema version
2 and a distinct kind. They never encode Claims, Plan Disposition, Approval,
Target Lifecycle State, or Operational Condition into legacy `readiness`.
Schema version 1 remains a documented, lossy read adapter. It may be retired
only in a later schema version after stored version 1 state has a supported
migration and every supported consumer accepts version 2. Discovery uses a
separate projection with `selection_outcome` equal to `candidate_selected`,
`no_candidate`, or `no_eligible_candidate`. When candidates exist, it includes
their identities and dispositions. It never includes Completion, Target
Lifecycle State, or Operational Condition.

Candidate discovery alone is not selection. As defined by the
[version-2 discovery contract](../reference/assessment-schema-v2.md#plan-discovery),
`candidate_selected` names exactly one policy-selected Eligible candidate even
when several candidates exist. Approval Required and Blocked candidates remain
visible but cannot be selected. `candidate_selected` is the only outcome with a
selected candidate. When no candidates exist, discovery emits `no_candidate`,
an empty candidate set, and a null selection. When candidates exist but none is
Eligible, discovery emits `no_eligible_candidate` and a null selection.

## Consequences

The Public Validation Registry stores reproducible Evidence and Claim
assessments rather than aggregate Plan classes. Every persisted assessment
retains its evidence policy or profile, candidate scope, Plan Purpose, and
evaluation time; applicability is recomputed for a new context. Currency,
integrity, compatibility, performance, support, Completion, and Operational
Condition can change independently and retain their provenance. Interfaces may
present a policy summary, but they must preserve the underlying Claims and may
not infer support or permission from a successful probe or erase a verified
result merely because one authority does not support it. Public projections are
versioned and legacy readers remain adapters until their migration is complete.
