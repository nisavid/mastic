# MASTIC

A guided local inference stack, tailored to your Apple-silicon Mac.

MASTIC previews, installs, and operates the pieces required to run local MLX
models without making you coordinate runtimes, model revisions, background
processes, ports, credentials, and application settings by hand. It gives
Codex and Hindsight stable authenticated paths through one loopback gateway,
with the same operations available through a CLI, TUI, and structured output.

MASTIC stands for **Modular, Adaptive, System-Tailored Inference Connector**.

## What MASTIC gives you

- **A setup preview before a mutation.** Review the exact runtime, model,
  service, gateway, Application Configuration Targets, host preflight, and
  ordered operations before confirmation permits any previewed setup operation.
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

Setup records a terminal canary step for each selected Codex or Hindsight
target. Unless explicitly skipped, the step runs a bounded application-native
canary and retains content-free exact-contract, phase, digest, and duration
evidence. A skipped required canary remains `unverified`. The recommended
performance policy also remains provisional until clean-host measurements from
a matching 48 GiB-or-larger Mac validate its thresholds, so a successful canary
currently remains `unverified`. This development target is not yet a support
claim.

The current milestone is deliberately narrow. MASTIC is not yet a general
adapter platform, remote inference host, multi-user service, or cross-platform
runtime manager. Exact model fit still depends on the Mac, workload, runtime,
and available evidence; MASTIC reports uncertainty instead of turning it into
a compatibility promise.

## Get started

To explore MASTIC from source, install `git` and `uv`, then install the command
line tool:

```sh
git clone https://github.com/nisavid/mastic.git
cd mastic
uv tool install .
```

You can inspect the command surface and review a recommended setup preview:

```sh
mastic setup --profile recommended
```

Answer `n` at confirmation when following the source-preview path. A source
install alone does not contain the attested Python, application artifacts, and
offline dependency closure required for confirmed setup on a clean host.

For a confirmed clean-host setup, obtain `bootstrap-mastic.zsh` and its exact
closure from the same trusted release artifact set, then run:

```sh
./bootstrap-mastic.zsh --artifact-dir RELEASE_ARTIFACT_DIRECTORY --yes
mastic setup
```

MASTIC inspects the host, builds an exact setup preview, and asks for
confirmation before applying it. Model and runtime downloads can be
substantial; review the selected revisions, projected resources, Application
Configuration Targets, preflight, and ordered operations before continuing.
After confirmation, setup reports installation `completion` separately from
application `readiness`, including one result for each selected target.

To learn the workflow without applying the previewed operations, follow
[Preview your first local inference service](docs/tutorials/first-preview.md).

## Operate an existing stack

```sh
mastic status
mastic check
mastic doctor
mastic tui
```

Read-only commands do not start the Supervisor or inference services. `status`
combines observed runtime state with durable setup `completion`, overall
`readiness`, and current per-target health. `check` applies the same view and
exits nonzero for operational failures or current target issues; provisional
or explicitly skipped `unverified` canary evidence alone is not a check
failure. `doctor` adds bounded issues and next actions.

See [How to inspect and diagnose a local stack](docs/how-to/inspect-and-diagnose.md)
for a focused recovery workflow.

## Documentation

The [documentation index](docs/README.md) routes by what you need now:

- learn through a guided first preview;
- complete operational tasks;
- look up CLI and deployment contracts;
- understand MASTIC's architecture and trust boundaries.

## Development

Run the project checks from a source checkout:

```sh
uv sync --locked --dev
uv run --frozen python -m unittest discover -s tests -t .
uv run --frozen pyrefly check --output-format=min-text
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv build --build-constraints packaging/build-backend.lock --require-hashes
```

Issues and implementation work are tracked in
[GitHub Issues](https://github.com/nisavid/mastic/issues).
