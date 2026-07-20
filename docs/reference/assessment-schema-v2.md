# Assessment schema version 2

This reference defines the canonical version-2 Plan, Evidence, Claim,
assessment, discovery, and compatibility records. Version 2 replaces the
schema-version-1 `completion` and `readiness` reduction as the domain source of
truth. It does not change the SQLite storage-engine version.

## Shared conventions

The JSON examples use uppercase digest suffixes as metasyntactic placeholders;
persisted values must satisfy the lowercase digest rule below.

- Every top-level version-2 persisted or public record contains
  `schema_version: 2` and one exact `kind` from this reference. Embedded objects
  use the exact enclosing contract and do not repeat those fields unless stated
  otherwise. A documented version-1 outbound compatibility response is not a
  version-2 record and is exempt from this invariant.
- Identity and fingerprint fields use lowercase `sha256:<64 hex>` values over
  the UTF-8 bytes produced by the
  [RFC 8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785).
  Inputs satisfy I-JSON; object properties use JCS UTF-16 code-unit ordering,
  strings and IEEE 754 numbers use JCS serialization, and the output contains
  no insignificant whitespace. Writers reject duplicate object names,
  non-finite or non-I-JSON numbers, and lone surrogate code points before
  hashing.
- Timestamps are valid RFC 3339 date-times in the single canonical UTC subset
  `YYYY-MM-DDTHH:MM:SS[.fraction]Z`. Years are `0001` through `9999`; calendar
  dates are valid; hours are `00` through `23`; and minutes and seconds are
  `00` through `59`. The date and whole-second fields have the displayed fixed
  widths. Fractional seconds are omitted when zero; otherwise they use one to
  nine digits with no trailing zero. Numeric offsets, lowercase separators,
  leap-second `60`, excess precision, and redundant fractional zeros are
  rejected before hashing. Every participating timestamp is already in this
  form when JCS serialization computes an identity.
- Identifier arrays are sorted, contain no duplicates, and retain identities;
  counts never replace them.
- Subject references contain `kind`, `id`, and `fingerprint`.
- Persisted Evidence and issues are content-free. They may contain stable codes,
  identities, timestamps, sizes, metrics, digests, and human-facing diagnostic
  messages or action labels derived from those values, but never raw
  observations, prompts, responses, tool arguments, credentials, authorization
  headers, or secret values.
- `null` is used only where this reference explicitly permits it. Missing and
  unknown are never inferred from an absent required field.

This JCS edge-case input is a normative canonicalization test vector:

```json
{"€":1.0,"a":-0.0,"é":"é"}
```

Its canonical UTF-8 form is `{"a":0,"é":"é","€":1}` and its SHA-256 digest
is `sha256:052a8c41d49d769806825e6ab1ccd73e19574290b227acd97c6438db2f6e39e0`.
Equivalent escaped spellings produce the same parsed strings; implementations
do not perform Unicode normalization beyond JCS serialization.

Set-like object arrays are sorted before JCS serialization using these primary
keys. Key components are compared lexicographically; ties use the complete
element's canonical JCS UTF-8 bytes as a final tiebreaker. Exact duplicate
elements are invalid.

| Array | Primary sort keys |
| --- | --- |
| Plan `targets`; operational-assessment `targets` | `target_id`, `plan_target_fingerprint` |
| `required_steps`; `step_satisfactions` | `step_id`, `step_fingerprint` |
| `claims`; `claim_qualifications`; `claim_applicability`; support and permission positions | `claim_identity` |
| `claim_conflicts` | `conflict_identity` |
| position `searches` | `search_identity` |
| policy `rule_evaluations` | `rule_id` |
| `approvals`; approval `evaluations` | `identity`; `approval_identity` |
| `issues` | `issue_identity` |
| discovery `candidates` | `plan_identity` |

The identifier arrays, Evidence closures, and summary buckets covered by the
general sorted-array rule contain scalar identities rather than objects. Array
order is semantic only when an individual field contract explicitly says so;
semantic arrays are serialized in their declared order.

For every immutable object with a `*_identity` field, that identity is the
SHA-256 digest of the canonical object after removing only its own identity
field and any storage-envelope `id` and `version` fields. Every other
`*_identity` field in the object, including a related `series_identity` or
`scope_identity`, remains in the digest input. Timestamps therefore participate
unless a field contract explicitly says otherwise.
`plan_target_fingerprint` is the digest of exactly the two-key wrapper object
`{"purpose": <Plan purpose>, "target": <complete target object>}`, after
removing only `plan_target_fingerprint` from the embedded target.
`step_fingerprint` is the digest of exactly the two-key wrapper object
`{"purpose": <Plan purpose>, "step": <complete step object>}`, after removing
only `step_fingerprint` from the embedded step. These wrappers are not
flattened into their embedded objects or represented as arrays.
`policy_fingerprint` is the digest of the complete versioned policy object
after removing only `fingerprint`.
`evidence_set_fingerprint` is the digest of the
canonical sorted array of exact Evidence identities, not a record locator.
Cross-record references resolve by exact `(kind, identity)`.

This canonical Claim preimage is a normative identity test vector:

