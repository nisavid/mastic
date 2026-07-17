# How to validate a target-Mac deployment

Use this guide after installing or updating MASTIC on a compatible Mac. It
checks the installed entry points, inactive lifecycle, managed clients, one
configured service, and clean shutdown.

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

## Verify managed client metadata

After configuring Codex, run:

```sh
mastic client inspect codex
codex debug models
```

The MASTIC route and context cap must agree, and Codex must not report a
fallback-metadata warning. If the installed Codex exposes custom catalogues
through app-server `model/list`, verify the same route and context there;
versions that return only the bundled catalogue use `codex debug models` as
the acceptance surface.

After configuring Hindsight, inspect the selected profile:

```sh
mastic client inspect hindsight
```

Confirm that it reports the intended MASTIC route and owned settings without
claiming unrelated profile fields.

## Verify one configured service

With the service running, verify:

- the exact Runtime Installation and Model Revision;
- the resolved launch arguments;
- the stable Gateway route and `/v1/models` entry;
- one bounded completion or Responses request through the managed client;
- the correlated logs and metrics;
- a clean service stop.

The bounded request must use the application-native path you intend to support,
not only a direct request to the private runtime port. Record the exact model,
runtime, client version, and observed result as deployment evidence.

For the paths and ownership rules behind these checks, see the
[deployment contract](../reference/deployment-contract.md).
