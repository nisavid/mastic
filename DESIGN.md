---
name: MASTIC
description: A quiet mineral instrument for host-tailored local inference.
colors:
  signal-current: "#a6f4c5"
  rock-void: "#090d0c"
  rock-deep: "#0c1210"
  rock-raised: "#101916"
  rock-panel: "#111a17"
  rock-control: "#111715"
  rock-hover: "#171f1d"
  metal-boundary: "#2b3733"
  metal-emphasis: "#2f5144"
  text-primary: "#e7ecea"
  text-secondary: "#b6c5bf"
  text-muted: "#8a9691"
spacing:
  compact: "1"
  standard: "2"
components:
  topbar:
    backgroundColor: "{colors.rock-raised}"
    textColor: "{colors.signal-current}"
    padding: "1 2"
    height: "3"
  machine-state:
    backgroundColor: "{colors.rock-raised}"
    textColor: "{colors.text-secondary}"
    padding: "0 2"
  navigation-item:
    backgroundColor: "transparent"
    textColor: "{colors.text-muted}"
    height: "3"
  navigation-item-focus:
    backgroundColor: "{colors.rock-hover}"
    textColor: "{colors.text-primary}"
    height: "3"
  workbench-panel:
    backgroundColor: "{colors.rock-panel}"
    textColor: "{colors.text-primary}"
    padding: "1"
  operation-form:
    backgroundColor: "{colors.rock-control}"
    textColor: "{colors.text-primary}"
    padding: "1"
  primary-action:
    backgroundColor: "{colors.signal-current}"
    textColor: "{colors.rock-void}"
---

# Design System: MASTIC

## Overview

**Creative North Star: "The Mineral Instrument"**

MASTIC is a precise operational instrument composed from believable natural
materials. Rock carries structure, metal carries boundaries and control, and
gemstones carry compact live signals. The material language is timeless and
functional: it should feel carefully made, not themed.

The implemented interface is a terminal-native control room. It translates the
Mineral Instrument into flat rock-dark panes, restrained green-gray metal
boundaries, one observed pale signal color, exact status words, and compact
symbols. It does not pretend a terminal can render physical texture, luster, or
facets. Future richer surfaces may express those material qualities, but they
must preserve the same semantic roles and operational restraint.

The current signal green is an observed implementation token, not a permanent
brand-color decision. The confirmed longer-term direction remains a broader
gemstone signal vocabulary. Additional gem colors become normative only when a
real interface assigns and validates their semantic roles.

**Key Characteristics:**

- Structural rock surfaces, metal boundaries, and sparingly used signal color
- One integrated workbench rather than a grid of floating cards
- Dense operational clarity with exact evidence and next actions close at hand
- Terminal-owned typography, keyboard-first control, and responsive pane removal
- Material richness scaled honestly to the rendering surface

## Colors

The implemented palette is a near-black mineral field with green-gray
boundaries, high-contrast mineral text, and one provisional pale signal color.
The frontmatter records every color currently used by the Textual interface.

### Primary

- **Current Signal Green** (`signal-current`): marks compact product identity,
  view titles, primary actions, focus treatment, and live state. It is
  implementation truth but remains provisional as a durable brand token.

### Secondary

- **Structural Metal** (`metal-boundary`, `metal-emphasis`): separates the
  navigation, workbench, inspector, top bar, forms, and emphasized panels.
  Boundaries establish structure; they are never decorative outlines around
  every piece of content.

### Tertiary

The confirmed direction includes sapphire, ruby, topaz, tourmaline, emerald,
and tanzanite signals for future semantic roles. None is tokenized yet because
the current interface has not implemented or validated those assignments.

### Neutral

- **Structural Rock** (`rock-void`, `rock-deep`, `rock-raised`, `rock-panel`,
  `rock-control`, `rock-hover`): builds depth through adjacent dark tones rather
  than shadows.
- **Mineral Text** (`text-primary`, `text-secondary`, `text-muted`): separates
  authoritative content, supporting machine state, and quiet metadata without
  relying on opacity or tiny type.

