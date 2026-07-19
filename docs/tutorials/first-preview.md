# Preview your first local inference service

In this tutorial, you will install MASTIC from source, inspect its initial
state, discover the built-in runtimes, and review a host-tailored setup
preview. You will stop before applying the previewed operations, so no
inference service or Application Configuration Target is created.

## Before you begin

Use an Apple-silicon Mac with:

- macOS;
- Python 3.11 or newer;
- at least 48 GiB of unified memory and 24 GiB of free disk for the
  recommended profile used in this tutorial;
- `git` and `uv` available in your shell;
- internet access.

## Install MASTIC

Clone the repository and install the two command-line entry points:

```sh
git clone https://github.com/nisavid/mastic.git
cd mastic
uv tool install .
```

Confirm the installation:

```sh
mastic --help
masticd --help
```

You should see the MASTIC command catalogue and the foreground Supervisor
entry point. No background process starts during these checks.

## Inspect the initial state

Run:

```sh
mastic status --plain
```

On an unconfigured system, the Supervisor and Gateway should report `stopped`
and the service list should be empty. The `completion` field should be
`partial`, `readiness` should be `pending`, and
`application_target_readiness` should be empty. The observation itself
succeeds without starting either process.

## Discover the runtime definitions

Run:

```sh
mastic runtime available --plain
```

The built-in catalogue should include `mlx_lm`, `mlx_vlm`, and `optiq`.
These are definitions, not proof that a runtime is installed or that a
particular model fits the Mac.

## Review a recommended setup preview

Start the guided setup:

```sh
mastic setup --profile recommended
```

MASTIC inspects the host and presents the selected runtime, exact model
revision, service and route names, capacity, Application Configuration Targets,
host preflight, and proposed mutations. Notice that the preview keeps desired
configuration, observed state, validation evidence, and readiness separate.

When MASTIC asks for confirmation, answer `n`. The command exits without
applying the previewed operations, downloading a runtime or model, starting the
Supervisor, or changing Codex or Hindsight.

You have now completed the preview path.

## Next steps

When you are ready to create the stack, rerun `mastic setup`, review the current
preview again, and confirm it. Material host evidence can change, so an earlier
preview is not permission to apply later operations unseen.

For day-to-day inspection after setup, continue with
[How to inspect and diagnose a local stack](../how-to/inspect-and-diagnose.md).
For exact command behavior, see the [CLI reference](../reference/cli.md).
