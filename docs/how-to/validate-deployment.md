# How to validate a target-Mac deployment

Use this guide after installing or updating MASTIC on a compatible Mac. It
checks the installed entry points, inactive lifecycle, Application
Configuration Targets, bounded application-native Codex and Hindsight paths,
durable setup evidence, one configured service, and clean shutdown.

The current performance profile is provisional. A successful native canary is
therefore valid exact-contract evidence but remains `Unverified` until matching
clean-host measurements validate the profile. Do not treat this guide as a
support claim.

Use an inactive test stack or a maintenance window. If `mastic status` reports
a running Supervisor, Gateway, or service that must remain available, stop here
and reschedule these checks; the lifecycle steps deliberately stop the stack.

## Verify the installed control surface

```sh
mastic --help
masticd --help
mastic status
mastic runtime available
mastic setup --help
```

These observations must succeed without starting the Supervisor. Confirm that
`status` still reports the Supervisor as stopped.

## Start the explicit lifecycle

```sh
mastic supervisor start
mastic supervisor status
```

Leave the Supervisor running for the Gateway and service checks below. The
final section stops the complete stack and verifies that reading status does
not reactivate `masticd`.

## Refresh durable setup evidence

Run the guided setup:

```sh
mastic setup
```

Review the exact preview and confirm it. Setup resumes matching completed work,
applies any changed steps, starts the selected service, and finishes with one
bounded application-native canary for each selected target. A failure is
attributed to its exact step and can be resumed by running setup again.

Inspect the persisted outcome:

```sh
mastic status --json
mastic check
```

For the recommended selection, `completion` must be `complete` and
`application_target_readiness` must contain `codex` and `hindsight`; for an
exact selection, it contains only the selected targets. With the provisional
profile, correct canaries remain `unverified`; that state alone does not make
`check` fail. Missing, drifted, incompatible, malformed, unmanaged, or
unobservable managed target state does fail the check and supplies bounded
next actions.

## Verify Application Configuration Target metadata

After configuring Codex, run:

```sh
mastic application-target inspect codex
codex debug models
```

The MASTIC route and context cap must agree, and Codex must not report a
fallback-metadata warning. If the installed Codex exposes custom catalogues
through app-server `model/list`, verify the same route and context there;
versions that return only the bundled catalogue use `codex debug models` as
the acceptance surface.

After configuring Hindsight, inspect the selected profile:

```sh
mastic application-target inspect hindsight
```

Confirm that it reports the intended MASTIC route and owned settings without
claiming unrelated profile fields.

## Verify the application-native paths

Run the target-specific native checks:

```sh
mastic service start SERVICE_NAME
mastic application-target test codex --profile coding
mastic application-target test hindsight --profile retain
```

The Codex check invokes an ephemeral, read-only `codex exec` through the owned
Responses configuration and requires one exact bounded response. The Hindsight
check starts a disposable loopback API and database, then exercises bank
creation, retain, and reflect through the owned configuration. Both checks
return content-free phase, digest, exact-contract, and duration evidence.

These standalone checks are useful for diagnosis, but they do not rewrite the
saved setup outcome. Confirmed setup runs the same checks as resumable terminal
steps and owns durable `Completion` and `Readiness` evidence. Leave the selected
service running for the next section.

## Verify one configured service

With the service running, inspect it and use the exact Runtime Installation and
Model Installation identities reported in the result:

```sh
mastic service inspect SERVICE_NAME
mastic service check SERVICE_NAME
mastic runtime inspect RUNTIME_INSTALLATION
mastic model verify MODEL_INSTALLATION
mastic gateway routes
mastic service logs SERVICE_NAME
mastic service metrics SERVICE_NAME
mastic service stop SERVICE_NAME
mastic supervisor stop
mastic status
```

`service inspect` must show the intended model, runtime, resolved launch
arguments, and Gateway route. `service check` must pass while the service is
healthy. Runtime inspection and model verification must identify the exact
installed artifacts. The Gateway route list must contain the service, and its
logs and metrics must correlate with that service's run. The final command must
report the Supervisor as stopped. `service stop` first demonstrates a clean
service drain; `supervisor stop` then stops the Gateway and Supervisor, and the
status observation must not reactivate either one.

## Preserve the readiness boundary

The native procedures establish whether each application consumed its owned
configuration and returned the exact contract. `Ready` and `Degraded` also
require a validated performance profile bound to the exact setup plan,
application versions, macOS major version, and supported host class. The
repository's profile remains provisional, so MASTIC correctly reports
`Unverified` even after successful canaries.

Record the exact plan identity, model, runtime, application versions, host
profile, per-target duration, evidence digest, and observed result when
collecting the clean-host measurements needed to validate that profile.

For the paths and ownership rules behind these checks, see the
[deployment contract](../reference/deployment-contract.md).
