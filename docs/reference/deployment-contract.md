# MASTIC deployment contract v1

This reference defines the current boundary between MASTIC and a deployment
owner such as chezmoi. The deployment owner installs the package and may apply
the inactive LaunchAgent. MASTIC owns desired state, Runtime Installations,
models, services, Application Configuration Target state, operational state,
and lifecycle.

## Installed entry points

| Entry point | Contract |
| --- | --- |
| `mastic` | Human and machine CLI; invoking it with no arguments opens the TUI. |
| `masticd` | Foreground per-user Supervisor entry point for direct invocation and help. |
| `python -m mastic.entrypoints daemon` | Private stable target used by MASTIC's generated LaunchAgent. |

The supported release entry point is `bootstrap-mastic.zsh`. Its embedded digest
verifies the bytes of one exact macOS/arm64 closure containing MASTIC, Python,
`uv`, the MASTIC dependency wheelhouse, and the selected application artifacts.
The digest does not authenticate the publisher; operators must obtain the script
and its digest manifest through a trusted release channel. Use
`--dry-run` for host validation without network or mutation, or `--artifact-dir`
with the exact closure for offline installation. Do not install separate MLX
runtimes globally for mastic: `mastic runtime install` owns isolated exact Runtime
Installations.

## LaunchAgent

- Label: `io.nisavid.masticd`
- Path: `~/Library/LaunchAgents/io.nisavid.masticd.plist`
- `RunAtLoad`: `false`
- `KeepAlive`: `false`
- `Umask`: `0077`
- stdout and stderr: `supervisor.log` in the effective log directory;
  `~/Library/Logs/mastic/supervisor.log` by default, with the directory
  overridden by `MASTIC_LOG_DIR`

MASTIC generates a literal executable path and the private module target in
`ProgramArguments`. The LaunchAgent is registered but inactive. A mutation
that needs the Supervisor registers or kickstarts it and waits for the private
control socket. Read-only operations never activate it.

`mastic supervisor stop` drains all Service Runs, stops the Gateway, replies to
the caller, closes the control service, and lets `masticd` exit. `launchd` does not
restart it.

## Owned paths

| Purpose | Default | Environment override |
| --- | --- | --- |
| Desired state | `~/.config/mastic/config.toml` | `MASTIC_CONFIG_DIR` |
| SQLite state and socket | `~/.local/state/mastic/` | `MASTIC_STATE_DIR` |
| Setup/removal and composition coordination | `~/.local/state/.mastic-locks/{setup-removal,composition-removal}.lock` | Fixed per-user namespace; product-root and XDG overrides do not change it |
| Application Configuration Target ownership | `~/.local/state/mastic/application-targets/` | `MASTIC_STATE_DIR` |
| Runtime Installations | `~/.local/share/mastic/runtimes/` | `MASTIC_DATA_DIR` |
| Verified bootstrap `uv` | `~/.local/share/mastic/bootstrap-uv/uv` | `MASTIC_DATA_DIR` |
| Verified bootstrap Python | `~/.local/share/mastic/bootstrap-python/` | `MASTIC_DATA_DIR` |
| Exact application artifact cache | `~/.local/share/mastic/bootstrap-artifacts/` | `MASTIC_DATA_DIR` |
| Owned Hindsight API tool | `~/.local/share/mastic/application-tools/` | `MASTIC_DATA_DIR` |
| Owned application launchers | `~/.local/share/mastic/application-bin/` | `MASTIC_DATA_DIR` |
| Logs | `~/Library/Logs/mastic/` | `MASTIC_LOG_DIR` |
| External `uv` override | Used only when explicitly selected | `MASTIC_UV_EXECUTABLE` |

