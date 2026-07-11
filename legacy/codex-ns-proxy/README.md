# codex-ns-proxy

A local HTTP proxy that sits between Codex CLI and a custom model provider,
flattening `type: "namespace"` tool groups into individual `type: "function"`
tools that models like GLM 5.2 can call.

## Problem

Codex wraps MCP and plugin tools in `type: "namespace"` groups in the
Responses API request. This is a non-standard extension that OpenAI models
understand but other models (e.g., GLM 5.2 on Systalyze) do not. The model
sees the tool descriptions but cannot emit `function_call`s for them.

## Solution

The proxy intercepts API requests and:

1. **Request side**: Flattens `type: "namespace"` tool groups into individual
   `type: "function"` tools with namespaced names (e.g., `github__get_me`).
   Also flattens `namespace` fields in conversation history `function_call`
   items so the model sees consistent naming.
2. **Response side**: Splits flattened function_call names (e.g.,
   `github__get_me`) back into separate `name` (`get_me`) and `namespace`
   (`github`) fields that Codex's tool router expects.

## Installation

```bash
# Symlink
ln -sf $(pwd)/codex-ns-proxy.py ~/.local/bin/codex-ns-proxy

# LaunchAgent (auto-start)
cp ~/Library/LaunchAgents/com.nisavid.codex-ns-proxy.plist \
   ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.nisavid.codex-ns-proxy.plist
```

## Configuration

Set `base_url` in `~/.codex/config.toml` to the proxy:

```toml
[model_providers.systalyze]
base_url = "http://127.0.0.1:18999/v1"
```

The proxy forwards to the real upstream URL set via the `NS_PROXY_UPSTREAM`
environment variable (default: Systalyze GLM 5.2 endpoint).

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NS_PROXY_HOST` | `127.0.0.1` | Listen address |
| `NS_PROXY_PORT` | `18999` | Listen port |
| `NS_PROXY_UPSTREAM` | Systalyze endpoint | Upstream API URL |
| `NS_PROXY_DEBUG` | unset | Enable verbose logging |