```json
{"authority_id":"authority:test","effective_at":"2026-07-20T00:00:00Z","predicate":"test.result","result":true,"scope":{"id":"test-host","kind":"host"},"series_identity":"sha256:0000000000000000000000000000000000000000000000000000000000000000","subject":{"fingerprint":"sha256:1111111111111111111111111111111111111111111111111111111111111111","id":"subject:test","kind":"test_subject"},"valid_until":"2026-07-21T00:00:00Z"}
```

Its `claim_identity` is
`sha256:9c1ac064c82f29c1d36f02595ec26361487e7348fcd362c08beddec12756de41`.

These canonical Plan Target and step preimages are normative identity test
vectors:

```json
{"purpose":"activation","target":{"expected_lifecycle_state":"active","lifecycle_applicability":"applicable","operational_contract_ref":{"fingerprint":"sha256:2222222222222222222222222222222222222222222222222222222222222222","id":"codex-native-canary","kind":"operational_contract"},"subject_fingerprint":"sha256:1111111111111111111111111111111111111111111111111111111111111111","target_id":"application-installation:codex:vite","target_kind":"external_application_installation"}}
```

Its `plan_target_fingerprint` is
`sha256:e603696cc457e9631d2a675d43ded22fa9bf0a08651691698cca583417082a80`.

```json
{"purpose":"activation","step":{"depends_on":[],"plan_target_fingerprint":"sha256:e603696cc457e9631d2a675d43ded22fa9bf0a08651691698cca583417082a80","reuse_rule_id":"exact-purpose-step-material","skip_rule_id":null,"step_id":"application.codex.upgrade","target_id":"application-installation:codex:vite"}}
```

Its `step_fingerprint` is
`sha256:eacda0b905caa28f2fdc49fe1e84a3018d8f69bc090c63e03ccd1cf4216a52e3`.

## Persisted record kinds

Version 2 writes these records separately:

| Kind | Mutability | Repository port | Purpose |
| --- | --- | --- | --- |
| `mastic.plan` | Immutable | Planning Record Repository | Exact purpose-bound Plan identity and assessment-facing step graph |
| `mastic.plan-step-evidence` | Immutable | Operational Record Repository | One exact step attempt or reusable terminal result |
| `mastic.evidence` | Immutable | Operational Record Repository | One content-free reusable Evidence record |
| `mastic.plan-approval` | Immutable | Planning Record Repository | One purpose-, policy-, Claim-, and Evidence-bound authorization |
| `mastic.plan-assessment` | Immutable | Planning Record Repository | Sibling policy and operational assessments over one evaluation context |
| `mastic.current-plan-pointer` | Replaceable pointer | Planning Record Repository | Current Plan and assessment for one declared scope |

Schema-version-1 `setup_plan` and `setup_evidence` records retain their original
meaning. A version-2 writer never updates or reinterprets them.

The current snapshot repository stores a persistence envelope separately from
the canonical record. Adapters use these exact keys:

| Record kind | Snapshot `id` | Snapshot `version` |
| --- | --- | --- |
| `mastic.plan` | `plan_identity` | `plan_identity` |
| `mastic.plan-step-evidence` | `evidence_identity` | `evidence_identity` |
| `mastic.evidence` | `evidence_identity` | `evidence_identity` |
| `mastic.plan-approval` | `approval_identity` | `approval_identity` |
| `mastic.plan-assessment` | `assessment_identity` | `assessment_identity` |
| `mastic.current-plan-pointer` | `scope_identity` | `<pointer_version>:<pointer_identity>` |

The envelope does not participate in the canonical record identity. Immutable
records reject a different payload at an existing `(kind, id, version)`.
For a current Plan pointer, split the envelope version at its first colon. The
prefix is the positive canonical base-10 `pointer_version` with no leading
zero; the
remainder is the complete `pointer_identity`, including its `sha256:` prefix.
The decoded values must equal the record payload fields before any pointer CAS.

## Plan record

`mastic.plan` requires:

```json
{
  "schema_version": 2,
  "kind": "mastic.plan",
  "plan_identity": "sha256:PLAN",
  "blueprint_identity": "sha256:BLUEPRINT",
  "scope": {
    "kind": "declared_scope",
    "id": "user:501/default",
    "fingerprint": "sha256:SCOPE"
  },
  "purpose": "activation",
  "created_at": "2026-07-20T00:00:00Z",
  "targets": [
    {
      "target_id": "application-installation:codex:vite",
      "target_kind": "external_application_installation",
      "subject_fingerprint": "sha256:TARGET",
      "plan_target_fingerprint": "sha256:PLAN_TARGET",
      "lifecycle_applicability": "applicable",
      "expected_lifecycle_state": "active",
      "operational_contract_ref": {
        "kind": "operational_contract",
        "id": "codex-native-canary",
        "fingerprint": "sha256:OPERATIONAL_CONTRACT"
      }
    }
  ],
  "required_steps": [
    {
      "step_id": "application.codex.upgrade",
      "step_fingerprint": "sha256:STEP",
      "target_id": "application-installation:codex:vite",
      "plan_target_fingerprint": "sha256:PLAN_TARGET",
      "depends_on": [],
      "skip_rule_id": null,
      "reuse_rule_id": "exact-purpose-step-material"
    }
  ]
}
```