**The Material Role Rule.** Rock provides structure, metal defines boundaries,
and gemstones signal state. Never exchange those roles merely for visual
variety.

**The Implemented-Token Rule.** Frontmatter contains colors the product
actually renders. Directional gem colors stay out until an implemented surface
gives them exact values and meanings.

**The Rare Light Rule.** Signal color is compact and limited to product
identity, view titles, action, focus, or live state. Every signal has an
authoritative word or symbol equivalent.

## Typography

**Display Font:** Not defined in the terminal interface

**Body Font:** The user's terminal monospace

**Label/Mono Font:** The user's terminal monospace

**Character:** MASTIC respects the operator's terminal typography instead of
bundling a competing face. Hierarchy comes from bold weight, spacing, case,
alignment, symbols, and pane position. Guidance reads as composed prose;
commands, identities, metrics, and evidence remain recognizably structured.

### Hierarchy

- **Headline** (terminal-owned monospace, bold, inherited terminal size): The
  current workspace or operation name, rendered with the current signal color.
- **Title** (terminal-owned monospace, regular or bold, inherited terminal
  size): Resource names, selected targets, and panel labels; bold only when it
  clarifies the active context.
- **Body** (terminal-owned monospace, regular, inherited terminal size):
  Explanations, evidence, previews, results, and next actions, wrapped by the
  available terminal width.
- **Label** (terminal-owned monospace, bold, inherited terminal size):
  Operation parameters and compact metadata, with muted text for help.
- **Machine state** (terminal-owned monospace, regular, inherited terminal
  size): A compact two-line strip that pairs symbols with complete state words
  and exact target names.

**The Terminal Ownership Rule.** Nerd-font glyphs may enrich a capable terminal
but can never be required for identity, navigation, state, or action.

**The Evidence Type Rule.** Monospace distinguishes exact evidence; it does not
make all prose feel like a daemon log.

## Layout

The interface is a vertically stacked instrument: a three-cell top bar, a
machine-state strip, a flexible horizontal shell, and a footer. At 120 columns
or wider, the shell contains a 41-cell resource navigation pane, a flexible
scrolling workbench, and a 30-cell inspector. Below 120 columns the inspector
disappears. Below 80 columns the navigation also disappears and workbench
horizontal padding tightens from two cells to one.

The workbench owns the primary reading and action flow. Its title leads into one
integrated content panel, an operation form when needed, and contextual actions.
Long-running work stays in background workers so navigation remains responsive.
Changing workspaces invalidates stale results and unresolved mutation previews.

Spacing follows a one-cell base rhythm. Two-cell horizontal padding establishes
comfortable pane edges when width permits; controls use a one-cell vertical
rhythm and full available width. Contextual action buttons use an 18-cell
minimum width so labels remain stable and scannable.

**The Capability-Preserving Collapse Rule.** Narrow layouts may remove secondary
panes, never operations. The command palette and workbench retain the complete
catalogue.

**The Workbench Rule.** Ordinary product structure belongs in rows, panes,
forms, and one integrated workbench—not nested or repeated cards.

## Elevation & Depth

The implemented system uses no shadows. Depth comes from adjacent rock tones,
full-length metal boundaries, and the softly rounded outline around the main
content panel. The navigation and inspector are structural peers of the
workbench, not floating layers above it.

Future richer surfaces may add plausible stone texture, metal luster, or gem
depth. Those effects must remain local, materially believable, and subordinate
to evidence and control.

**The Flat-at-Rest Rule.** Surfaces remain integrated and shadowless. State
changes use color, weight, and content before simulated elevation.

**The Earned Glow Rule.** Glow is permitted only for a live semantic signal on
a capable surface. Ambient decorative bloom is forbidden.

## Shapes

The form language is predominantly rectilinear and instrument-like. Full-height
solid and tall borders define major regions. Navigation buttons are borderless
rectangles that fill the pane width. The primary content panel alone uses
Textual's rounded border to soften the reading surface; operation forms return
to a solid rectangular boundary.

