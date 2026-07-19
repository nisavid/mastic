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

Setup tests each selected Codex or Hindsight integration before reporting its
readiness. Those application-native checks are implemented, but the recommended
performance thresholds remain provisional until matching clean-host
measurements validate them. A successful setup can therefore still report
`unverified` in this early milestone. See the
[deployment contract](docs/reference/deployment-contract.md#setup-outcomes-and-performance-evidence)
for the exact readiness rules.

The current milestone is deliberately narrow. MASTIC is not yet a general
adapter platform, remote inference host, multi-user service, or cross-platform
runtime manager. Exact model fit still depends on the Mac, workload, runtime,
and available evidence; MASTIC reports uncertainty instead of turning it into
a compatibility promise.

## Get started

Choose the path that matches what you want to do.

### Preview MASTIC without applying setup

This path installs the command-line tool from source so you can inspect MASTIC
and review a host-tailored setup preview. It does not install the selected
runtime or model, start a service, or configure Codex or Hindsight.

Install `git` and `uv`, then run:

```sh
git clone https://github.com/nisavid/mastic.git
cd mastic
uv tool install .
```

Review a recommended setup preview:

```sh
mastic setup --profile recommended
```

Answer `n` at confirmation when following the source-preview path. A source
install alone does not contain the attested Python, application artifacts, and
offline dependency closure required for confirmed setup on a clean host.

Follow [Preview your first local inference service](docs/tutorials/first-preview.md)
for the complete guided walkthrough.

### Set up a clean host from release artifacts

This path installs the exact MASTIC closure and proceeds to confirmed setup.
Obtain `bootstrap-mastic.zsh` and its closure from the same trusted release
artifact set, then run:

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
