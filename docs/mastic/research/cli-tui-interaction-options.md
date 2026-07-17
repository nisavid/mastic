# CLI and terminal-UI interaction options

Research completed 2026-07-15 against `systools` commit
`2c41892f867a23a86d7282d58d0e0309c5feaa39` and current upstream documentation.

## Question

Which maintained Python stacks can support one coherent mastic application with
discoverable commands, shell completion, contextual help, accessible keyboard
operation, responsive terminal layouts, testable screens, Apple-silicon
packaging, and predictable non-interactive output?

This document reports framework and implementation facts. It does not choose
the product's command model, navigation model, accessibility contract, or
migration sequence.

## Executive finding

The current `argparse` and `curses` implementation remains a viable foundation
for a small command set, and it can gain dynamic completion without replacing
the parser. It does not provide the higher-level primitives needed to keep a
large CLI and terminal UI coherent automatically. The application would still
need its own shared operation catalogue, semantic presentation models, and
cross-surface contract tests.

For a growing management application, the strongest maintained terminal-UI
fit is Textual. It provides widgets, focus management, bindings, a command
palette, layouts, themes, headless interaction testing, terminal resizing, and
visual snapshot testing. It is not a substitute for a scriptable CLI, and its
documentation does not establish screen-reader conformance.

There are two credible implementation routes:

1. Keep `argparse`, add argcomplete and better help, and replace the hand-built
   curses dashboard with Textual.
2. Move the CLI to a richer framework such as Typer, Click, or Cyclopts and use
   Textual for the terminal UI.

Neither route creates feature parity by itself. Parity comes from making CLI
commands and TUI actions adapters over the same application operations and
testing the resulting capability matrix.

## Current mastic interaction architecture

### CLI facts

- `mastic` constructs one static `argparse.ArgumentParser` with `start`, `stop`,
  `status`, `models`, `metrics`, and `dashboard` subcommands.
- The positional `server` arguments have no help text, choices, or completion.
  For example, `mastic status --help` renders `[server]` and a blank description.
- The parser is built before configuration is read, so it has no configured
  Server Definitions available to show as choices. Dynamic help or completion
  would need a deliberately side-effect-free configuration lookup.
- Each non-dashboard command has a `--json` switch. Human rendering and JSON
  rendering already share the same control response, which is a useful seam to
  preserve.
- Every command attempts launchd activation when it cannot reach the control
  socket. There is no CLI distinction between starting or stopping a Server
  Definition and activating or shutting down the Supervisor.
- CLI tests cover installed entry-point help and command behavior, but there is
  no parser-schema snapshot, completion test, or all-operations parity test.

### Terminal-UI facts

- `dashboard.py` owns separate presentation models and manually handles curses
  input, terminal resizing, selection, redraws, background control calls, and
  string truncation.
- The interactive actions are fixed key branches for start, stop, refresh, and
  quit. The footer is the only in-context help.
- A terminal smaller than 60 columns by 10 rows becomes a fixed warning screen.
  Larger terminals use line slicing rather than reflowing semantic widgets.
- Non-TTY execution prints one deterministic, escape-free snapshot. This is a
  strong behavior to keep even if the interactive framework changes.
- Tests cover deterministic wide and narrow rendering, degraded control-plane
  output, pseudo-terminal key interaction, resize behavior, and non-blocking
  refreshes. They do not have semantic focus assertions or framework-level
  screen queries because curses exposes no widget tree.

These are properties of the current implementation, not inherent limitations
of `argparse` or curses.

## CLI framework options

### Keep argparse and add argcomplete

