# How to validate a target-Mac deployment

Use this guide after installing or updating MASTIC on a compatible Mac. It
checks the installed entry points, inactive lifecycle, Application
Configuration Targets, bounded application-native Codex and Hindsight paths,
durable setup evidence, one configured service, and clean shutdown.

The current performance profile is provisional. A successful native canary is
therefore valid exact-contract evidence but remains `unverified` until matching
clean-host measurements validate the profile. Do not treat this guide as a
support claim.

Use an inactive test stack or a maintenance window. If `mastic status` reports
a running Supervisor, Gateway, or service that must remain available, stop here
and reschedule these checks; the lifecycle steps deliberately stop the stack.

An update installed through `bootstrap-mastic.zsh` preserves the live lifecycle
state: an inactive Supervisor stays inactive, while a running Supervisor is
drained, unregistered, replaced, and restarted on the new code. Do not continue
this procedure after a nonzero bootstrap result. If bootstrap reports that it
restored the previous release but could not restart its Supervisor, run the
reported recovery command and verify `mastic supervisor status` first. If it
reports an incomplete rollback, do not start the Supervisor until the installed
release has been repaired. The same restriction applies when bootstrap reports
that it retained recovery backups because Supervisor state could not be
confirmed.

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

If a later command fails or you stop the procedure early, run
`mastic supervisor stop`, then `mastic status`. Confirm that the Supervisor is
stopped before retrying or leaving the maintenance window. Supervisor shutdown
drains every started service and the Gateway, so use it as the recovery command
even when setup did not create the selected service. Do not let an optional
individual service stop prevent this cleanup.

## Refresh durable setup evidence

Run the guided setup:

```sh
mastic setup
```

Review the exact preview and confirm it. Setup reuses matching terminal evidence
without reexecuting those steps, then applies only changed or incomplete steps.
The selected service starts and each selected target's bounded native canary
runs only when its corresponding step lacks matching terminal evidence. A
failure is attributed to its exact step and can be resumed by running setup
again. Reused service-start evidence does not prove that the service is still
running; inspect it before the standalone checks below and start it manually if
it is stopped.

Inspect the persisted outcome:

```sh
mastic status --json
mastic check
```

For the recommended selection, `completion` must be `complete` and
`application_target_readiness` must contain `codex` and `hindsight`; for an
exact selection, it contains only the selected targets. With the provisional
profile, correct canaries remain `unverified`; an explicitly skipped required
canary does too. Neither condition alone makes `check` fail. Missing, drifted,
incompatible, malformed, unmanaged, or unobservable managed target state does
fail the check and supplies bounded next actions.

## Verify Application Configuration Target metadata

For each selected target, follow its subsection and skip the other one. If Codex
is selected, run:

```sh
mastic application-target inspect codex
codex debug models
```

The MASTIC route and context cap must agree, and Codex must not report a
fallback-metadata warning. If the installed Codex exposes custom catalogues
through app-server `model/list`, verify the same route and context there;
versions that return only the bundled catalogue use `codex debug models` as
the acceptance surface.

If Hindsight is selected, inspect its configured environment profile:

```sh
mastic application-target inspect hindsight
```

Confirm that it reports the intended MASTIC route and owned settings without
claiming unrelated profile fields.

## Verify the application-native paths

Confirmed setup includes starting the selected service, but a resumed setup may
reuse prior terminal evidence. Inspect the current service state before the
target-specific native checks. If it is stopped, run
`mastic service start SERVICE_NAME` first.

Inspect the selected service, then run only the native checks for selected
targets:

```sh
mastic service inspect SERVICE_NAME
# When Codex is selected:
mastic application-target test codex --profile coding
# When Hindsight is selected:
mastic application-target test hindsight --profile retain
```

`coding` and `retain` are the canonical v1 native-canary sampling profiles. The
Hindsight canary uses the separately configured Hindsight environment profile
automatically.

The Codex check invokes an ephemeral, read-only `codex exec` through the owned
Responses configuration and requires one exact bounded response. The Hindsight
check starts a disposable loopback API and database, then exercises bank
creation, retain, and reflect through the owned configuration. Both checks
return content-free phase, digest, exact-contract, and duration evidence.

These standalone checks are useful for diagnosis, but they do not rewrite the
saved setup outcome. Confirmed setup runs the same checks as resumable terminal
steps and owns durable `completion` and `readiness` evidence. Leave the selected
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
configuration and returned the exact contract. `ready` and `degraded` also
require a validated performance profile bound to the exact Plan,
application versions, macOS major version, and supported host class. The
repository's profile remains provisional, so MASTIC correctly reports
`unverified` even after successful canaries.

Record the exact Plan identity, model, runtime, application versions, host
profile, per-target duration, evidence digest, and observed result when
collecting the clean-host measurements needed to validate that profile.

For the paths and ownership rules behind these checks, see the
[deployment contract](../reference/deployment-contract.md).
