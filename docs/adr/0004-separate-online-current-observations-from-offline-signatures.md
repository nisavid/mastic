---
status: accepted
---

# Separate online current observations from offline signatures

Current-release resolution and offline authentication answer different
questions. The resolver observes what an owner-native Release Authority reports
for one exact installation query. A signature envelope proves that a complete
retained observation is authentic and unmodified when verified later without
consulting that authority. It does not prove that the observation remains true
or current.
Treating an intended signature path as if it were already signed Evidence would
collapse those boundaries and permit an unsigned record to look offline-ready.

## Decision

Direct online Current Release Resolution is Observed Evidence. Its canonical
payload binds the exact Installation Observation fingerprint and every
owner-, channel-, platform-, artifact-, authority-, policy-, and freshness
field needed to reproduce the query and assess the Currency Claim. It contains
no signature binding.

Offline use requires a separate Signed Current Release Resolution envelope.
That envelope binds the complete canonical online payload to exactly one
profile-authorized signature path and contains the corresponding authentic
signature. Selecting a future Attestation Issuer or upstream key is not
Evidence and cannot satisfy offline policy.

External Application Installation remains selected state. Installation
Observation remains time-bound Observed State and records the exact
owner-native installation identity, artifact, and reachable invocations. A
resolver rejects an owner, channel, platform, architecture, application, or
installation mismatch between them before any authority or artifact I/O.

## Consequences

The first target migration may use fail-closed online resolution without
implementing offline signatures. Signed offline Current support can be added as
an envelope and verifier without changing the online authority port or
pretending that a placeholder binding is authentic Evidence. Every resolution
also invalidates when its bound Installation Observation changes.