When a `MASTIC_*_DIR` override is absent, config, state, and data first follow
`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, and `XDG_DATA_HOME` respectively. Their
fallbacks are the default paths shown above. Log placement does not use an XDG
root.

Directories are user-owned, non-symlink directories with mode `0700`.
Supervisor and service logs are user-owned regular files with mode `0600`.

The fixed per-user coordination directory is independent of the configured
config, state, data, and log roots. Each lock is a user-owned regular file with
mode `0600`. The directory and lock files intentionally remain after those
product roots are removed, so concurrent composition, confirmed setup, and
removal transactions continue to share stable exclusion boundaries.

The Hugging Face cache remains shared and is not product-owned. MASTIC manages
its bytes through model-cache operations and reference checks.

Bootstrap leaves an existing external `uv`, Codex, or Hindsight installation
untouched. Guided setup adopts an exact matching application nonmutatively. A
different application at the conventional path is a visible conflict; MASTIC
does not replace it implicitly. Removal previews the product-owned roots and
retains shared resources and every external application. External-application
removal requires a separate exact Removal Plan. Ordinary removal refuses to
remove application-target fields when their current content no longer matches
MASTIC's ownership evidence.

## Setup outcomes and performance evidence

Setup exposes two independent serialized axes: `completion` is `partial` or
`complete`, and `readiness` is `pending`, `unverified`, `ready`, or `degraded`.
Interrupted work persists exact step fingerprints and returns the current axes
and failed step;
the next preview shows the same evidence-derived state before resume.

The repository currently publishes the Phase 1 performance policy as
`provisional`. It is bound to the exact Plan, application versions, macOS major,
and host profile. For a selected Application Configuration Target, it cannot
produce performance-derived `ready` or `degraded` readiness until measurements
from a supported 48 GiB-or-larger host promote that exact policy to `validated`.
A correct canary remains `unverified` while the policy is provisional. An
explicitly skipped required canary also remains `unverified`. Neither condition
alone makes `mastic check` fail. With no selected target, the independent exact
Gateway verification can still produce `ready` when its saved response digest
matches the contract.

`status`, `check`, `doctor`, and the TUI reconstruct these outcomes after a
restart from the content-free Plan record and evidence. They re-inspect every
selected Application Configuration Target before reporting it. Unhealthy or
unobservable target state forces that target and the combined readiness to
`unverified`; unlike provisional or explicitly skipped canary evidence, that
current target issue also makes `check` fail its health policy.

## Ownership boundary

The deployment owner may:

- install or update the MASTIC package from an immutable MASTIC revision;
- render and register the inactive LaunchAgent generated by the current
  production composition;
- invoke `mastic setup` or other public CLI operations after installation;
- verify package entry points and inactive launchd registration.

The deployment owner must not author or mutate:

- `config.toml` service, model, runtime, Gateway, or application-target tables;
- isolated Runtime Installation directories;
- model cache contents;
- Codex provider or model-catalog settings and Hindsight settings that MASTIC owns;
- SQLite operational state or the control socket;
- live Service Runs or Gateway state outside the `mastic` interface.

This keeps `chezmoi apply` idempotent and prevents two systems from competing
over the same desired state.

## Control protocol

`mastic` and `masticd` use a private, versioned, length-prefixed JSON protocol on
`masticd.sock` in the effective state directory. That directory is
`MASTIC_STATE_DIR` when set; otherwise it is `mastic` under `XDG_STATE_HOME`,
defaulting to `~/.local/state/mastic`. The shipped client opens a connection,
negotiates v1, carries one correlated operation, streams bounded progress
frames, receives one result or stable error, and closes the connection. The
server can accept multiple correlated requests on one negotiated connection,
including a cancellation request while an operation is active. Frames are
size-bounded. The socket is user-owned with mode `0600`, and `masticd` verifies
the connecting process has the same user identity through peer credentials
before accepting protocol work. The socket is loopback-equivalent local IPC
and is never exposed on a network interface.

Physical runtime and model operations receive durable operation identities.
Public v1 supports listing and inspecting those operations. It does not claim
resume or cancellation semantics that an owner cannot guarantee.

## Gateway and runtime processes

The Gateway binds to a literal loopback address, defaulting to
`127.0.0.1:8766`, and exposes OpenAI-compatible routes below `/v1`. Each
Inference Service process binds to a private dynamic loopback port. The public
request `model` field selects the stable service route.

All managed profile routes and ordinary `/v1` routes require the private bearer
credential stored as `gateway.token` in the effective state directory. That
directory is `MASTIC_STATE_DIR` when set; otherwise it is `mastic` under
`XDG_STATE_HOME`, defaulting to `~/.local/state/mastic`. MASTIC supplies the
credential to owned Application Configuration Targets. It must be a regular,
non-symlink file owned by the current user with mode `0600`; MASTIC validates
those properties before reading it. Missing and invalid credentials both return
`401` with `WWW-Authenticate: Bearer`; the Gateway never forwards the credential
to an Inference Service.

Several services may run concurrently. The Gateway does not start a stopped
service in response to traffic. Runtime processes are launched from exact
owned installations with validated argv, capabilities, cached model identity,
and revision/runtime-scoped trust grants.

Chat output and unadapted streams are forwarded incrementally. Adapted non-SSE
responses and individual adapted SSE frames are buffered only within configured
response-adaptation limits. The initial upstream response and idle time between
upstream reads have a 30-second default timeout; an admitted stream has no
separate maximum lifetime while reads continue within that bound. Before
streaming begins, an upstream timeout or failure returns `502` with
`upstream_unavailable`. A late-adapted Responses SSE failure emits a Responses
protocol error; an ordinary unadapted stream closes without a second HTTP error
body. Cleanup still releases request activity.

Gateway telemetry retains bounded, content-free admission, completion,
active-request, pressure, and service-correlation metrics. It does not retain a
per-request journal, prompt or response content, authorization headers, or token
payloads.

## Resource admission and pressure

Per-service admission is bounded and returns a stable retryable response at the
concurrency limit. Critical memory pressure blocks new starts and sheds new
Gateway work while admitted requests continue. The in-flight bound limits
concurrency, not lifetime: automatic recovery adds no total-stream deadline and
does not force-close a busy run.

The Supervisor may stop least-recently-used idle Service Runs until pressure
recovers, but never stops a pinned or busy service automatically. If only pinned
or busy services remain, MASTIC continues shedding new work and presents an
ordered operator stop sequence. An explicit service stop terminates that run.
An explicit Supervisor stop allows the configured Gateway drain interval—10
seconds by default—before terminating remaining runs. After streaming begins,
an ordinary client observes transport closure; an adapted Responses SSE stream
may receive its protocol error.

## Deployment validation

The installed control surface, explicit lifecycle, Application Configuration
Targets, bounded native canaries, durable setup outcome, and clean shutdown can
be verified on the target Mac by following
[Validate a target-Mac deployment](../how-to/validate-deployment.md).

The native request procedures are implemented: Codex runs ephemerally through
its owned configuration, and Hindsight runs against a disposable local API and
database. The current performance profile is still `provisional`, so successful
canaries remain `unverified` until matching clean-host measurements validate
the profile. Operators must not promote that result by hand.

A standalone `mastic application-target test` returns the same bounded native
result on demand but does not rewrite setup evidence. Confirmed setup owns
durable `completion` and `readiness`.
