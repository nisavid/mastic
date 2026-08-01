---
status: accepted
---

# Delegate approved Plan steps without interface reconfirmation

A Plan Approval authorizes the exact reviewed Plan and its step fingerprints.
Direct CLI and TUI mutations separately require confirmation of their resolved
interface preview. Applying both mechanisms to one setup run created a
dispatcher cycle: the approved setup Plan delegated a subordinate mutation
through the public interface, which either rejected the missing nested preview
fingerprint or would have needed to mint a second approval that the user never
reviewed.

## Decision

Application-owned setup coordination owns one typed nested-operation contract.
The contract composes separate desired-state, runtime-supply, model-supply,
service-lifecycle, external-application-lifecycle, application-configuration,
native-canary, verification, and product-state-removal capability ports. Each
operation carries the step fingerprint used for exact Completion Evidence.
Infrastructure adapters translate the operation and invoke the selected
physical owner directly. They never re-enter the public dispatcher, resolve a
second interface preview, or treat confirmation as a substitute for Plan
Approval.

## Consequences

Physical adapters still enforce current ownership, configuration revision,
host eligibility, and other mutation-time invariants. Completion Evidence is
recorded against the exact step fingerprint, so a resumed Plan skips only the
exact completed work. Public direct mutations retain their existing
preview-and-confirmation gate.
