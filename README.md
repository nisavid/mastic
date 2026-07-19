# MASTIC

Modular, Adaptive, System-Tailored Inference Connector.

MASTIC is one guided CLI and TUI for configuring and operating a
host-tailored local inference stack on a compatible Apple-silicon Mac.

The first supported vertical installs and operates one MLX model path, then
configures Codex Responses and Hindsight Chat Completions reversibly through
the same authenticated local gateway. `mastic` is the human and automation
entry point; `masticd` owns the per-user controller and gateway lifecycle.

The initial vertical deliberately keeps broader adapter, remote-hosting, and
Phase 2 Messages decisions open. Its design evidence lives in `docs/research/`.

## Development

```sh
uv run --frozen python -m unittest discover -s tests -t . -v
uv run --frozen pyrefly check --output-format min-text
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv build
```