`argparse` already supplies subcommands, help text, typed values, static
`choices`, aliases, and custom actions. Python 3.14 added colored help and typo
suggestions, but mastic currently supports Python 3.11 and therefore cannot
rely on those additions without changing its runtime floor. See the
[Python argparse reference](https://docs.python.org/3/library/argparse.html).

[argcomplete](https://kislyuk.github.io/argcomplete/) augments an argparse
parser with option, subparser, static-choice, and custom dynamic-value
completion. Its completers can return descriptions, and a configured-server
completer could read the local Server Definitions. Completion executes the
program up to the `argcomplete.autocomplete()` call on every tab request, so
imports and configuration reads before that seam must be fast and free of
activation, network, model download, or other side effects.

On macOS, argcomplete officially supports Bash and Zsh. Its global Bash mode
requires Bash 4.2, while macOS ships Bash 3.2; Zsh or per-command registration
avoids that mismatch. Other shells have only contributed support. Argcomplete
is therefore an incremental solution with a small migration cost, but it does
not improve help layout, command composition, or CLI testing by itself.

### Typer

[Typer](https://typer.tiangolo.com/) derives commands, values, validation, and
help from Python type annotations. Current Typer includes Rich help and errors,
`--install-completion` and `--show-completion`, multi-command groups, and a
[`CliRunner` testing API](https://typer.tiangolo.com/tutorial/testing/). Its
completion path covers Bash, Zsh, Fish, and PowerShell.

As of this research, PyPI publishes Typer 0.26.8 as a platform-independent
wheel for Python 3.10 and newer. Since 0.26.0, Typer vendors Click rather than
depending on it, and its project page warns that some Click functionality may
not remain available. That current implementation and Typer's rapid release
cadence make a pinned dependency and an upgrade test matrix prudent. Typer can
substantially improve help and completion ergonomics, but it does not provide
a terminal application framework or automatically expose commands as TUI
screens.

### Click directly

[Click](https://click.palletsprojects.com/en/stable/) offers explicit command
groups, parameter types, help generation, prompts, error handling, and
`CliRunner` tests without Typer's annotation layer. Its
[shell-completion system](https://click.palletsprojects.com/en/stable/shell-completion/)
supports Bash 4.4+, Zsh, and Fish; completion callbacks can return values with
descriptions. Generated scripts may be installed or shipped ahead of time to
avoid shell-startup overhead.

Click is more verbose than Typer or Cyclopts but gives direct control over the
command object graph. Its `Context.to_info_dict()` representation can help
generate documentation or inspect coverage. It still does not supply a
full-screen TUI or a product-level parity contract.

### Cyclopts

[Cyclopts](https://cyclopts.readthedocs.io/en/stable/) is another
type-annotation-driven CLI framework. It provides Rich help, nested commands,
validation, typo suggestions, documentation generation, and Bash, Zsh, and
Fish completion. It can generate static completion scripts or execute dynamic
completion; its documentation recommends static completion for production to
avoid importing the application on every tab request.

As of this research, PyPI publishes Cyclopts 4.20.0 as a
platform-independent wheel for Python 3.10 and newer, while a 5.0 prerelease
line is active. It fits mastic's Python floor and has a broad typed-parameter
model. Its smaller ecosystem and active major-version work are adoption risks
to assess with a command-shape prototype. Like Typer and Click, it does not
provide the terminal UI.

## Terminal-UI framework options

### Keep curses

Python's `curses` module is available on Unix platforms and adds no project
dependency. It exposes terminal windows, input codes, colors, pads, and direct
screen drawing. It does not provide semantic widgets, CSS-like layout,
focus traversal, a command palette, a standard headless interaction harness,
or accessibility metadata. All of those concerns remain application code.

Keeping curses is credible if the terminal UI stays a compact dashboard. For
the requested full management surface, continuing with curses means building
and maintaining a private UI framework alongside the product.

### Textual

[Textual](https://textual.textualize.io/) is a maintained Python application
framework for terminal and browser interfaces. It provides semantic widgets,
reactive state, focusable controls, keyboard and mouse bindings, screens and
modals, themes, CSS-like layout and styling, scrolling, and a built-in fuzzy
[command palette](https://textual.textualize.io/guide/command_palette/).

Its [`run_test()` and `Pilot` APIs](https://textual.textualize.io/guide/testing/)
run applications headlessly and can press keys, click widgets, pause for
messages, and set or change terminal dimensions. The official snapshot plugin
captures SVG output for visual regression testing. This is a material
improvement over pseudo-terminal-only curses tests because tests can query
semantic application state as well as rendered output.

As of this research, PyPI publishes Textual 8.2.8 as a `py3-none-any` wheel,
declares macOS support, and requires Python 3.9 or newer. It therefore fits
mastic's Apple-silicon and Python 3.11 packaging boundary without a native
architecture-specific TUI build. Textual is asynchronous internally, so the
Unix control client and long-running operations should be integrated through
workers or an async boundary instead of blocking the UI message loop.

Textual's public documentation describes focus, keyboard operation, themes,
and terminal testing, but it does not claim WCAG, VoiceOver, or other
screen-reader conformance. A Textual UI should not be treated as the sole
accessible interface without separate validation.

### prompt_toolkit

[prompt_toolkit](https://python-prompt-toolkit.readthedocs.io/en/stable/)
supports prompts, completion, validation, key bindings, focus, styles, layout
containers, and full-screen applications. Its official testing guidance uses
pipe input and dummy output to exercise interactions without a real terminal.

It is an excellent fit for an interactive shell, command prompt, or highly
custom keyboard-driven UI. Compared with Textual, it supplies fewer
product-level widgets and less styling and visual-testing infrastructure, so a
polished multi-screen management application requires more custom rendering
and composition code. It is a credible alternative when an interactive command
shell is the desired navigation model, not a drop-in dashboard upgrade.

### Generated command forms

Tools such as [Trogon](https://github.com/Textualize/trogon) can generate a
Textual form from a Click command tree. This can make unfamiliar options easier
to enter, but a generated command form is not an operational model browser,
cache manager, metrics dashboard, lifecycle monitor, or multi-step workflow.
It may be useful as an auxiliary command composer, not as the primary route to
full CLI/TUI parity.

## Comparison

| Stack | Discoverable CLI | Dynamic completion | Polished multi-screen TUI | Headless UI tests | Plain/JSON path | Migration weight |
| --- | --- | --- | --- | --- | --- | --- |
| argparse + curses | Basic | With argcomplete | Application-built | PTY/application-built | Already present | Low initially; high as UI grows |
| argparse + Textual | Basic; separately improvable | With argcomplete | Strong | Strong | Preserve current CLI | Medium |
| Typer + Textual | Rich typed help | Built in | Strong | Strong | Explicit output policy needed | Medium-high |
| Click + Textual | Rich explicit help | Built in | Strong | Strong | Explicit output policy needed | Medium-high |
| Cyclopts + Textual | Rich typed help | Built in | Strong | Strong | Explicit output policy needed | Medium-high |
| CLI framework + prompt_toolkit | Framework-dependent | Framework-dependent | Flexible but custom | Pipe/dummy I/O | Explicit output policy needed | Medium-high |

“Built in” means the CLI framework provides completion machinery. Users or
deployment tooling must still install or source the generated shell script.

## Accessibility and non-interactive behavior

The full-screen terminal UI and the CLI should be complementary, not mutually
exclusive accessibility modes. Framework choice cannot replace a product
contract. The evidence supports carrying these requirements into design:

- Every operation remains executable without a TTY and without prompts.
- Machine output has a versionable structured form and never gains decoration.
- Human output detects redirection, honors `NO_COLOR`, avoids control sequences
  in pipes, and uses stable exit statuses and stderr boundaries.
- The TUI is completely keyboard-operable, exposes visible focus, offers a
  discoverable command palette and contextual help, and never relies on color
  alone.
- Narrow terminals recompose or scroll semantic regions rather than truncate
  essential values.
- A plain CLI path remains available for VoiceOver and other assistive-tool
  validation even if the TUI passes keyboard and contrast checks.
- Completion callbacks must be fast and side-effect-free; completing a Server
  Definition or Model Alias must never activate masticd or contact a remote model
  registry unless the user explicitly requests a network-backed operation.

## Architectural implication: share operations, not widgets

No evaluated framework makes a CLI and TUI coherent automatically. A durable
shape would separate:

1. application operations and typed request/result models;
2. a capability catalogue containing names, summaries, argument metadata,
   applicability rules, and destructive/confirmation semantics;
3. CLI adapters and renderers;
4. TUI screens, widgets, and presenters; and
5. plain, JSON, and terminal presentation policies.

The CLI parser may derive some help and completion from the catalogue, and the
TUI command palette may derive available actions from it. Neither surface
should call the other or parse the other's rendered output. Cross-surface tests
should enumerate the catalogue and prove that every public operation has CLI
and TUI access, contextual help, and the intended non-interactive behavior.

## Decisions this research surfaces

The following remain product or architecture choices rather than research
findings:

- Whether to preserve argparse and modernize incrementally or replace the CLI
  parser before expanding the command model.
- If replacing it, whether Typer's concise typed API, Click's explicit command
  graph, or Cyclopts' typed parameter model best fits the desired command
  grammar and dependency policy.
- Whether Textual becomes the terminal-UI foundation, and whether the existing
  curses dashboard remains temporarily as a compatibility surface.
- What “all operations” means: the authoritative operation and information
  inventory must be defined before parity can be tested.
- Whether the default interactive navigation is resource-oriented screens,
  goal-oriented workflows, a command palette, an interactive shell, or a
  deliberate combination.
- What accessibility promise applies to the full-screen TUI versus the plain
  CLI, and which terminals and VoiceOver workflows form the acceptance matrix.
- How completion is installed on managed and unmanaged machines, and which
  completions may read local state or perform network access.
- The stable contracts for TTY detection, color, prompts, progress, JSON,
  error output, exit codes, and backwards compatibility.
- How dependencies are pinned and upgraded given the current release cadence
  of Typer, Cyclopts, and Textual.

## Source index

- [Python `argparse`](https://docs.python.org/3/library/argparse.html)
- [Python `curses`](https://docs.python.org/3/library/curses.html)
- [argcomplete documentation](https://kislyuk.github.io/argcomplete/)
- [Typer documentation](https://typer.tiangolo.com/)
- [Typer package metadata](https://pypi.org/project/typer/)
- [Click documentation](https://click.palletsprojects.com/en/stable/)
- [Click shell completion](https://click.palletsprojects.com/en/stable/shell-completion/)
- [Click testing](https://click.palletsprojects.com/en/stable/testing/)
- [Cyclopts documentation](https://cyclopts.readthedocs.io/en/stable/)
- [Cyclopts shell completion](https://cyclopts.readthedocs.io/en/latest/shell_completion.html)
- [Cyclopts package metadata](https://pypi.org/project/cyclopts/)
- [Textual documentation](https://textual.textualize.io/)
- [Textual testing](https://textual.textualize.io/guide/testing/)
- [Textual package metadata](https://pypi.org/project/textual/)
- [Trogon repository](https://github.com/Textualize/trogon)
- [prompt_toolkit documentation](https://python-prompt-toolkit.readthedocs.io/en/stable/)
- [prompt_toolkit unit testing](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/advanced_topics/unit_testing.html)