`purpose` is exactly one of `validation`, `activation`, `reconciliation`,
`rollback`, or `removal`. It participates in `plan_identity`, every preview
fingerprint, every step fingerprint, and every approval fingerprint.
Confirmation cannot change it. Validation produces Evidence only; activation
requires a distinct successor Plan.

Each target declares whether lifecycle projection is `applicable` or
`not_applicable`. An applicable target requires one purpose-bound
`expected_lifecycle_state`; a nonapplicable target requires
`expected_lifecycle_state: null`. `operational_contract_ref` identifies the
exact contract used to assess function or is `null` when condition is not
structurally applicable. A non-null contract defines condition only when the
target's current lifecycle makes functional observation meaningful. Expected
lifecycle is declared per purpose-bound Plan Target; it is never inferred by
hard-coding activation, validation, rollback, removal, or any other purpose. It
is one of the stable states `absent`, `present`, or `active`; a completed Plan
never expects `transitioning`.

Within a Plan, `target_id` and `plan_target_fingerprint` values are each unique.
Within `required_steps`, `step_id` and `step_fingerprint` values are also each
unique. A duplicate makes the Plan invalid and can never participate in
Completion. Every required step references exactly one declared target and
repeats its exact `plan_target_fingerprint`. Its
`(target_id, plan_target_fingerprint)` tuple must equal exactly one declared
target tuple; combining the ID and fingerprint of different targets is invalid.

## Current Plan pointer

`mastic.current-plan-pointer` requires:

```json
{
  "schema_version": 2,
  "kind": "mastic.current-plan-pointer",
  "pointer_identity": "sha256:POINTER",
  "scope": {
    "kind": "declared_scope",
    "id": "user:501/default",
    "fingerprint": "sha256:SCOPE"
  },
  "scope_identity": "sha256:SCOPE",
  "pointer_version": 3,
  "expected_predecessor_identity": "sha256:PREVIOUS_POINTER",
  "plan_identity": "sha256:PLAN",
  "plan_purpose": "activation",
  "assessment_identity": "sha256:ASSESSMENT",
  "updated_at": "2026-07-20T00:02:00Z"
}
```

The first pointer uses `pointer_version: 1` and
`expected_predecessor_identity: null`. Its creation is an atomic compare-and-
append against expected absence for the scope: exactly one of any concurrent
first writers may succeed, and every loser is rejected after the winner creates
the current pointer. Replacement is an atomic compare-and-append: after
decoding and validating the latest envelope as specified above,
its numeric `pointer_version` must be one less than the new payload's version
and its decoded `pointer_identity` must equal
`expected_predecessor_identity`. A mismatch rejects the write. The referenced
Plan and assessment must exist, share scope and purpose, and match each other's
Plan identity. A pointer is a current-selection mechanism only; it never makes
its immutable Plan or assessment mutable. An absent pointer is represented by
no `mastic.current-plan-pointer` record for the scope; no sentinel identity such
as `active` is used.

`pointer_identity` is the SHA-256 digest of the complete canonical pointer
object after removing only `pointer_identity`; storage-envelope `id` and
`version` fields are never part of the object or its digest. Every other
identity field, including `scope_identity`, remains. Scope, pointer version,
expected predecessor, Plan identity and purpose, assessment identity, and update
time all participate. The snapshot envelope stores this replaceable
record under `scope_identity` with version
`<pointer_version>:<pointer_identity>`. A writer computes the new identity
before compare-and-append, then validates both the predecessor identity and
one-less predecessor version against the latest stored pointer.

## Step Evidence record

`mastic.plan-step-evidence` requires:

```json
{
  "schema_version": 2,
  "kind": "mastic.plan-step-evidence",
  "evidence_identity": "sha256:STEP_EVIDENCE",
  "plan_identity": "sha256:PLAN",
  "plan_purpose": "activation",
  "step_id": "application.codex.upgrade",
  "step_fingerprint": "sha256:STEP",
  "target_id": "application-installation:codex:vite",
  "plan_target_fingerprint": "sha256:PLAN_TARGET",
  "result": "complete",
  "skip_rule_id": null,
  "authorization_evidence_ids": [],
  "observed_at": "2026-07-20T00:01:00Z",
  "material_digest": "sha256:MATERIAL",
  "code": "owner_upgrade_verified"
}
```

`result` is one of `complete`, `skipped`, `failed`, or `blocked`. `complete`
requires `skip_rule_id: null` and an empty authorization list. `skipped` can
satisfy Completion only when the Plan step names the same non-null
`skip_rule_id` and `authorization_evidence_ids` identifies nonempty Evidence
that the rule accepted. Every such authorization Evidence record's
`subject_bindings` contains the exact `plan_identity`, `plan_purpose`,
`step_fingerprint`, `plan_target_fingerprint`, `skip_rule_id`,
`policy_fingerprint`, and `evaluated_at` for this decision. All bindings must
match the Step Evidence and current evaluation; omission or mismatch rejects
the skip, and authorization Evidence is never reusable across another Plan,
purpose, step, target, rule, policy, or evaluation time. `failed` and
execution-step `blocked` never imply Plan Disposition and never count as
terminal completion Evidence.

