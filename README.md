# MASTIC

A guided local inference stack, tailored to your Apple-silicon Mac.

MASTIC plans, installs, and operates the pieces required to run local MLX
models without making you coordinate runtimes, model revisions, background
processes, ports, credentials, and application settings by hand. It gives
Codex and Hindsight stable authenticated paths through one loopback gateway,
with the same operations available through a CLI, TUI, and structured output.

MASTIC stands for **Modular, Adaptive, System-Tailored Inference Connector**.

## What MASTIC gives you

- **A Plan before a mutation.** Review the exact runtime, model, service,
  gateway, Application Configuration Targets, ownership, and recovery steps
  before anything changes.
- **Exact local components.** Runtime installations and model revisions are
  recorded, verified, and kept distinct from shared cache bytes.
- **One stable gateway.** Named inference services remain behind an
  authenticated OpenAI-compatible loopback endpoint while private process
  ports change underneath it.
- **Reversible application setup.** MASTIC configures only the Codex and
  Hindsight fields it owns, then can remove those fields without erasing
  unrelated user configuration.
- **Operational evidence.** Status, health checks, diagnostics, bounded logs,
  metrics, and durable operation records explain what is running and what to
  do next.

## Compatibility and project status

MASTIC is an early, source-installed project. Its first development target is
macOS on Apple silicon with Python 3.11 or newer. It manages MLX-LM, MLX-VLM,
and OptiQ runtime definitions and configures Codex and Hindsight as Application
Configuration Targets. The current recommended profile targets Macs with at
least 48 GiB of unified memory and 24 GiB of free disk. Other exact selections
use the exact-selection path and carry only the evidence collected for them.

The repository validates the control plane and each managed Gateway contract.
A clean-host, application-native Codex and Hindsight canary on the recommended
target remains pending, so this development target is not yet a support claim.

The current milestone is deliberately narrow. MASTIC is not yet a general
adapter platform, remote inference host, multi-user service, or cross-platform
runtime manager. Exact model fit still depends on the Mac, workload, runtime,
and available evidence; MASTIC reports uncertainty instead of turning it into
a compatibility promise.

## Get started

Install `git` and `uv`, then install MASTIC from a source checkout:

```sh
git clone https://github.com/nisavid/mastic.git
cd mastic
uv tool install .
```

Open the guided setup:

```sh
mastic setup
```

MASTIC inspects the host, builds a complete Plan, and asks for confirmation
before applying it. Model and runtime downloads can be substantial; review the
selected revisions, projected resources, Application Configuration Targets,
and owned paths in that Plan before continuing.

To learn the workflow without applying a Plan, follow
[Plan your first local inference service](docs/tutorials/first-plan.md).

## Operate an existing stack

```sh
mastic status
mastic check
mastic doctor
mastic tui
```

Read-only commands do not start the Supervisor or inference services. `status`
reports observed state; `check` returns a failing exit status when its health
policy is not satisfied; `doctor` adds diagnosis and next actions.

See [How to inspect and diagnose a local stack](docs/how-to/inspect-and-diagnose.md)
for a focused recovery workflow.

## Documentation

The [documentation index](docs/README.md) routes by what you need now:

- learn through a guided first plan;
- complete operational tasks;
- look up CLI and deployment contracts;
- understand MASTIC's architecture and trust boundaries.

## Development

Run the project checks from a source checkout:

```sh
uv run --frozen python -m unittest discover -s tests -t . -v
uv run --frozen pyrefly check --output-format min-text
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv build
```

Issues and implementation work are tracked in
[GitHub Issues](https://github.com/nisavid/mastic/issues).
