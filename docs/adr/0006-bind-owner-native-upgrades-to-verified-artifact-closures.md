---
status: accepted
---

# Bind owner-native upgrades to verified artifact closures

An owner-preserving application upgrade must keep more than the application
name and release. It must retain the exact Installation Owner, owner-native
installation, owner runtime, release channel, invocation topology, and complete
installed artifact set. A command exit status cannot establish those facts,
and post-command observation drift must not erase evidence that a mutation was
already verified.

## Decision

An owner-native upgrade preview binds the stable source Installation
Observation, Current Release Resolution, policy assessment, exact owner action,
rollback source release, and one Verified Artifact Closure. The closure names
every required archive and its authority-selected release, archive digest, and
installed payload fingerprint. For Codex on Apple silicon, the closure contains
exactly the `@openai/codex` wrapper and its aliased
`@openai/codex-darwin-arm64` platform package at the corresponding release.

Materialization reads the release authority before and after selecting the
closure. It downloads bounded redirect-free artifacts from the selected
authority, verifies their SHA-512 digests, rejects unsafe or ambiguous archive
topology, and derives bounded payload fingerprints without extracting the
archives. Private staging and cache directories are retained through the
attempt and released exactly once afterward.

Apply verifies the Plan and Plan Approval through an authoritative verifier
before acquiring the installation transition lock. Under that lock it compares
the stable source state and fresh Current target with the preview, verifies the
staged closure, and performs a final expected-current discovery immediately
before starting the owner command. The request carries the exact owner-native
installation identity, owner runtime identity, stable state fingerprint,
preview, action, and closure.

The Installation Owner performs the mutation. A Vite+-managed Codex installation
uses the matching Vite+ owner path and exact observed Node runtime. Its command
receives only a sanitized environment and the private reviewed archive. The
first online installation may consult the official npm registry for dependency
resolution, but completion requires the installed wrapper and platform payload
fingerprints to equal the authority-selected retained archives. Extra,
symlinked, or relocated dependency roots are rejected.

The owner executor returns valid execution evidence only after rediscovery,
owner/runtime/release/invocation preservation, installed topology verification,
and installed payload verification on the same post-command observation.
Before valid execution evidence, an expected-current failure is
`not_attempted`; an attempt whose completion cannot be verified is `unknown`.
After valid execution evidence, the Mutation Outcome is `verified`. Later
observation or currentness drift requires a successor Plan but cannot
reclassify the completed mutation as unknown. An unknown outcome never triggers
an automatic rollback, because a second mutation without authoritative state
could compound the failure. Closure cleanup has its own outcome; a cleanup
failure retains the primary mutation classification and requires explicit
follow-up.

## Consequences

Observation time may refresh without invalidating a stable source or target,
while owner, runtime, installed bytes, release, channel, or invocation drift
invalidates the relevant compare-and-swap boundary. The resolved target
fingerprint represents the semantic authority-selected target and excludes
evidence acquisition times.

The upgrade adapter remains owner-specific, but authorization, stable-state
fencing, mutation outcome, and successor-plan semantics remain application
contracts. Offline installation requires a separate complete signed-currentness
and artifact-availability path; this decision does not infer it from an online
cache. Reversible legacy-service deactivation and permanent Removal Plan work
remain separate from the application upgrade.