Symbols are compact geometric marks—diamonds, circles, loops, arrows, and
crosses—paired with words. They identify topology, lifecycle, and actions
without becoming ornamental iconography.

**The One Soft Surface Rule.** Reserve the rounded outline for the principal
content surface. Repeating it around every region would turn the instrument
into a card grid.

## Components

Frontmatter component tokens are normative for Stitch's supported
eight-property schema. The rules below and the v2 sidecar are normative for
borders, minimum dimensions, focus treatment, weight, and full interaction
states that frontmatter cannot encode.

### Top Bar

- **Structure:** Three cells high with one cell of vertical and two cells of
  horizontal padding.
- **Color:** Raised rock background, current signal text, and an emphasized
  metal bottom boundary.
- **Content:** Product identity and operating context on the left; the global
  command-palette shortcut remains visible.

### Machine-State Strip

- **Structure:** At least two cells high and directly attached to the top bar.
- **Content:** Supervisor, Gateway, pressure, active operations, completion,
  readiness, and per-target readiness.
- **State:** Words are authoritative; circles, diamonds, and crosses add
  scanability without replacing the labels.

### Resource Navigation

- **Structure:** A 41-cell pane with full-width, three-cell buttons.
- **Default:** Transparent on deep rock with muted text.
- **Hover / Focus:** Hover rock, primary text, and bold weight.
- **Behavior:** Hidden below 80 columns because the command palette preserves
  access to every operation.

### Workbench Panel

- **Structure:** Flexible width, one-cell internal padding, rounded emphasized
  metal border, and panel rock background.
- **Content:** Current state, resource rows, exact results, evidence, and next
  actions. It remains the single dominant reading surface.

### Inspector

- **Structure:** A 30-cell pane separated by a full-height metal boundary.
- **Content:** The selected service's state, model, runtime, Gateway route, and
  pressure policy.
- **Behavior:** Hidden below 120 columns; no capability depends on it.

### Action Buttons

- **Primary:** Guided setup uses the current signal background, void-rock text,
  and bold weight.
- **Contextual:** Other actions use Textual's standard button treatment with an
  18-cell minimum width and one-cell separation.
- **Mutation:** A primary review action leads to a distinct warning confirmation
  action and a cancel action. Confirmation never replaces the resolved preview.

### Operation Form

- **Structure:** Full-width solid metal boundary, control-rock background, and
  one-cell padding.
- **Labels:** Bold primary text names the parameter, surface, and requirement;
  muted help text explains accepted values and consequences.
- **Fields:** Inputs and selects fill the available width. Checkboxes and
  tri-state selects preserve explicit unchanged, yes, and no meanings.
- **Feedback:** Validation, preview, working, failure, completion, evidence, and
  next actions stay in the workbench; notifications supplement rather than
  replace durable content.

## Do's and Don'ts

### Do:

- **Do** keep exact artifacts, trust, resource cost, ownership, evidence, and
  planned mutations visible before confirmation.
- **Do** pair every state color or symbol with an exact state word.
- **Do** preserve the same operation catalogue, vocabulary, previews, and
  outcomes across CLI, TUI, and structured automation.
- **Do** use rock for structural surfaces, metal for boundaries, and signal
  color for compact action or state emphasis.
- **Do** collapse secondary panes at narrow widths while preserving the
  workbench and command palette.
- **Do** update the frontmatter and sidecar when an implemented token or
  component becomes part of the system.

### Don't:

- **Don't** promote the current signal green into a permanent brand color
  without a separate confirmed decision.
- **Don't** fabricate exact gem colors or semantic assignments before an
  interface implements and validates them.
- **Don't** expose ports, processes, configuration files, and adapter trivia as
  the primary information architecture.
- **Don't** hide exact artifacts, trust, resource cost, ownership, evidence, or
  mutations behind a generic setup wizard.
- **Don't** use generic dashboard card grids, nested cards, glassmorphism,
  decorative terminal nostalgia, neon cyberpunk, gratuitous animation, or
  unfamiliar controls invented for novelty.
- **Don't** imitate game-inventory UI with jewel overload, ornate dark frames,
  constant glow, or status spectacle.
