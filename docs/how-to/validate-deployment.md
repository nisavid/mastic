# How to validate deployment prerequisites

Use this guide after installing or updating MASTIC on a compatible Mac. It
checks the installed entry points, inactive lifecycle, Application
Configuration Targets, one configured service, and clean shutdown. The current
milestone cannot establish Deployment Readiness because repeatable
application-native Codex and Hindsight checks are not yet available.

## Verify the installed control surface

```sh
mastic --help
masticd --help
mastic status
mastic runtime available
mastic setup --help
```

These observations must succeed without starting the Supervisor. Confirm that
`status` still reports the Supervisor as stopped unless it was already running.

## Verify explicit lifecycle control

```sh
mastic supervisor start
mastic supervisor status
mastic supervisor stop
```

After `stop` completes, run `mastic status` again. Reading status must not
reactivate `masticd`.

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

## Verify the managed Gateway contracts

Run the target-specific contract checks:

```sh
mastic application-target test codex --profile coding
mastic application-target test hindsight --profile retain
```

These checks exercise the managed Codex Responses and Hindsight Chat
Completions paths. They do not invoke either application and therefore leave
application-target Readiness `Unverified`.

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
```

`service inspect` must show the intended model, runtime, resolved launch
arguments, and Gateway route. `service check` must pass while the service is
healthy. Runtime inspection and model verification must identify the exact
installed artifacts. The Gateway route list must contain the service, and its
logs and metrics must correlate with that service's run. The final command must
drain and stop the service without stopping the Gateway.

## Keep application-native validation as a readiness gate

Deployment Readiness also requires a bounded request from both Codex and a
disposable Hindsight instance, using the configuration each application will
actually consume. This development milestone does not yet provide those safe,
repeatable procedures. Do not promote Readiness from `Unverified` to `Ready`
based only on `mastic application-target test` or a direct request to the private runtime
port. [Issue #20](https://github.com/nisavid/mastic/issues/20) tracks the full
clean-host gate; [issue #4](https://github.com/nisavid/mastic/issues/4) tracks
the remaining Codex conformance decision.

When those procedures are available, record the exact model, runtime,
application version, isolated-state details, and observed result as deployment
evidence.

For the paths and ownership rules behind these checks, see the
[deployment contract](../reference/deployment-contract.md).
