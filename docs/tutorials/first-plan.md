# Plan your first local inference service

In this tutorial, you will install MASTIC from source, inspect its initial
state, discover the built-in runtimes, and review a host-tailored Plan. You
will stop before applying the Plan, so no inference service or Application
Configuration Target is created.

## Before you begin

Use an Apple-silicon Mac with:

- macOS;
- Python 3.11 or newer;
- `git` and `uv` available in your shell;
- internet access for live model and runtime evidence, or the required
  artifacts already cached for offline planning.

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
and the service list should be empty. The observation itself succeeds without
starting either process.

## Discover the runtime definitions

Run:

```sh
mastic runtime available --plain
```

The built-in catalogue should include `mlx_lm`, `mlx_vlm`, and `optiq`.
These are definitions, not proof that a runtime is installed or that a
particular model fits the Mac.

## Review a recommended plan

Start the guided planner:

```sh
mastic setup --profile recommended
```

MASTIC inspects the host and presents the selected runtime, exact model
revision, service and route names, capacity, Application Configuration Targets,
owned paths, and planned mutations. Notice that the Plan keeps desired
configuration, observed state, validation evidence, and readiness separate.

When MASTIC asks for confirmation, answer `n`. The command exits without
applying the Plan, downloading a runtime or model, starting the Supervisor, or
changing Codex or Hindsight.

You have now completed the planning path. When you are ready to create the
stack, rerun `mastic setup`, review the current Plan again, and confirm it.
Material host evidence can change, so an earlier preview is not permission to
apply a later Plan unseen.

For day-to-day inspection after setup, continue with
[How to inspect and diagnose a local stack](../how-to/inspect-and-diagnose.md).
For exact command behavior, see the [CLI reference](../reference/cli.md).
