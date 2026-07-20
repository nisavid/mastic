---
status: accepted
---

# Separate upgrade policy, recovery evidence, and removal authority

External applications keep their own lifecycle owners even when MASTIC
coordinates their use. A durable preference for current applications, a
successful backup, and MASTIC's own installation provenance answer different
questions. Treating any one of them as standing mutation authority would permit
owner or channel drift, reuse stale recovery evidence, or delete an external
application during ordinary product removal.

## Decision

An Unattended Upgrade Policy is a durable subject-bound rule. It may require a
new exact Plan assessment for a same-owner, same-channel Current transition
that the Installation Owner classifies as an upgrade. A matching assessment
still requires an exact Plan and Plan Approval. The policy never acts as
approval and never authorizes a shadow installation, owner switch, channel
switch, same-release mutation, implicit downgrade, or unclassified transition.

A data-bearing upgrade additionally requires profile-specific Recovery
Qualification. Every candidate binds one fresh Validated Backup to the exact
source Installation Observation and target Current Release Resolution. The
qualification binds that candidate and backup to a successful isolated restore
and representative recall. A different source observation, target resolution,
validation profile, backup, or expired freshness interval invalidates it.

Ordinary `mastic remove` retains every External Application Installation,
including installations originally created by MASTIC. External-application
removal requires a separate exact Removal Plan naming the installation,
Installation Owner, owner-native installation identity, and current
Installation Observation fingerprint. Application Configuration Target and
MASTIC-owned product-state removal remain separate operations.

## Consequences

Policy evaluation can produce only `approval_required` or `blocked`; it cannot
report that a mutation is approved. Release direction comes from an
owner-specific lifecycle adapter rather than generic version-string ordering.
Hindsight backup, isolated-restore, and recall adapters can be implemented
without weakening the generic policy seam. The existing low-level application
remover is not reachable from ordinary product removal and can be replaced by
an exact Removal Plan executor later.

Reversible legacy-service deactivation remains distinct from permanent
cleanup. Files and state retained for rollback cannot be deleted without their
own accepted Removal Plan.
