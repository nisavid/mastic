---
name: mlxctl
description: A quiet control room for dependable local MLX inference.
colors:
  background: "#090d0c"
  surface: "#111715"
  surface-raised: "#171f1d"
  border: "#2b3733"
  text: "#e7ecea"
  text-muted: "#8a9691"
  accent: "#89e2a2"
  information: "#98c8ff"
  warning: "#f4cd73"
  error: "#ff8b82"
typography:
  title:
    fontFamily: "SFMono-Regular, SF Mono, ui-monospace, monospace"
    fontSize: "1.25rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  body:
    fontFamily: "SFMono-Regular, SF Mono, ui-monospace, monospace"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "SFMono-Regular, SF Mono, ui-monospace, monospace"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: 1.25
rounded:
  sm: "4px"
  md: "6px"
  lg: "8px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
components:
  button-primary:
    backgroundColor: "{colors.text}"
    textColor: "{colors.background}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  resource-row:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  command-palette:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.lg}"
    padding: "12px"
---

# Design System: mlxctl

## Overview

**Creative North Star: "The Quiet Control Room"**

mlxctl is a calm, dense operational surface used while real work is happening.
It favors a stable shell, restrained tonal layers, exact resource names, and
direct actions. It is dark because its primary users work in terminals beside
editors and agent tools for extended sessions—not because inference software
needs cyberpunk decoration.

The operations console is the persistent frame. Intent-first guidance takes
over first-run and empty states. Resource topology appears when relationships or
failures need explanation. The interface remains useful in a narrow terminal by
collapsing the inspector and navigation before hiding any capability.

**Key Characteristics:**

- Dense but never cryptic
- Flat and quiet at rest
- Evidence and remediation one action away
- Color reserved for selection, status, and consequence
- Familiar terminal controls with explicit labels and help

## Colors

Near-black green neutrals keep long sessions quiet; one soft green accent marks
selection and readiness, while information, warning, and error hues carry only
their semantic meanings.

### Primary

- **Operational Green:** Current selection, primary focus, readiness, and the
  one most useful safe action.

### Secondary

- **Information Blue:** Links, focus, pinned state, topology edges, and neutral
  explanatory actions.
- **Caution Amber:** Blocked, stopped-with-attention, conflicting evidence, and
  consequences that require review.
- **Failure Coral:** Failed operations, destructive actions, and errors only.

### Neutral

- **Night Console:** The uninterrupted application background.
- **Workbench Surface:** Navigation, inspector, tables, and grouped controls.
- **Raised Surface:** Command palette, focused workbench, and temporary overlays.
- **Quiet Ink:** Supporting text that remains readable rather than decorative.

**The Semantic Color Rule.** Accent and state colors never decorate inactive
surfaces. Every colored mark must communicate selection, status, relationship,
focus, or consequence, and must have a text or symbol equivalent.

## Typography

**Display Font:** SF Mono with the platform monospace fallback
**Body Font:** SF Mono with the platform monospace fallback
**Label/Mono Font:** SF Mono with the platform monospace fallback

**Character:** One crisp monospace vocabulary keeps resource identity, commands,
data, and prose visually coherent. Hierarchy comes from weight, spacing, and
placement—not oversized headings or decorative type.

### Hierarchy

- **Title** (700, 1.25rem, 1.2): Screen and selected-resource titles.
- **Body** (400, 0.875rem, 1.5): Explanations, state summaries, and guidance;
  prose remains under 72 characters where layout permits.
- **Label** (600, 0.75rem, 1.25): Controls, table headings, and compact metadata.

**The Identity Rule.** Exact revisions, routes, operation IDs, and commands use
the same type family but gain distinction through semantic color and placement;
they are never made tiny to reduce visual weight.

## Elevation

The TUI is flat by default. Depth comes from tonal layering and single-cell
boundaries, not shadows. Temporary overlays such as the command palette may use
the raised surface, but ordinary resources remain rows, tables, and panes rather
than floating cards.

**The Flat Control Room Rule.** A panel earns another surface layer only when it
changes focus, ownership, or interaction mode.

## Components

### Buttons

- **Shape:** Compact, gently squared controls; no pill-shaped action buttons.
- **Primary:** Light ink on the night background inversion, reserved for the one
  safe next action in a focused context.
- **Hover / Focus:** Visible information-blue focus, state label, and immediate
  150–200 ms response where the terminal supports animation.
- **Secondary / Ghost:** Surface-toned contextual actions; destructive actions
  use failure text and always state the resource affected.

### Chips

- **Style:** Compact text-plus-symbol state labels such as `● READY`,
  `■ STOPPED`, or `! BLOCKED`.
- **State:** Color supplements the authoritative word and symbol; chips never
  become unlabeled dots.

### Cards / Containers

- **Corner Style:** Small radii in rendered prototypes; terminal panes use
  conventional Textual borders.
- **Background:** Tonal surfaces distinguish navigation, workspace, and
  inspector.
- **Shadow Strategy:** None.
- **Border:** Single subtle separators; alerts use a full border or tinted
  surface, never a decorative side stripe.
- **Internal Padding:** One compact spacing step for rows, two for focused panes.

### Inputs / Fields

- **Style:** Native Textual controls on the workbench surface with an explicit
  label and current value.
- **Focus:** Information-blue focus plus cursor or selection change.
- **Error / Disabled:** Error or muted styling plus plain-language reason and
  valid next action.

### Navigation

Persistent resource navigation anchors wide terminals. Narrow terminals use a
compact switcher and command palette without removing operations. Active state
combines text weight, a marker, and semantic color. `?` explains the current
screen and `Ctrl+P` opens the complete operation palette.

### Resource Row

The canonical dense unit shows identity first, then desired/observed state,
evidence or activity, and one contextual action. Opening it reveals the
inspector; it never expands into nested cards.

## Do's and Don'ts

### Do:

- **Do** keep the operations console stable while detail and intent flows change
  in the workspace.
- **Do** use intent-first first-run and empty states, then reveal exact revisions,
  fit, trust, and resource effects before confirmation.
- **Do** use topology only when model, runtime, service, run, and Gateway
  relationships materially explain state or failure.
- **Do** label every state independently of color and keep every action available
  from both context and the command palette.
- **Do** collapse panes for narrow terminals before reducing functionality.

### Don't:

- **Don't** recreate the existing feature-poor dashboard: dense state without
  enough action, ambiguous resource language, weak hierarchy, and no satisfying
  path forward.
- **Don't** build a raw daemon console that exposes ports, process trivia, and
  configuration files instead of the user's runtimes, models, services, and
  clients.
- **Don't** use generic dashboard card soup, decorative terminal nostalgia,
  neon cyberpunk, or unfamiliar controls invented for visual novelty.
- **Don't** build a setup wizard that hides exact revisions, trust, resource
  cost, or the plan it is about to apply.
- **Don't** use colored side stripes, decorative animation, unlabeled status
  dots, oversized headings, deep nesting, or modal dialogs as the first answer.