`authorization_evidence_ids` is sorted and contains no duplicates. When a
`skipped` Step Evidence record satisfies Completion, every one of these
identities resolves as `(mastic.evidence, evidence_identity)` and is included
transitively in the Plan Assessment's top-level `evidence_ids` closure and
`evidence_set_fingerprint`.

## Evidence record and Claim values

Each immutable `mastic.evidence` record contains `schema_version: 2`, its exact
`kind`, and:

| Field | Contract |
| --- | --- |
| `evidence_identity` | Immutable Evidence fingerprint |
| `provenance` | `reported`, `declared`, `observed`, or `derived` |
| `source_id` | Exact source identity |
| `producer_id` | Producer identity or `null` |
| `observer_id` | Observer identity or `null` |
| `issuer_id` | Issuer identity or `null` |
| `acquisition_method` | Stable method code |
| `scope` | Exact JSON scope object |
| `subject_bindings` | Evidence-subtype bindings for exact identities, fingerprints, purpose, rule, policy, and evaluation time; never secret values |
| `observed_at` | Acquisition or observation time |
| `valid_until` | Evidence expiry or `null` |
| `material_digest` | Digest of the content-free retained material |

Source, producer, observer, and issuer identities never confer Claim Authority.
Pre-assessment observations are retained as these records. A Plan Assessment
references them through its exact `evidence_ids`; Evidence is independently
dereferenced by `(mastic.evidence, evidence_identity)` and may be reused without
copying or changing identity.

The `claims` array contains immutable assertion value objects embedded in the
assessment:

| Field | Contract |
| --- | --- |
| `claim_identity` | Immutable Claim fingerprint |
| `series_identity` | Same-authority subject/predicate/scope series |
| `subject` | Exact subject reference |
| `predicate` | Stable domain predicate |
| `result` | Predicate-specific JSON value |
| `authority_id` | Explicit Claim Authority |
| `scope` | Exact immutable Claim scope |
| `effective_at` | Claim effective time |
| `valid_until` | Claim validity endpoint or `null` |

Evidence relations and Qualification are not stored inside the immutable Claim.
The `claim_qualifications` array evaluates the assertion under the current
Validation Profile and Evidence context:

Within one assessment, it contains at most one record for each complete
`(claim_identity, plan_identity, plan_purpose, validation_profile, evaluated_at)`
tuple. The complete Validation Profile reference is a tuple member;
`evidence_ids` remains payload and varying that Evidence subset does not create
a distinct evaluation context. Duplicate or conflicting records for one tuple
are invalid.

| Field | Contract |
| --- | --- |
| `claim_identity` | Assessed immutable Claim |
| `plan_identity` | Exact candidate Plan context |
| `plan_purpose` | Exact purpose context |
| `validation_profile` | Exact subject reference for the Validation Profile |
| `evaluated_at` | Evaluation time |
| `evidence_ids` | Exact admissible Evidence used for Qualification |
| `value` | `unknown`, `provisional`, or `verified` |
| `reason_codes` | Sorted stable codes explaining the result |

The same immutable Claim may therefore receive a different Qualification under
a new Evidence set or Validation Profile without changing Claim identity.
Applicability is also not stored inside the Claim. The `claim_applicability`
array evaluates it for the current context:

Within one assessment, it contains at most one record for each
`(claim_identity, plan_identity, plan_purpose, evaluated_at)` tuple. Duplicate or
conflicting records for one tuple are invalid.

| Field | Contract |
| --- | --- |
| `claim_identity` | Assessed Claim |
| `plan_identity` | Exact candidate Plan identity |
| `plan_purpose` | Exact purpose |
| `evaluated_at` | Evaluation time |
| `value` | `applicable`, `not_yet_effective`, `expired`, `superseded`, `revoked`, or `out_of_scope` |
| `successor_claim_identity` | Required only for `superseded`; otherwise `null` |
| `revocation_claim_identity` | Required when a recognized revocation Claim establishes `revoked`; otherwise `null` |

`claim_conflicts` records relations among independently qualified Claims. Each
record contains `conflict_identity`, at least two `claim_ids`, and the exact
`subject`, `predicate`, `scope`, and `effective_time` over which they conflict.
`effective_time` is one RFC 3339 UTC timestamp: the assessment instant at which
the referenced Claims overlap and conflict. It is distinct from each Claim's
`effective_at`, which states when that Claim begins to apply. The complete
canonical conflict record, including `effective_time`, participates in
`conflict_identity` after removing only `conflict_identity`.
Conflict is never a Claim Qualification. Claims are immutable value objects
embedded in an assessment; cross-assessment reuse copies the exact Claim and
preserves its identity. Evidence is independently dereferenced and reused by
its `mastic.evidence` identity.

## Positions and searches

The `positions` object is a typed projection over Claims:

