---
status: accepted
---

# Resolve current external applications with expiring evidence

MASTIC defaults an External Application Installation whose Release Intent tracks
current to the current release of its selected Release Channel. An exact Release
Intent preserves the selected exact release and bypasses current-release lookup.
For a Release Intent that tracks current, MASTIC resolves the release through
the selected Release Channel's authority, records the exact result as
time-bounded Plan evidence, and delegates installation to the Installation
Owner. This avoids historical release pins and shadow installations without
pretending that integrity, compatibility, or a successful probe proves
currentness.

## Decision

A canonical unsigned Current Release Resolution payload binds one External
Application Installation, Installation Owner, effective Release Channel,
platform and architecture, exact release, artifact coordinate and digest,
authority identity and normalized response digest, observation and expiry
times, resolver-policy and validation-profile identities, evidence provenance,
and a discriminated signature binding. A `derived_attestation` binding contains
the Attestation Issuer identity, and its evidence envelope attaches that
issuer's signature over the canonical resolution payload. An `upstream_claim`
binding instead contains the profile-trusted upstream key identity, and its
envelope attaches that upstream signature over the entire canonical resolution
payload; it contains no Attestation Issuer. Signature fields are not part of the
canonical resolution payload, and verifiers reject missing, mixed, or
kind-incompatible binding fields. An upstream signature that does not cover
every canonical payload field is ineligible for `upstream_claim`; resolution
must use `derived_attestation` instead.

Resolution reads the authority, materializes the exact artifact evidence, and
reads the authority again. A changed result retries within a bounded limit.
Persistent instability leaves only the affected installation's current-release
resolution unresolved and prevents that installation from contributing a fully
specified exact target to a candidate Plan. Discovery aggregates the issue only
when the Plan Purpose requires that installation; unrelated installations remain
independently resolvable.

Plans are immutable. Online Apply re-resolves at the last safe point before
each affected installation mutation. A changed exact artifact, owner, channel,
evidence mode, or material compatibility evidence blocks that Plan and creates
a successor Plan with a focused approval diff. If owner-native auto-update has
already produced the Plan's exact artifact, MASTIC may treat the mutation as
complete only after revalidating the owner, active installation, artifact
identity, and dependent evidence. MASTIC never infers another owner or creates
a shadow installation.

Evidence mode is selected independently for each installation, so one Plan may
combine online and offline installations. An offline current-tracking Release
Intent requires the exact artifact to be locally present and digest-bound plus
exactly one authentic, unexpired signature path: either a
`derived_attestation` or a profile-trusted upstream signature over the entire
canonical resolution payload. In either case, the Currency Claim is only
“latest observed on this channel at this time, acceptable under this policy
until expiry.” Freshness begins at the authority observation. Validation
profiles set application- and
channel-specific maximum ages under a repository-wide ceiling. The immutable
Plan records the earliest expiry imposed by the authority evidence,
signed currentness Evidence, and profile; neither the Plan nor its operator
selects or extends that policy.

Evidence uses exactly one signature path. For `upstream_claim`, an upstream
signature may serve directly only when it covers the entire canonical resolution
payload under a profile-trusted key. Coverage includes the installation, owner,
channel, platform and architecture, validation-profile identity, observation
and policy-derived expiry, and the exact release, artifact coordinate, and
artifact digest as one authenticated tuple. A publisher signature over only an
artifact, release, or currentness statement is retained as provenance but is
insufficient for `upstream_claim`. For `derived_attestation`, an Attestation
Issuer authorized by the validation profile signs the canonical unsigned
resolution payload after observing the authority over authenticated transport.
The target verifies the kind-specific binding, trusted key, signature, and
expiry. This proves only what the selected signature path authenticates; it
never represents an issuer observation as a publisher-signed statement.

Offline expiry requires coherent time. MASTIC checks signed observation and
expiry bounds against local time and a persisted highest-trusted-time
watermark, allowing only a small policy-defined skew. Clock rollback, missing
or expired evidence, authority contradiction, and owner ambiguity prevent
the affected installation from contributing a candidate target when they leave
no fully specified exact release. When an exact candidate target already exists,
those conditions make its Plan a Blocked Plan. No new installation mutation
starts after expiry; an operation already in flight may only reach a safe
completion, verification, or rollback boundary. An online
authority outage never silently changes evidence mode. A complete, unexpired
closure may instead support a separately generated offline or mixed-mode
successor Plan.

Release Intent, Currency Claim, Plan classification, Completion, and Readiness
are distinct. Plan classifications are mutually exclusive current evaluations
of immutable Plan evidence; reevaluation does not rewrite the Plan or its
history. Missing or expired currency evidence makes an existing Plan whose
Release Intent tracks current a Blocked Plan. A Plan with an exact Release Intent
makes no currentness claim but may be a Validated Plan when it satisfies the
validation profile, even after its former channel advances. Known-Risk requires
evidence of an actual overridable soft conflict; an exact Release Intent is not
itself a risk. MASTIC never silently substitutes an exact Release Intent for a
current-tracking Release Intent or an older validated release for an unresolved
current release.

An ordinary channel withdrawal may permit a new Plan with an exact Release
Intent. A security revocation, same-release digest contradiction, or hard safety
violation is non-overridable and Blocked. A Release Intent that tracks current
never causes an implicit downgrade when an authority moves backward; a Plan
with an exact Release Intent may propose a native-owner downgrade only with
complete evidence and approval.

## Consequences

Static application versions and immutable artifact digests remain useful test
fixtures and integrity inputs, but they cannot serve as current-release policy.
Each independently owned Hindsight installation resolves separately. Closure,
installation, canary, performance, and readiness evidence bind to the resolved
installation identity, and a change invalidates only that installation and its
dependent evidence rather than the entire closure indiscriminately.
