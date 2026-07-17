<!-- SEED: re-run $impeccable document once there's code to capture the actual tokens and components. -->
---
name: MASTIC
description: The Mineral Instrument for host-tailored inference.
---

# Design System: MASTIC

## Overview

**Creative North Star: "The Mineral Instrument"**

MASTIC is a precise operational instrument composed from believable natural
materials. Rock carries structure, metal carries boundaries and control, and
gemstones carry compact live signals. The material language is timeless and
functional: it should feel carefully made, not themed.

The primary user works in a terminal beside an editor or agent tool, often for
long sessions in subdued ambient light. Dark surfaces therefore use quiet rock
tones rather than generic black; light surfaces use pale stone without becoming
paper-beige. Rich web or native surfaces may render realistic texture, luster,
facets, and tightly bounded glow. CLI and TUI surfaces project the same roles
through flat terminal colors, symbols, weight, and spacing without pretending
to render materials the terminal cannot support.

Peter Zumthor's Therme Vals guides stone composition, Grand Seiko Zaratsu
finishing guides brushed-versus-polished metal contrast, and Smithsonian gem
photography guides believable color depth and facets. The system explicitly
rejects the Diablo Immortal inventory aesthetic: no loot-like jewel overload,
ornate dark frames, constant glow, or game-status spectacle.

**Key Characteristics:**

- Structural stone, precise metal, vivid gemstone signals
- Integrated workbench panels rather than floating card grids
- Dense operational clarity with exact evidence close at hand
- Material realism scaled to the rendering surface
- Ornament reserved for meaning, state, and control

## Colors

The palette is organized by material role rather than arbitrary hue ramps. All
exact values and tonal ramps remain **[to be resolved during implementation]**.

### Primary

- **Dark Rock:** Slate, basalt, and obsidian tones form dark-mode backgrounds,
  workbenches, and integrated panels.
- **Light Rock:** Shale, granite, feldspar, and marble tones form light-mode
  backgrounds and surfaces without defaulting to cream, sand, or parchment.

### Secondary

- **Structural Metal:** Titanium, steel, and aluminum carry full-length
  component boundaries, controls, frames, and durable separators with flat,
  brushed, or rough-polished character.
- **Luminous Metal:** Mercury, tin, silver, and gold carry rare emphasis, fine
  content separators, and special surfaces with varied realistic luster.

### Tertiary

- **Gem Signals:** Sapphire, ruby, topaz, tourmaline, emerald, and tanzanite
  provide vivid semantic lights, bullets, notifications, and compact status
  marks. Exact semantic assignments are resolved with the implemented state
  vocabulary.

### Neutral

- **Stone Text:** High-contrast mineral neutrals carry guidance and long-form
  text without washed-out gray.
- **Metal Text:** Metallic neutrals distinguish exact identities, controls,
  borders, outlines, and measured evidence.

**The Material Role Rule.** Rock structures, metal defines, and gemstones
signal. Never exchange those roles merely for visual variety.

**The Believable Matter Rule.** Material effects must reflect plausible depth,
facet, polish, and luster. Stylized noise, glassmorphism, and decorative texture
are forbidden.

**The Rare Light Rule.** A gemstone glows only for an active state, attention,
or notification. Every light has an authoritative word or symbol equivalent.

**The Separator Rule.** Content separators may be thin silver or gold and
feather at their ends. Component separators are thicker, flatter structural
metals and run the full required length.

## Typography

**Display Font:** Calm humanist sans **[font to be chosen during implementation]**
**Body Font:** Calm humanist sans **[font to be chosen during implementation]**
**Label/Mono Font:** User-owned terminal monospace for CLI/TUI; a compatible
monospace **[font to be chosen during implementation]** for richer surfaces

**Character:** Guidance is composed and immediately readable. Commands,
revisions, metrics, identities, and structured evidence remain recognizably
monospaced. A terminal user's font choice is authoritative; MASTIC relies on
weight, spacing, alignment, symbols, and color rather than a bundled typeface.

### Hierarchy

- **Display:** Reserved for future brand or onboarding surfaces, never routine
  product controls.
- **Headline:** Names the current task or Plan stage without oversized drama.
- **Title:** Identifies the current target, resource, or evidence surface.
- **Body:** Explains state and next actions in lines no longer than 65-75
  characters where the terminal layout permits.
- **Label:** Carries controls, table headings, compact metadata, and state words
  without tiny uppercase tracking.

**The Terminal Ownership Rule.** Nerd-font glyphs may enrich a capable terminal
but can never be required for identity, navigation, state, or action.

**The Evidence Type Rule.** Monospace distinguishes exact evidence; it does not
make all prose feel like a daemon log.

## Elevation

MASTIC is flat and integrated at rest. Rock tone changes and structural metal
boundaries establish depth; panels are worked into their containing workbench.
Shadows are not a default material. Polished-metal luster and gemstone depth
may add local dimensionality on capable surfaces, but never turn every control
into a floating object.

Motion is responsive state feedback only: approximately 150-200 ms for focus,
selection, progress, structural change, and active gem light. There are no
orchestrated page entrances. Reduced-motion settings replace transitions with
an immediate change or restrained crossfade.

**The Integrated Panel Rule.** A panel belongs to its surrounding instrument;
it does not float above it merely to manufacture hierarchy.

**The Earned Glow Rule.** Glow is local, subtle, and caused by a live semantic
state. Ambient decorative bloom is forbidden.

## Do's and Don'ts

### Do:

- **Do** use rock for structural surfaces, metal for boundaries and controls,
  and gemstones for compact semantic signals.
- **Do** use cards only for self-contained, portable, or transferable entities;
  use rows, tables, panes, and integrated panels for ordinary product structure.
- **Do** keep exact artifacts, trust, resource cost, ownership, evidence, and
  planned mutations visible before confirmation.
- **Do** adapt material realism to the rendering surface while preserving the
  same semantic roles in CLI, TUI, web, and native interfaces.
- **Do** use icons, emoji, or optional Nerd-font glyphs deliberately and fairly
  liberally for attention and control targets, always with accessible labels.

### Don't:

- **Don't** build a raw daemon console that exposes ports, processes,
  configuration files, and adapter trivia instead of user intent, Plans,
  application targets, and valid next actions.
- **Don't** build a setup wizard that hides exact artifacts, trust, resource
  cost, ownership, evidence, or the mutations it is about to perform.
- **Don't** recreate a feature-poor dashboard that presents dense state without
  clear hierarchy, consequences, or recovery paths.
- **Don't** use generic dashboard card grids, decorative terminal nostalgia,
  neon cyberpunk, gratuitous animation, or unfamiliar controls invented for
  visual novelty.
- **Don't** make automatic behavior silently substitute components, rewrite
  external state, change capacity, or treat incomplete evidence as validation.
- **Don't** imitate Diablo Immortal inventory UI with loot-like jewel overload,
  ornate dark frames, constant glow, or game-status spectacle.
- **Don't** use cream, sand, parchment, or beige as the reflexive light-mode
  background; light stone must remain visibly mineral and brand-specific.
- **Don't** use colored side stripes, gradient text, glassmorphism, nested
  cards, or border-plus-wide-shadow ghost cards.
