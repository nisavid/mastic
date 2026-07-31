# Product

<!-- impeccable:product-schema 1 -->

## Users

MASTIC serves prospective, new, and experienced Apple-silicon Mac users who
want dependable local inference without manually coordinating installers,
runtimes, model artifacts, process lifecycles, gateways, credentials, or
application configuration. They work primarily in terminals alongside coding
and memory tools. Before approving a consequential change, they need to
understand what fits their machine, what MASTIC will own, and what evidence
supports the recommendation.

## Product Purpose

MASTIC turns portable user intent into an exact, evidenced Plan for a
host-tailored inference stack. It bootstraps the required tools, manages local
MLX inference behind stable authenticated application paths, configures Codex
and Hindsight reversibly, and keeps setup, verification, recovery, updates, and
removal available through semantically equivalent CLI, TUI, and structured
automation surfaces.

Success means a new user can move from a compatible Mac to verified Codex and
Hindsight requests through a guided Plan, while an experienced user can inspect
evidence, alter intent, automate an exact Plan, reconcile drift, recover
failures, and remove only owned state without reaching for component-specific
tools.

## Positioning

MASTIC is not merely a launcher or a bundle of local inference components. It
separates portable Blueprint intent from an exact machine-bound Plan, binds
every selected subject to evidence and ownership, and exposes the same
operation catalogue through human and machine interfaces. Neighboring tools
can install or serve a model; MASTIC's distinct mechanism is making the whole
host-adapted system previewable, attributable, resumable, and reversible before
it mutates the Mac or an external application.

## Operating Context

MASTIC runs locally on an Apple-silicon Mac and is used from a terminal beside
an editor, Codex, and Hindsight. A typical session moves from machine and model
inspection through a resolved setup preview, explicit confirmation, background
Supervisor and Gateway operation, application-native verification, and later
status, diagnosis, reconciliation, update, or removal.

The CLI is the scriptable and reference surface. Invoking `mastic` without a
subcommand opens the Textual TUI, which exposes the same operations through
navigation, contextual workbenches, and a command palette. JSON and NDJSON
outputs carry deterministic structured results for automation. Read-only
operations do not start the Supervisor; mutations resolve and display an exact
preview before confirmation.

MASTIC owns its desired state, isolated Runtime Installations, service and
Gateway lifecycle, operational evidence, and the smallest complete application
configuration closures it changes. It cooperates with `launchd`, upstream
package and model publishers, shared Hugging Face cache storage, and externally
owned Codex and Hindsight installations without silently claiming their
lifecycle.

## Capabilities and Constraints

- The current development target is macOS on Apple silicon with Python 3.11 or
  newer. MASTIC is not yet a cross-platform runtime manager.
- The current runtime catalogue covers MLX-LM, MLX-VLM, and OptiQ. The current
  Application Configuration Targets are Codex and Hindsight.
- The recommended profile targets Macs with at least 48 GiB of unified memory
  and 24 GiB of free disk. Other exact selections carry only the evidence
  collected for them.
- A loopback OpenAI-compatible Gateway gives applications stable authenticated
  routes while private runtime ports and Service Runs change underneath it.
- Setup, removal, lifecycle, and owner-reconciliation mutations require an
  exact preview and explicit Plan Approval. MASTIC does not silently
  substitute components, change capacity, overwrite externally changed
  configuration, or treat incomplete evidence as validation.
- Completion, readiness, desired state, observed state, Claim Qualification,
  Operational Condition, Plan Disposition, ownership, and mutation outcome are
  distinct product concepts. Interfaces must not collapse them into one generic
  status.
- Recommended performance thresholds remain provisional until matching
  clean-host measurements validate them. A correct setup may therefore remain
  `unverified`; uncertainty is reported rather than converted into a
  compatibility promise.
- The current milestone is not a general adapter platform, remote inference
  host, multi-user service, or public redistribution channel for Python, uv,
  Codex, Hindsight, runtimes, or model artifacts.

## Brand Commitments

MASTIC stands for **Modular, Adaptive, System-Tailored Inference Connector**.
The product voice is calm, capable, and considerate. It should feel technically
exact without becoming clinical, make machine adaptation understandable rather
than magical, and reserve delight for useful clarity: a trustworthy
recommendation, an honest No Candidate result, a safe Plan, a well-explained
failure, or a target becoming active with a functional Operational Condition.

Product language uses the canonical vocabulary in `CONTEXT.md`. It names user
intent, exact subjects, evidence, ownership, outcomes, and valid next actions
instead of exposing ports, processes, configuration files, or adapter trivia as
the primary model.

## Evidence on Hand

- `README.md`, `docs/tutorials/`, `docs/how-to/`, and `docs/reference/`
  contain the current user-facing setup, operation, and deployment contracts.
- `CONTEXT.md` and `docs/adr/` contain the canonical domain language and
  system-wide decisions.
- `src/mastic/interfaces/cli.py` and `src/mastic/interfaces/tui.py` implement
  the shared human interfaces; the application catalogue and structured output
  provide the automation contract.
- `src/mastic/model_definitions/` and `src/mastic/runtime_definitions/` contain
  the current model, runtime, bundle, and lock evidence shipped by the source
  tree.
- The test suite exercises CLI, TUI, control protocol, persistence, Gateway,
  runtime, application-target, ownership, setup, and host-integration
  boundaries.
- The repository does not contain validated clean-host performance thresholds,
  customer testimonials, case studies, press, pricing, or broad compatibility
  evidence. Future product work must not fabricate them.

## Product Principles

- **Intent first, evidence close.** Start with the user's job and machine, then
  keep exact identities, validation, tradeoffs, and consequences one action
  away.
- **One product, equivalent surfaces.** CLI, TUI, and automation expose the
  same Plans, vocabulary, evidence, choices, and recovery outcomes.
- **Plan before mutation.** Save a resumable exact Plan, show ownership and
  dependencies, and require renewed review when material inputs change.
- **State stays precise.** Preserve the distinctions among desired state,
  observations, evidence, claims, assessments, lifecycle, operational health,
  completion, readiness, ownership, and mutation outcomes.
- **Every outcome teaches.** Missing Evidence, Partial Completion, Degraded or
  Nonfunctional targets, Blocked Plans, and No Candidate outcomes explain their
  evidence and offer a valid next action.

## Accessibility & Inclusion

All operations are keyboard-accessible and remain available in compact and
narrow terminals. Status never relies on color alone; focus is visible; plain,
structured, and no-color output remain useful; and motion is brief,
state-driven, removable, and compatible with reduced-motion preferences. Text
and state colors meet WCAG 2.2 AA contrast targets where the rendering surface
permits, with clear words and symbols as the authoritative cues.
