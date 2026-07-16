# mlxctl

`mlxctl` is a local inference manager for Apple-silicon Macs. It installs and
checks MLX runtimes, resolves exact model revisions, runs several named
Inference Services at once, and gives clients one stable OpenAI-compatible
Gateway at `http://127.0.0.1:8766/v1`.

Use `mlxctl` for day-to-day work. Its companion process, `mlxd`, owns the
Gateway and child runtime processes. You should not need to edit configuration
files or call `launchctl`, Hugging Face cache tools, or runtime binaries to
perform a supported operation.

First-party Runtime Definitions cover
[`mlx-lm`](https://github.com/ml-explore/mlx-lm), MLX-VLM, and
[OptiQ](https://github.com/ChenMnZ/OptiQ). Each Inference Service selects one
exact Runtime Installation, one stable Model Alias, its own private upstream
port, and a public Gateway route. Several services can therefore run at the
same time without competing for one hard-coded server port.

## Get a working service

This path installs mlxctl, creates the recommended service for a suitable Mac,
starts it, and sends a request through the Gateway.

### Requirements

- macOS on Apple silicon
- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- enough free memory and disk for the model you select

Clone the repository and install the tool:

```sh
git clone https://github.com/nisavid/systools.git
cd systools
uv tool install ./tools/mlxctl
```

Confirm that the complete interface is available:

```sh
mlxctl --help
mlxctl runtime available
```

Run guided setup:

```sh
mlxctl setup
```

On the 48 GiB target, guided setup offers three coherent capacity profiles:

| Profile | Context per request | Simultaneous requests | Use when |
| --- | ---: | ---: | --- |
| `balanced` | 128K | 6 | Several coding and memory clients may be active; this is the default. |
| `long-context` | 192K | 4 | Individual tasks need more source or retrieval context. |
| `native-context` | 256K | 3 | A request needs the model's full native context. |

Choose one directly with `mlxctl setup --capacity long-context`. A concurrency
slot is used only by an in-flight inference request: idle agents use no slot,
and requests beyond the selected limit queue. All three profiles keep the same
projected persistent KV budget and a 2 GiB prompt-prefix cache; they trade
context per request for concurrency without silently giving clients a larger
window than the service accepts. The complete resolved values remain visible
and editable in the CLI and TUI review plan.

mlxctl checks the Mac, renders an editable plan, and asks before changing
anything. The built-in recommendation currently targets Macs with at least
48 GiB of unified memory and 24 GiB of free disk. It installs the tested OptiQ
runtime, pins
`mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit` to an exact revision, uses the
model's `kv_config.json`, enables MTP, creates the `qwen36-optiq` service and
route, starts it, and verifies a request.

If no recommendation fits, setup stops without choosing an unsafe model. Use
the model workflow below to inspect a smaller candidate, then run
`mlxctl setup --profile expert --help` to supply an exact editable selection.

Check the resulting system:

```sh
mlxctl status
mlxctl service inspect qwen36-optiq
mlxctl gateway routes
```

Send an OpenAI-compatible request:

```sh
MLXCTL_TOKEN="$(<"${XDG_STATE_HOME:-$HOME/.local/state}/mlxctl/gateway.token")"
curl http://127.0.0.1:8766/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $MLXCTL_TOKEN" \
  -d '{
    "model": "qwen36-optiq",
    "messages": [{"role": "user", "content": "Write a tiny Swift hello world."}],
    "temperature": 0
  }'
```

The Gateway is private to the current user through an owner-only bearer
credential. `mlxctl gateway inspect` shows its location and client setup
instructions without revealing its value. `mlxctl client configure` wires the
credential into supported clients automatically; do not copy it into mlxctl
configuration, logs, or command arguments.

Stop only that model process while leaving the Gateway available:

```sh
mlxctl service stop qwen36-optiq
```

Stop every Service Run, the Gateway, and `mlxd` itself:

```sh
mlxctl supervisor stop
```

The next mutation that needs `mlxd` can activate it again. Read-only commands,
including `status`, `check`, `doctor`, lists, and inspections, never start it.

## Use the operations console

Run `mlxctl` with no arguments, or run `mlxctl tui`, to open the terminal UI.
It provides the same operations as the CLI from one shared catalogue.

- Use the left navigation for live resources, topology, diagnostics, and the
  complete command catalogue.
- Use the guided setup, model search, runtime install, and service builder
  actions for common workflows.
- Press `Ctrl+P` and type a command or intent to open any operation.
- Review the complete resolved plan before confirming a mutation.
- Press `?` for help and `q` to quit.

State is expressed with words and symbols as well as color. Narrow terminals
collapse secondary panes without removing operations.

## Find and manage models

Model discovery, fit analysis, installation, aliases, and cached bytes are
separate surfaces:

```sh
# Curated mlx-community candidates
mlxctl model search qwen --source curated

# Broader Hugging Face search
mlxctl model search qwen --source broad --limit 20

# Models already present in the local Hugging Face cache
mlxctl model search --source local

# Identity, declared capabilities, trust signals, and estimated Mac fit
mlxctl model inspect mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit \
  --revision 70a3aa32c7feef511182bf16aa332f37e8d82014 \
  --context-tokens 32768 \
  --concurrency 1

# Exact configured installations and aliases
mlxctl model list

# Physical cached revisions and disk usage
mlxctl model cache list
```

Install a chosen revision under a stable alias:

```sh
mlxctl model install OWNER/REPOSITORY \
  --revision EXACT_COMMIT_SHA \
  --alias my-model
```

If another tool already downloaded an exact Hugging Face snapshot into its own
directory, register it without copying the bytes:

```sh
mlxctl model adopt OWNER/REPOSITORY \
  --revision EXACT_40_CHARACTER_COMMIT_SHA \
  --path "$(pwd)/models/exact-snapshot" \
  --alias my-model
```

The preview fingerprints the existing directory. Confirmation then checks that
the directory did not change, assesses that exact repository revision, and
verifies its files against the Hub manifest before registration. Adopted
snapshots must be regular, non-symlink files owned by the current user. They
must also live outside mlxctl-owned directories and the managed Hugging Face
cache; use `model install` for a revision already in that cache, or move an
externally owned snapshot before adopting it.

`model verify`, `repair`, `update`, `rollback`, and `uninstall` manage the
logical installation. `model cache inspect`, `evict`, and `prune`
manage physical shared cache bytes. Cache deletion remains reference-aware.
Adopted bytes remain externally owned: `uninstall` only unregisters them,
`repair` directs you back to their owner, and cache eviction or pruning never
claims or deletes them.

Models that require remote code are refused by default. A trust grant names
the exact Model Revision, exact Runtime Installation, and accepted risk:

```sh
mlxctl model trust my-model \
  --runtime EXACT_RUNTIME_INSTALLATION \
  --accepted-risks '["remote_code"]'
```

Changing the revision or runtime invalidates that grant. Known security
findings and integrity mismatches cannot be overridden.

## Build and run several services

Inspect the available runtime families and installed instances:

```sh
mlxctl runtime available
mlxctl runtime list
mlxctl runtime doctor
```

Install the tested channel, then create a service from an installed runtime
and Model Alias:

```sh
mlxctl runtime install optiq

mlxctl service create assistant \
  --model-alias my-model \
  --runtime EXACT_RUNTIME_INSTALLATION \
  --route assistant \
  --options '{"max_context":32768,"kv_config":"kv_config.json","mtp":true}'
```

Run several services by giving each a different service name and route. Their
runtime processes receive private dynamic upstream ports; clients continue to
use the one Gateway port and select a route through the request's `model`
field.

```sh
mlxctl service start assistant
mlxctl service list
mlxctl gateway routes
mlxctl service stop assistant
```

`activation=manual` starts only when requested. `activation=supervisor` starts
with the Supervisor. Pinned services are protected from automatic pressure
eviction; critical memory pressure rejects new work but does not kill a busy or
pinned service.

## Configure Codex or Hindsight

Client integrations are owned and reversible. They write only the settings
needed for the selected Gateway route and retain ownership evidence for safe
removal. Guided setup configures both clients with the selected service context
and conservative sampling defaults.

For Codex, mlxctl also creates a custom model catalog from the installed
Codex version's bundled catalog, preserves its bundled coding instructions,
and declares only capabilities verified for the local route. This prevents
Codex from guessing fallback metadata for `qwen36-optiq`. `client inspect`
reports a missing, malformed, incompatible, or externally changed catalog and
gives the repair command; re-running `client configure codex` repairs it.

```sh
mlxctl client configure codex \
  --service assistant \
  --context-window 131072 \
  --sampling-profiles '{"coding":{"temperature":0}}'

mlxctl client configure hindsight \
  --service assistant \
  --profile default \
  --context-window 131072 \
  --max-concurrent 1 \
  --sampling-profiles '{
    "verification":{"temperature":0},
    "retain":{"temperature":0.1},
    "reflect":{"temperature":0.9},
    "consolidation":{"temperature":0}
  }'
```

Verify and remove an integration through mlxctl:

```sh
mlxctl client test codex
mlxctl client inspect codex
mlxctl client remove codex
```

## Diagnose and recover

Start with the narrowest surface that answers the question:

```sh
mlxctl status                 # whole-system overview
mlxctl check                  # concise component checks
mlxctl doctor                 # diagnosed issues and next actions
mlxctl service check NAME     # one service and route
mlxctl runtime doctor         # runtime roots and launchers
mlxctl logs NAME              # bounded product-owned logs
mlxctl operation list        # physical install/update operation history
```

Every command supports `--json`, `--json-lines`, and `--plain`. Mutations can
use `--yes` only after mlxctl has resolved an exact plan. Run any command with
`--help` for accepted values and discovery commands.

## Understand the resource model

- A **Runtime Definition** describes how mlx-lm, MLX-VLM, or OptiQ is installed,
  probed, and launched. A **Runtime Installation** is one exact local instance.
- A **Model Revision** is immutable Hugging Face content. A **Model
  Installation** records that revision, and a **Model Alias** gives services a
  stable name.
- An **Inference Service** combines one Model Alias, Runtime Installation,
  launch options, activation policy, and Gateway route. A **Service Run** is
  its current process.
- The **Gateway** owns the stable loopback client endpoint and routes by the
  OpenAI `model` field. It never starts a stopped service because a request
  arrived.
- The **Supervisor** owns the Gateway, Service Runs, pressure admission, and
  lifecycle evidence. It is explicit and per-user.

Desired state is strict TOML. Operational state is content-free SQLite in WAL
mode. Inference prompts and responses are never stored there.

## Files and deployment

| Purpose | Default path | Override |
| --- | --- | --- |
| Desired state | `~/.config/mlxctl/config.toml` | `MLXCTL_CONFIG_DIR` |
| Operations and socket | `~/.local/state/mlxctl/` | `MLXCTL_STATE_DIR` |
| Runtime Installations | `~/.local/share/mlxctl/runtimes/` | `MLXCTL_DATA_DIR` |
| Private logs | `~/Library/Logs/mlxctl/` | `MLXCTL_LOG_DIR` |

mlxctl creates owned directories with mode `0700`. Runtime and model content,
state, logs, and client ownership evidence do not belong in Git.

For launchd and deployment-tool integration, see
[`docs/deployment-contract.md`](docs/deployment-contract.md). For the resource
and process design, see [`docs/architecture.md`](docs/architecture.md).

## Develop mlxctl

Run checks from `tools/mlxctl/`:

```sh
uv run python -m unittest discover -s tests
uvx ruff check .
uvx ruff format --check .
uv build
```

The package uses a `src/` layout and locks its development environment in
`uv.lock`. Changes should test the public behavior they claim.

## License

mlxctl is available under the [MIT License](LICENSE).