```json
{
  "support": [
    {
      "claim_identity": "sha256:SUPPORT_CLAIM",
      "authority_id": "authority:mastic",
      "value": "supported"
    }
  ],
  "permission": [
    {
      "claim_identity": "sha256:PERMISSION_CLAIM",
      "authority_id": "authority:organization",
      "value": "permitted"
    }
  ],
  "searches": [
    {
      "search_identity": "sha256:SEARCH",
      "authority_id": "authority:publisher",
      "position_type": "support",
      "result": "no_published_position_found",
      "searched_at": "2026-07-20T00:00:00Z",
      "evidence_ids": ["sha256:SEARCH_EVIDENCE"]
    }
  ]
}
```

Support values are `supported` or `unsupported`. Permission values are
`permitted` or `prohibited`. A search result is Evidence about a bounded search;
it is never a Support Position or Permission Position.

## Approval record

`mastic.plan-approval` requires:

```json
{
  "schema_version": 2,
  "kind": "mastic.plan-approval",
  "approval_identity": "sha256:APPROVAL",
  "approver_ref": "local-user:501",
  "plan_identity": "sha256:PLAN",
  "plan_purpose": "activation",
  "policy_fingerprint": "sha256:POLICY",
  "evidence_set_fingerprint": "sha256:EVIDENCE_SET",
  "applicable_claim_ids": ["sha256:CLAIM"],
  "rule_ids": ["publisher-support-exception"],
  "override_rule_ids": [],
  "granted_at": "2026-07-20T00:00:30Z"
}
```

`override_rule_ids` is empty for an ordinary Plan Approval. An Override names
every overridable default rule it supersedes. Evidence, Claim, purpose, Plan,
or policy drift makes the approval inapplicable without mutating it.

## Plan Assessment record

`mastic.plan-assessment` requires this top-level shape:

```json
{
  "schema_version": 2,
  "kind": "mastic.plan-assessment",
  "assessment_identity": "sha256:ASSESSMENT",
  "plan": {
    "plan_identity": "sha256:PLAN",
    "blueprint_identity": "sha256:BLUEPRINT",
    "scope": {
      "kind": "declared_scope",
      "id": "user:501/default",
      "fingerprint": "sha256:SCOPE"
    },
    "purpose": "activation",
    "target_ids": ["application-installation:codex:vite"]
  },
  "evaluation": {
    "evaluated_at": "2026-07-20T00:02:00Z",
    "policy": {
      "id": "phase1-online-current",
      "version": 1,
      "fingerprint": "sha256:POLICY"
    },
    "evidence_set_fingerprint": "sha256:EVIDENCE_SET"
  },
  "evidence_ids": [
    "sha256:CONDITION_EVIDENCE",
    "sha256:LIFECYCLE_EVIDENCE"
  ],
  "claims": [],
  "claim_qualifications": [],
  "claim_applicability": [],
  "claim_conflicts": [],
  "positions": {
    "support": [],
    "permission": [],
    "searches": []
  },
  "approvals": [],
  "policy_assessment": {
    "disposition": "eligible",
    "applicable_claim_ids": [],
    "claim_conflict_ids": [],
    "rule_evaluations": [],
    "approval_evaluation": {
      "requirement": "not_required",
      "evaluations": []
    }
  },
  "operational_assessment": {
    "completion": {
      "value": "partial",
      "required_step_ids": ["application.codex.upgrade"],
      "step_satisfactions": []
    },
    "targets": [
      {
        "target_id": "application-installation:codex:vite",
        "target_kind": "external_application_installation",
        "subject_fingerprint": "sha256:TARGET",
        "plan_target_fingerprint": "sha256:PLAN_TARGET",
        "completion": "partial",
        "lifecycle": {
          "observation": "observed",
          "state": "active",
          "observed_at": "2026-07-20T00:02:00Z",
          "evidence_ids": ["sha256:LIFECYCLE_EVIDENCE"]
        },
        "operational_condition": {
          "observation": "observed",
          "value": "functional",
          "observed_at": "2026-07-20T00:02:00Z",
          "evidence_ids": ["sha256:CONDITION_EVIDENCE"]
        },
        "issue_ids": []
      }
    ],
    "summary": {
      "by_completion": {
        "partial": ["application-installation:codex:vite"],
        "complete": []
      },
      "by_lifecycle_state": {
        "absent": [],
        "present": [],
        "transitioning": [],
        "active": ["application-installation:codex:vite"]
      },
      "by_operational_condition": {
        "functional": ["application-installation:codex:vite"],
        "degraded": [],
        "nonfunctional": []
      },
      "lifecycle_not_observed": [],
      "lifecycle_not_applicable": [],
      "condition_not_observed": [],
      "condition_not_applicable": []
    }
  },
  "issues": []
}
```

Every Claim reference resolves within the assessment. Evidence, Plan, Plan
Approval, and Plan Assessment references resolve by exact `(kind, identity)` in
their owning immutable record repositories. An Evidence-set fingerprint binds
the exact sorted top-level `evidence_ids` used for evaluation but is never
itself a locator. That array is the validated, sorted, unique closure of every
Evidence identity referenced by Claim Qualifications, position searches, rule
evaluations, operational projections, and issues, plus every
`authorization_evidence_ids` identity referenced by a satisfied `skipped` Step
Evidence record. Every identity resolves exactly as
`(mastic.evidence, evidence_identity)`, every nested or transitive Evidence
reference is in the closure, and the closure has no unreferenced member. A rule
that consumes Evidence directly records those identities in its rule
evaluation. Policy
Assessment and Plan Operational Assessment use the same Plan, Plan Purpose,
Evidence set, policy, and evaluation time but remain sibling projections. An
applicable Plan Approval's `evidence_set_fingerprint` must equal this closure's
fingerprint.

