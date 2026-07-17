# Product

## Register

product

## Users

mlxctl serves prospective, new, and experienced Apple-silicon Mac users who
want dependable local inference without manually coordinating Python
environments, model caches, ports, daemon state, or client configuration. They
work primarily in a terminal, often while coding or operating an agentic tool,
and need to understand the machine before deciding what to run.

## Product Purpose

mlxctl turns compatible MLX runtimes and exact model revisions into named local
Inference Services behind one stable loopback Gateway. Success means a new user
can reach a verified request through guided setup, while an experienced user
can inspect, automate, diagnose, update, stop, and remove every managed resource
through equivalent CLI and TUI operations without editing configuration files
or reaching for runtime-specific tools.

## Brand Personality

Calm, capable, and considerate. The interface should feel technically exact
without becoming clinical, make consequential state changes feel controlled,
and reserve delight for useful moments of clarity: a safe plan, a well-explained
failure, a fast path to the next action, or a service becoming ready.

## Anti-references

- The existing feature-poor dashboard: dense state without enough action,
  ambiguous resource language, weak hierarchy, and no satisfying path forward.
- A raw daemon console that exposes ports, process trivia, and configuration
  files instead of the user's runtimes, models, services, and clients.
- Generic dashboard card soup, decorative terminal nostalgia, neon cyberpunk,
  or unfamiliar controls invented for visual novelty.
- A setup wizard that hides exact revisions, trust, resource cost, or the plan
  it is about to apply.

## Design Principles

- **One product, two surfaces.** CLI and TUI expose the same operation catalogue,
  vocabulary, evidence, and next actions.
- **Intent first, evidence close.** Lead with what the user can accomplish, then
  keep exact resource identities and observed evidence one step away.
- **Desired and observed stay distinct.** Never blur a configured service, its
  current run, its private upstream, or its stable Gateway route.
- **Every state teaches.** Help, loading, empty, blocked, failure, and completion
  states explain what happened and offer a valid next action.
- **Consequences stay visible.** Preview resource, trust, client, pressure, and
  removal effects before mutation; make long work resumable and inspectable.

## Accessibility & Inclusion

All operations are keyboard-accessible. Status never relies on color alone;
focus is visible; compact and narrow terminals retain the complete operation
set; plain and no-color output remain useful; motion is brief, state-driven,
and removable. Text and state colors meet WCAG 2.2 AA contrast targets where
terminal palette control permits, with clear labels as the authoritative cue.
