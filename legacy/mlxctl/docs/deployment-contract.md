# mlxctl Deployment Contract v1

The interface between `mlxctl` (this repo) and `nisavid/dotfiles` (the
personal deployment layer). The dotfiles agent implements this contract
with personal values: the LaunchAgent plist, chezmoi config data, and
install hook.

## Entry Points

| Name     | Purpose                          | Installed by        |
|----------|----------------------------------|---------------------|
| `mlxctl` | CLI/TUI — status, start, stop, models, metrics, dashboard | `pyproject.toml` `console_scripts` |
| `mlxd`   | Supervisor daemon — launchd-managed process that manages MLX server lifecycle | `pyproject.toml` `console_scripts` |

Both are installed from `tools/mlxctl/` via `uv tool install` or `pip install`
as console script entry points. The plist's `ProgramArguments` invokes `mlxd`.

## Plist Label

```
io.nisavid.mlxd
```

Reverse-DNS of the personal domain `nisavid.io`. The LaunchAgent plist
file is `~/Library/LaunchAgents/io.nisavid.mlxd.plist`.

## Directory Layout

| Purpose | Default path                      | Env override            |
|---------|-----------------------------------|-------------------------|
| Config  | `~/.config/mlxd/`                 | `MLXD_CONFIG_DIR`       |
| State   | `~/.local/state/mlxd/`            | `MLXD_STATE_DIR`        |
| Logs    | `~/Library/Logs/mlxd/`            | `MLXD_LOG_DIR`          |

Follows XDG Base Directory Specification for config and state; macOS
convention for logs.

### Config directory (`~/.config/mlxd/`)

`config.toml` is the versioned configuration surface. Schema version 1 contains
daemon timeouts and sampling cadence, metrics retention, Model Alias to Model
Reference mappings, and named Server Definitions. Each Server Definition names
its Server Type, Model Alias, loopback Client Endpoint, environment, and
server-type-specific options. Unknown fields, unsupported server types,
non-loopback addresses, invalid ports, and duplicate Client Endpoints are
rejected before the Supervisor starts.

Client Endpoint ports are declared statically in `config.toml`. When a Server
Definition starts, the Supervisor allocates a private ephemeral Upstream
Endpoint on `127.0.0.1`; the metrics proxy retains the configured Client
Endpoint while forwarding requests to that process.

### State directory (`~/.local/state/mlxd/`)

Runtime state: supervisor lifecycle state, the `mlxd.sock` control socket,
and the `metrics.db` SQLite database. The directory is mode `0700`; the
socket, database, and lifecycle state files are mode `0600`.

## Control Protocol v1

`mlxctl` connects to `mlxd.sock` using newline-delimited JSON over a Unix
socket. Each connection carries one versioned request and one response.
The protocol supports `start`, `stop`, `status`, `models`, and `metrics`
commands. `mlxd` rejects malformed, unsupported, unknown,
and oversized requests without exposing machine-local paths or tracebacks.

### Log directory (`~/Library/Logs/mlxd/`)

`mlxd.log` (supervisor stdout/stderr) and one `<server-name>.log` file for each
Server Definition.

## Environment Variables

The plist sets these in `EnvironmentVariables`:

| Variable              | Purpose                                          |
|-----------------------|--------------------------------------------------|
| `PATH`                | Must include `~/.local/bin/` where `uv tool` installs `mlx_lm`, `optiq`, and other MLX tool entry points |
| `MLXD_CONFIG_DIR`     | Override config directory (defaults to `~/.config/mlxd/`) |
| `MLXD_STATE_DIR`      | Override state directory (defaults to `~/.local/state/mlxd/`) |
| `MLXD_LOG_DIR`        | Override log directory (defaults to `~/Library/Logs/mlxd/`) |

## LaunchAgent Behavior

- `RunAtLoad=false` — the registered service does not start on login.
- `KeepAlive=false` — launchd does not auto-restart the supervisor.
- The deployment layer registers the plist in the user's launchd domain.
- When the control socket is absent, `mlxctl` runs `launchctl kickstart`
  for `io.nisavid.mlxd` and waits boundedly for the socket.
- The user starts inference explicitly via `mlxctl start <server>`; the
  daemon exits after its idle grace once no managed servers remain active.

## Follow-up (Not in v1)

These graduate as separate contract revisions once their requirements resolve:

- **Log level** — `MLXD_LOG_LEVEL` env var.
- **Full env-var set** — any additional variables the supervisor needs.