Every `issue_ids` value anywhere in the assessment resolves to exactly one
entry in `issues` with the same `issue_identity`. A target-scoped issue is
referenced by its applicable target, lifecycle, or condition projection. A
Plan- or assessment-scoped issue may remain without a reverse `issue_ids`
reference; it remains explicit in the top-level `issues` array.

`approvals` is a lexicographically identity-sorted array of unique exact
references with this shape:

```json
{"identity":"sha256:APPROVAL","kind":"mastic.plan-approval"}
```

It contains every Plan Approval considered by the assessment. Each reference
resolves to the immutable Planning Record Repository entry with that
`approval_identity`; the complete canonical reference array participates in
`assessment_identity`. A non-null `approval_identity` in a rule evaluation or
approval evaluation must name an entry in `approvals`.

## Policy Assessment

`policy_assessment` requires:

```json
{
  "disposition": "approval_required",
  "applicable_claim_ids": ["sha256:CLAIM"],
  "claim_conflict_ids": [],
  "rule_evaluations": [
    {
      "rule_id": "publisher-support-exception",
      "result": "approval_required",
      "claim_ids": ["sha256:CLAIM"],
      "evidence_ids": [],
      "approval_identity": null
    }
  ],
  "approval_evaluation": {
    "requirement": "missing",
    "evaluations": []
  }
}
```

`disposition` is `eligible`, `approval_required`, or `blocked`.
`rule_evaluations[].result` is `satisfied`, `approval_required`, or `blocked`.
Every rule evaluation contains sorted, unique `claim_ids` and `evidence_ids`
for the exact direct inputs to that rule.
`approval_evaluation.requirement` is `not_required`, `missing`, or `evaluated`.
For `not_required` and `missing`, both `approvals` and the `evaluations` array
are empty. For `evaluated`, both are nonempty and `evaluations` contains exactly
one entry for every reference in `approvals`, sorted by `approval_identity`,
with this shape:

```json
{"approval_identity":"sha256:APPROVAL","value":"applicable"}
```

Each evaluation value is `applicable` or `inapplicable`, and approval
identities are unique. A Verified, Applicable prohibition from a
policy-recognized binding authority requires `blocked`. Other qualifications
and conflicts may also fail closed through explicit rule evaluations without
establishing that a prohibition exists.

`approval_required` requires `approval_evaluation.requirement` to be `missing`
or `evaluated` with at least one approval-required rule still unsatisfied. One
or more applicable sufficient Approvals satisfy their named approval-required
rules and trigger reassessment to `eligible` unless an independent rule blocks
the Plan. `blocked` takes precedence over approvals and cannot be changed by an
Approval unless the blocking rule is explicitly overridable and named by an
applicable Override.

## Plan Operational Assessment

`operational_assessment` requires:

```json
{
  "completion": {
    "value": "partial",
    "required_step_ids": ["application.codex.upgrade"],
    "step_satisfactions": []
  },
  "targets": [
    {
      "target_id": "application-installation:codex:vite",
      "target_kind": "external_application_installation",
      "subject_fingerprint": "sha256:TARGET",
      "plan_target_fingerprint": "sha256:PLAN_TARGET",
      "completion": "partial",
      "lifecycle": {
        "observation": "observed",
        "state": "present",
        "observed_at": "2026-07-20T00:02:00Z",
        "evidence_ids": ["sha256:LIFECYCLE_EVIDENCE"]
      },
      "operational_condition": {
        "observation": "not_observed",
        "issue_ids": ["sha256:ISSUE"]
      },
      "issue_ids": ["sha256:ISSUE"]
    }
  ],
  "summary": {
    "by_completion": {
      "partial": ["application-installation:codex:vite"],
      "complete": []
    },
    "by_lifecycle_state": {
      "absent": [],
      "present": ["application-installation:codex:vite"],
      "transitioning": [],
      "active": []
    },
    "by_operational_condition": {
      "functional": [],
      "degraded": [],
      "nonfunctional": []
    },
    "lifecycle_not_observed": [],
    "lifecycle_not_applicable": [],
    "condition_not_observed": ["application-installation:codex:vite"],
    "condition_not_applicable": []
  }
}
```

