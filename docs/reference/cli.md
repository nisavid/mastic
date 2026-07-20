# CLI reference

This reference describes MASTIC's installed entry points, top-level command
groups, output formats, and exit behavior. Use `--help` at any level for the
complete arguments and options of one operation.

## Entry points

| Command | Behavior |
| --- | --- |
| `mastic` | Human and automation interface. With no arguments, opens the TUI. |
| `masticd` | Runs the foreground per-user Supervisor directly; the generated LaunchAgent uses the private module entry point. |

## Top-level commands

| Command | Behavior |
| --- | --- |
| `mastic setup` | Previews and applies a complete local-inference Plan, resuming steps whose persisted terminal evidence matches. |
| `mastic remove` | Previews and removes MASTIC-owned services, product state, and integrations; every external application and the external coordination locks remain. |
| `mastic status` | Reports runtime state plus durable and current per-target setup outcomes without starting the system. |
| `mastic check` | Applies operational and current application-target health policy to the status view. |
| `mastic doctor` | Diagnoses application-target health and context alongside configuration, lifecycle, routing, and service failures. |
| `mastic logs` | Reads bounded MASTIC logs, optionally for one resource. |
| `mastic metrics` | Reads locally recorded operational metrics. |
| `mastic tui` | Opens the interactive operations console. |
| `mastic supervisor` | Inspects and controls the per-user Supervisor. |
| `mastic gateway` | Inspects and configures the stable loopback Gateway. |
| `mastic runtime` | Discovers and manages exact Runtime Installations. |
| `mastic model` | Searches, inspects, installs, verifies, and trusts exact Model Revisions. |
| `mastic service` | Creates and controls named Inference Services. |
| `mastic operation` | Inspects durable physical operations and their events. |
| `mastic application-target` | Configures, inspects, and runs bounded native Codex and Hindsight checks. |
| `mastic config` | Inspects, edits, validates, diffs, and restores desired configuration. |

## Output formats

Operations that support structured output expose these options:

| Option | Output |
| --- | --- |
| `--json` | One deterministic, versioned JSON document. |
| `--json-lines` | Versioned NDJSON progress and terminal events. |
| `--plain` | Human-oriented output without terminal decoration. |

The default human output is TTY-aware. Machine consumers should select JSON or
NDJSON explicitly and use the returned `schema_version` rather than parsing
human prose.

## Mutation behavior

Mutating commands show their resolved preview before confirmation. Interactive
callers confirm that preview at the prompt. Automation supplies `--yes` only
after providing all required values and reviewing the same preview shape.

Read-only commands never start `masticd`. A mutation may start it when the
operation requires the Supervisor and reports that action.

## Setup outcome fields

`setup`, `status`, `check`, `doctor`, and the TUI expose installation completion
separately from application readiness:

| Field | Values and meaning |
| --- | --- |
| `completion` | `partial` while planned steps lack matching terminal evidence; `complete` when every step has matching `complete` or explicit `skipped` evidence. |
| `readiness` | `pending`, `unverified`, `degraded`, or `ready`. |
| `application_target_readiness` | One readiness value for each selected Application Configuration Target. |

Setup runs the selected Codex and Hindsight application-native canaries as
resumable terminal steps and stores content-free exact-contract, duration, and
digest evidence. A correct canary under a provisional policy and an explicitly
skipped required canary both remain `unverified`; neither condition alone makes
`check` fail. Setup evidence preserves whether the canary completed or was
skipped. `status`, `check`, and `doctor` re-observe current target ownership
before reporting the durable outcome. Drift or missing owned state can downgrade
retained evidence to `unverified` without deleting it; `check` returns nonzero
for that current health-policy failure.

When targets are selected, overall readiness is the first state present in
this most-to-least dominant order: `pending`, `unverified`, `degraded`,
`ready`. With no selected target, the exact Gateway verification step produces
`pending` while evidence is absent, `ready` when its saved response digest
matches the contract, and `unverified` when saved evidence is present but
invalid.

## Application Configuration Target commands

| Command | Behavior |
| --- | --- |
| `mastic application-target list` | Lists MASTIC-owned targets. |
| `mastic application-target inspect TARGET` | Reports owned settings and current health without mutating them. |
| `mastic application-target configure TARGET` | Previews and applies owned Codex or Hindsight settings. |
| `mastic application-target test TARGET [--profile PROFILE]` | Requires a healthy target and invokes its bounded native path; the canonical v1 canary profile is `coding` for Codex and `retain` for Hindsight. |
| `mastic application-target remove TARGET` | Previews and removes only fields whose recorded ownership and current digest still match MASTIC's journal. |

A standalone `application-target test` returns native exact-contract evidence
on demand but does not advance the durable setup outcome. Confirmed setup owns
that evidence through its resumable canary steps.

## Exit behavior

- Observation commands exit zero when the observation succeeds, even when a
  reported resource is stopped or degraded.
- `mastic check` and `mastic service check` exit nonzero when their health
  policy fails.
- Invalid invocation, failed observation, rejected confirmation, and failed
  mutation exit nonzero.

## Help and discovery

```sh
mastic --help
mastic COMMAND --help
mastic RESOURCE COMMAND --help
```

Resource commands accept stable names or identities described by their help.
Use the corresponding `list`, `available`, or `search` operation to discover
valid values before scripting a mutation.
