# mlxctl

`mlxctl` manages named local [MLX](https://github.com/ml-explore/mlx)
inference servers as services instead of loose terminal processes. It gives
clients stable loopback endpoints while a small supervisor starts and stops the
underlying server processes, checks readiness, and records local metrics.

The project provides two commands:

- `mlxctl` is the operator-facing CLI and terminal dashboard.
- `mlxd` is the foreground supervisor that owns server lifecycle and runtime
  state.

`mlxctl` targets Apple-silicon Macs and supports `mlx_lm` and `optiq` server
definitions. Runtime endpoints are loopback-only; the project does not expose
inference servers directly to a network.

## What it provides

- Named models and server definitions in one validated TOML file.
- Stable client ports in front of private, dynamically allocated upstream
  ports.
- Start, stop, status, model-discovery, and metrics commands with optional JSON
  output.
- A curses dashboard for interactive operation and a plain snapshot when
  output is redirected.
- Request and process metrics stored in a local SQLite database.
- A versioned Unix-socket control protocol between `mlxctl` and `mlxd`.
- Optional launchd activation when the documented LaunchAgent is installed.

## Get to a working control plane

This first path runs `mlxd` in the foreground. It verifies installation and
configuration without installing a LaunchAgent or downloading a model.

### Prerequisites

- macOS on Apple silicon
- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)

Clone the repository and install the tool:

```sh
git clone https://github.com/nisavid/systools.git
cd systools
uv tool install ./tools/mlxctl
```

Create the configuration directory with private permissions:

```sh
mkdir -p ~/.config/mlxd
chmod 700 ~/.config/mlxd
```

Create `~/.config/mlxd/config.toml`:

```toml
schema_version = 1

[models.small]
reference = "mlx-community/Llama-3.2-3B-Instruct-4bit"

[servers.local]
type = "mlx_lm"
model = "small"
port = 8765
```

In one terminal, start the supervisor with a longer idle window for this
check:

```sh
mlxd --idle-grace-seconds 300
```

In another terminal, inspect the configured server:

```sh
mlxctl status
```

You should see:

```text
local is stopped at http://127.0.0.1:8765.
```

Stop the foreground supervisor with `Ctrl-C`. The control plane is now ready;
install a supported server runtime before starting inference.

## Run a server

`mlxd` launches the selected runtime by command name. Ensure either
`mlx_lm.server` or `optiq` is installed and visible on the supervisor's
`PATH`, then start `mlxd` in the foreground as above or install the launchd
deployment described below.

Start the example server and inspect its stable endpoint:

```sh
mlxctl start local
mlxctl status local
```

Clients can use `http://127.0.0.1:8765` while the server is ready. Stop it when
finished:

```sh
mlxctl stop local
```

The first start may take longer while the selected runtime obtains model
weights. The readiness timeout defaults to 120 seconds and can be changed in
the `[daemon]` configuration table.

## Install launchd activation

On macOS, `mlxctl` can kickstart a registered per-user LaunchAgent when the
control socket is absent. The current activation seam expects the label
`io.nisavid.mlxd`; `RunAtLoad` and `KeepAlive` can remain disabled so the
supervisor starts only when a command needs it and exits after becoming idle.

The exact plist, directories, environment variables, permissions, and control
behavior are defined in the
[`mlxctl` deployment contract](docs/deployment-contract.md). The author's
chezmoi-managed deployment implements that contract, but it is not required
for foreground operation.

## Use the CLI

| Command | Purpose |
| --- | --- |
| `mlxctl start SERVER` | Start one configured server and wait for readiness. |
| `mlxctl stop SERVER` | Stop one managed server. |
| `mlxctl status [SERVER]` | Show lifecycle state and the stable client endpoint. |
| `mlxctl models SERVER` | Show model identifiers advertised by a ready server. |
| `mlxctl metrics [SERVER]` | Summarize request and process metrics. |
| `mlxctl dashboard` | Open the interactive dashboard, or print one snapshot when redirected. |

Every non-dashboard command accepts `--json`. Metrics can also be filtered with
`--model`, `--start`, and `--end`; timestamps must be timezone-aware ISO 8601
values. Run `mlxctl COMMAND --help` for the exact arguments.

In the interactive dashboard:

| Key | Action |
| --- | --- |
| `j` / `↓` | Select the next server. |
| `k` / `↑` | Select the previous server. |
| `s` | Start the selected server. |
| `x` | Stop the selected server. |
| `r` | Refresh immediately. |
| `q` | Quit. |

## Configure models and servers

The configuration file is `~/.config/mlxd/config.toml` by default. Version 1
has these top-level tables:

| Table | Purpose | Defaults |
| --- | --- | --- |
| `[daemon]` | Readiness, stop, and metrics-sampling intervals. | `readiness_timeout_seconds = 120`, `stop_timeout_seconds = 10`, `metrics_interval_seconds = 5` |
| `[metrics]` | Local metrics retention. | `retention_days = 30` |
| `[models.NAME]` | Maps a local alias to a model repository or filesystem reference. | `reference` is required. |
| `[servers.NAME]` | Declares one named server and stable client endpoint. | `host = "127.0.0.1"`; `type`, `model`, and `port` are required. |

A server may also define an `[servers.NAME.environment]` table of string
values and an `[servers.NAME.options]` table. `mlx_lm` accepts
`draft_model`, `prompt_cache_size`, `prompt_concurrency`, `pipeline`, `temp`,
`top_p`, and `top_k`. `optiq` accepts those options plus `adapter`,
`allow_model_switch`, `anthropic`, `idle_timeout`, `kv_bits`, `kv_config`,
`kv_group_size`, `max_context`, and `quantized_kv_start`.

Aliases must match `[A-Za-z0-9][A-Za-z0-9._-]*`. Hosts must be `localhost` or
a literal loopback IP address, client host/port pairs must be unique, and
unknown fields or options are rejected before the supervisor starts.

## Runtime files

| Purpose | Default | Override |
| --- | --- | --- |
| Configuration | `~/.config/mlxd/` | `MLXD_CONFIG_DIR` |
| State, socket, and metrics | `~/.local/state/mlxd/` | `MLXD_STATE_DIR` |
| Supervisor and server logs | `~/Library/Logs/mlxd/` | `MLXD_LOG_DIR` |

`mlxd` creates state and log directories with mode `0700` and runtime files
with mode `0600`. Keep model weights, credentials, state, databases, and logs
out of Git.

## How the pieces fit

Each server definition owns a stable client endpoint. When it starts, `mlxd`
allocates a private upstream port, launches the configured runtime through its
adapter, and waits for its health and model APIs. A local metrics proxy keeps
the stable client port in place, forwards traffic to the upstream process, and
records request observations. `mlxctl` talks to the supervisor over a private
Unix socket rather than controlling child processes directly.

The supervisor exits after 15 idle seconds by default when no server or control
client remains active. A later CLI command can activate it again through
launchd.

For the exact filesystem and control interfaces shared with deployment tooling,
see [`docs/deployment-contract.md`](docs/deployment-contract.md). For the
server APIs used by readiness and metrics, see
[`docs/research/mlx-server-apis.md`](docs/research/mlx-server-apis.md).

## Develop mlxctl

Run all commands from `tools/mlxctl/`:

```sh
uv run python -m unittest discover -s tests
uvx ruff check .
uvx ruff format --check .
uv build
```

The package uses a `src/` layout and keeps its resolved runtime dependencies in
`uv.lock`. Contributions should include tests at the behavioral boundary they
change.

## License

`mlxctl` is available under the [MIT License](LICENSE).
