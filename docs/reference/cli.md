# CLI reference

This reference describes MASTIC's installed entry points, top-level command
groups, output formats, and exit behavior. Use `--help` at any level for the
complete arguments and options of one operation.

## Entry points

| Command | Behavior |
| --- | --- |
| `mastic` | Human and automation interface. With no arguments, opens the TUI. |
| `masticd` | Runs the foreground per-user Supervisor used by launchd. |

## Top-level commands

| Command | Behavior |
| --- | --- |
| `mastic setup` | Plans and creates a complete local inference service. |
| `mastic remove` | Previews and removes MASTIC-owned services, state, and integrations. |
| `mastic status` | Reports the whole local system without starting it. |
| `mastic check` | Evaluates health policy across the Supervisor, Gateway, and services. |
| `mastic doctor` | Diagnoses configuration, lifecycle, routing, and service failures. |
| `mastic logs` | Reads bounded MASTIC logs, optionally for one resource. |
| `mastic metrics` | Reads locally recorded operational metrics. |
| `mastic tui` | Opens the interactive operations console. |
| `mastic supervisor` | Inspects and controls the per-user Supervisor. |
| `mastic gateway` | Inspects and configures the stable loopback Gateway. |
| `mastic runtime` | Discovers and manages exact Runtime Installations. |
| `mastic model` | Searches, inspects, installs, verifies, and trusts exact Model Revisions. |
| `mastic service` | Creates and controls named Inference Services. |
| `mastic operation` | Inspects durable physical operations and their events. |
| `mastic client` | Configures and verifies Codex and Hindsight integrations. |
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

Mutating commands preview their complete resolved plan before confirmation.
Interactive callers confirm that plan at the prompt. Automation supplies
`--yes` only after providing all required values and reviewing the same plan
shape.

Read-only commands never start `masticd`. A mutation may start it when the
operation requires the Supervisor and reports that action.

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