Completion is `partial` or `complete`. `step_satisfactions` contains one entry
per satisfied required step with exact `step_id`, `step_fingerprint`,
`step_evidence_kind`, `step_evidence_identity`, and `evidence_result`.
Every satisfaction's `(step_id, step_fingerprint)` tuple matches exactly one
`required_steps` entry. Duplicate, optional, unrelated, and stale-step
satisfactions are invalid.
`step_evidence_kind` is always `mastic.plan-step-evidence`, and the pair resolves
the exact Operational Record Repository entry. `evidence_result` is exactly
`complete` or `skipped` and equals that resolved Step Evidence record's
`result`. A satisfaction may name only an admissible `complete` or authorized
`skipped` Step Evidence record. A satisfaction contains `reuse: null` when Step
Evidence belongs to the current Plan. In that case, the resolved record's
`plan_identity`, `plan_purpose`, `step_id`, `step_fingerprint`, `target_id`, and
`plan_target_fingerprint` must equal the current Plan, its purpose, and the
required step and target. Cross-Plan reuse requires a `reuse` object containing
`rule_id` and `source_plan_identity`; the resolved record's `plan_identity` must
equal `source_plan_identity`, its `plan_purpose` must equal the current Plan
Purpose, and its step and target identities and fingerprints must equal the
current required step and target. The current Plan step must name the same
non-null `reuse_rule_id`, and the rule must revalidate its exact subject
bindings, material, freshness, and applicability. A changed Plan Target requires
new Step Evidence. Completion is `complete` only when every required
purpose-bound step has exactly one such binding. Per-target Completion is
derived from that target's required steps; Plan Completion requires every target
to be `complete`. Operational Assessment targets are an exact one-to-one
projection of the Plan target set. Every operational target's
`(target_id, plan_target_fingerprint)` tuple matches exactly one Plan target,
binding its lifecycle and condition projections to the Plan's purpose-specific
expected lifecycle and Operational Contract. Duplicate, missing, or mixed
target tuples are invalid.

### Lifecycle projection

`lifecycle.observation` is `observed`, `not_observed`, or `not_applicable`.

- `observed` requires `state`, `observed_at`, and nonempty `evidence_ids`.
- `state` is `absent`, `present`, `transitioning`, or `active`.
- `transitioning` additionally requires `source_state`, `destination_state`, and
  `operation_identity`; source and destination must be distinct and each must
  be `absent`, `present`, or `active`. The other states forbid those fields.
- `not_observed` requires nonempty `issue_ids` and forbids `state`.
- `not_applicable` requires a stable `reason_code` and forbids `state`.

`not_applicable` is required exactly when the bound Plan Target declares
`lifecycle_applicability: not_applicable`. An applicable lifecycle permits only
`observed` or `not_observed`.

An authoritative negative observation may establish `absent`. Timeout, missing
authority, stale Evidence, or failed inspection must use `not_observed`.
`active` means the target exists and is the current effective realization in
its target-specific operational relationship or participates in live
execution. Plan selection or a Desired State reference alone never makes it
`active`.

### Operational-condition projection

`operational_condition.observation` is `observed`, `not_observed`, or
`not_applicable`.

- `observed` requires `value`, `observed_at`, and nonempty `evidence_ids`.
- `value` is `functional`, `degraded`, or `nonfunctional`.
- `not_observed` requires nonempty `issue_ids` and forbids `value`.
- `not_applicable` requires a stable `reason_code` and forbids `value`.

Observation failure never implies `nonfunctional`. Performance may establish
`degraded` only when the target's exact operational contract includes the
measured threshold. A null Operational Contract requires `not_applicable`. A
non-null contract defines the assessment but condition remains `not_applicable`
when the current lifecycle makes functional observation meaningless.

### Operational Summary

The summary arrays form identity-preserving partitions of the exact selected
target set. Each target appears exactly once under Completion, exactly once
under observed lifecycle state, `lifecycle_not_observed`, or
`lifecycle_not_applicable`, and exactly once under observed condition,
`condition_not_observed`, or `condition_not_applicable`. The summary has no
scalar condition, severity, readiness, or worst-state ordering.

## Issues

Each issue requires:

```json
{
  "issue_identity": "sha256:ISSUE",
  "category": "observation",
  "code": "condition_not_observed",
  "scope": {
    "kind": "target",
    "id": "application-installation:codex:vite"
  },
  "message": "The current Codex condition has not been observed.",
  "evidence_ids": [],
  "next_actions": ["run the bounded Codex canary"]
}
```

`category` is `observation`, `execution`, or `policy`. Issue codes are the
machine contract; messages and next actions are human-facing. Issues diagnose
facts and never carry an implicit Plan Disposition.

`next_actions` is a semantic sequence in recommended execution order. It
contains no duplicates, is serialized in its declared order, and participates
in `issue_identity`; reordering it changes the Issue identity.

## Public operation projections

Version-2 operations use distinct outer kinds:

- `mastic.status`
- `mastic.setup-preview`
- `mastic.setup-result`

Each contains `schema_version: 2`, its exact `kind`, `operation`, and one
explicit `projection` discriminator:

| `projection` | Required payload | Permitted outer kinds |
| --- | --- | --- |
| `assessed_plan` | Complete `assessment`; `discovery` and `no_current_plan` are `null` | all three |
| `plan_discovery` | Complete `discovery`; `assessment` and `no_current_plan` are `null` | setup preview and result |
| `no_current_plan` | `no_current_plan` with exact `scope`, `observed_at`, and `issues`; `assessment` and `discovery` are `null` | status only |

Here, a complete `assessment` is the complete `mastic.plan-assessment` public
record defined above, and a complete `discovery` is the complete
`mastic.plan-discovery` public record defined below. Each nested payload retains
its own `schema_version: 2` and exact `kind`; the operation neither strips nor
rewraps that envelope. This is an explicit exception to the shared convention
for embedded objects.

This is an explicit exception to the general null rule. A first-run status uses
`no_current_plan`; a no-candidate setup response uses `plan_discovery`. Neither
fabricates a Plan, Completion, lifecycle, condition, or disposition.
Operation-specific supervisor, gateway, service, preview, execution, and
progress fields remain outside these payloads. Claims, positions, policy
results, and operational results are never flattened into one status field.

## Plan discovery

Discovery uses `kind: "mastic.plan-discovery"` and contains:

```json
{
  "schema_version": 2,
  "kind": "mastic.plan-discovery",
  "blueprint_identity": "sha256:BLUEPRINT",
  "scope": {
    "kind": "declared_scope",
    "id": "user:501/default",
    "fingerprint": "sha256:SCOPE"
  },
  "intended_purpose": "activation",
  "evaluated_at": "2026-07-20T00:00:00Z",
  "policy_fingerprint": "sha256:POLICY",
  "selection_outcome": "no_eligible_candidate",
  "selected_plan_identity": null,
  "candidates": [
    {
      "plan_identity": "sha256:CANDIDATE",
      "purpose": "activation",
      "disposition": "blocked"
    }
  ],
  "issues": []
}
```

`selection_outcome` is `candidate_selected`, `no_candidate`, or
`no_eligible_candidate`.

- Every candidate has `purpose` equal to `intended_purpose`, and candidate
  `plan_identity` values are unique. `disposition` is exactly `eligible`,
  `approval_required`, or `blocked`.
- `candidate_selected` requires nonempty `candidates` and one matching
  `selected_plan_identity`; exactly one matching candidate exists and is
  `eligible`.
- `no_candidate` requires empty `candidates` and a null selection.
- `no_eligible_candidate` requires nonempty `candidates`, no `eligible` candidate,
  and a null selection.

Discovery never contains Completion, Target Lifecycle State, Operational
Condition, or synthetic Plan Disposition when no Plan exists.

## Schema-version-1 compatibility boundaries

Version 2 is the only canonical writer. Version 1 remains a documented inbound
read adapter while consumers migrate.

1. A stored version-1 `setup_plan` omitted Plan Purpose. It cannot become a
   version-2 Plan identity, resume authority, or Approval target. Mutation
   requires fresh purpose-bound resolution into a version-2 Plan.
2. Version-1 step Evidence remains historical input but cannot directly satisfy
   version-2 Completion because it lacks a purpose-bound version-2 step
   identity. Reuse requires a new version-2 validation result; only version-2
   Step Evidence can then participate in a cross-Plan reuse evaluation.
3. Version-1 `readiness` and `application_target_readiness` never map to Claim
   Qualification, Support Position, Permission Position, Plan Disposition,
   Target Lifecycle State, or Operational Condition. A version-1 response may
   preserve them only as legacy fields.
4. Version-1 `no_validated_fit` becomes `no_candidate` only after fresh
   purpose-bound discovery establishes that result.

### Legacy outbound compatibility projection

When an existing public consumer still requires version 1, only an explicitly
compatible `activation` Plan shape has an outbound projection. A compatible
shape requires every lifecycle-applicable target to declare `active` as its
expected lifecycle state; an activation Plan that expects `present` or `absent`
for any such target has no version-1 projection. Validation, reconciliation,
rollback, and removal Plans have no version-1 projection. A compatible
activation assessment projects to version-1 `pending` when
Completion is Partial. A Complete assessment projects to `ready` only when
every lifecycle-applicable target has `lifecycle.observation: observed` and
`state: active`, every lifecycle-inapplicable target has
`lifecycle.observation: not_applicable`, and every target either has
`operational_condition.observation: observed` with `value: functional` or has
`operational_condition.observation: not_applicable`. At least one target must
be observed `active` or `functional`. This preserves a condition-observed
gateway-only projection without allowing an all-not-applicable assessment to
become `ready`. It projects to `degraded` only when the same lifecycle
requirements hold, every target either has
`operational_condition.observation: observed` with `value: functional` or
`value: degraded` or has `operational_condition.observation: not_applicable`,
and at least one target is observed `degraded`. Every other Complete compatible
activation assessment projects to `unverified`.

This lossy outbound projection is never persisted and is never used for policy,
checks, execution gates, resume, reconciliation, or canonical status. It may
expose compatible issues but never injects Claims, positions, Approval, or
Disposition into legacy readiness.

## Retirement gate

Version-1 reading may be removed only in a later schema version after all of
these are true:

- every supported public consumer accepts version 2;
- all retained version-1 state has a supported migration or archival read path;
- restore, bootstrap, rollback, and recovery tooling reads version 2;
- version-1 Evidence is never reused without a version-2 purpose-bound identity;
- fixtures prove version-1 decoding, lossy version-2 projection, and rejection
  of mismatched purpose and Approval fingerprints;
- the published compatibility window has elapsed.

SQLite `PRAGMA user_version` remains an independently migrated storage-engine
contract and is never inferred from this public schema version.
