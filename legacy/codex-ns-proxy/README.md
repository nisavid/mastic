# codex-ns-proxy

`codex-ns-proxy` is an authenticated loopback gateway between Codex and a
Responses-compatible model provider. It keeps provider-specific transport and
Codex namespace-tool adaptation behind one local interface.

The gateway supports a local OptiQ server and an explicitly configured remote
provider such as an air-gapped GLM deployment. It has no upstream default and
does not contact an upstream during import, configuration validation, or
startup.

The inbound bearer protects the app-facing gateway seam. It does not add
authentication to the provider's separate listener: for example, a stock
OptiQ loopback listener still accepts direct local requests. Run the provider
on loopback, keep the machine's local processes within the trust model, and use
an isolated profile so Codex reaches it through this gateway.

## Contract

The gateway:

- listens only on a loopback address;
- requires a fresh inbound bearer token for every run;
- forwards only `GET /v1/models` and `POST /v1/responses`;
- never forwards the inbound credential upstream;
- optionally injects a distinct upstream bearer credential;
- optionally flattens Codex namespace tools before forwarding and reconstructs
  only the exact mapped namespace function calls in plain and SSE output;
- sends configurable SSE comment heartbeats while an upstream stream is silent;
- preserves unchanged SSE lines and event order; and
- applies request-size and connection-timeout limits.

Request and response bodies, tool arguments, generated text, and credentials
are never logged. Diagnostic mode reports only structural state such as the
number of transformed namespaces, transport error class, downstream
disconnects, and whether `response.completed` was observed.

## Run

Create a high-entropy token for the run and pass it independently to the
gateway and the isolated Codex profile:

```bash
run_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

NS_PROXY_UPSTREAM='http://127.0.0.1:18997/v1' \
NS_PROXY_INBOUND_TOKEN="$run_token" \
NS_PROXY_ADAPTER='codex-namespace' \
python3 tooling/codex-ns-proxy/codex-ns-proxy.py
```

Configure the isolated Codex provider with the gateway URL and expose the same
per-run token to Codex through its provider credential environment variable:

```toml
[model_providers.local_gateway]
base_url = "http://127.0.0.1:18999/v1"
env_key = "CODEX_LOCAL_GATEWAY_TOKEN"
wire_api = "responses"
```

```bash
CODEX_LOCAL_GATEWAY_TOKEN="$run_token" codex
```

For an upstream that requires its own bearer credential, set
`NS_PROXY_UPSTREAM_TOKEN` separately. The gateway rejects reuse of the inbound
token so the two credential seams cannot be conflated.

`NS_PROXY_ADAPTER` defaults to `identity`. Select `codex-namespace` only for a
profile whose captured Responses traffic proves namespace adaptation is
required. The adapter rejects ambiguous flattened names and non-function
namespace children. It keeps a bounded, process-local least-recently-used map
for `previous_response_id` continuation; a missing or evicted ID fails closed.
Restarting the gateway clears this state. Full inline history, including
function-call output, is also supported.

Shutdown closes active upstream connections before joining request threads, so
a live Responses stream cannot hold gateway teardown open indefinitely.

SSE heartbeats default to every 15 seconds and are emitted only after the
downstream SSE headers, between complete upstream frames, and before a terminal
event or EOF. Each heartbeat is an SSE comment, so it carries no model data and
does not alter the relative order or bytes of upstream frames. Heartbeats keep
the downstream transport active; they do not extend the configured upstream
read timeout.

A valid `response.completed` frame marks semantic completion as soon as that
frame is flushed. The gateway logs that state once and suppresses later
heartbeats, while continuing to forward upstream frames unchanged. A data-only
SSE frame with one exact `[DONE]` payload is forwarded and then ends the stream.
Frames that also contain event, ID, retry, comment, or additional data fields
remain ordinary upstream frames. If a provider omits the sentinel, EOF or the
configured upstream read timeout ends the stream; the gateway never fabricates
`[DONE]`.

The configured `NS_PROXY_UPSTREAM` URL is the complete upstream allowlist. Its
origin and base path are parsed once; fixed route suffixes are appended without
accepting caller-controlled path segments. Sanitized terminal evidence is
written only to stderr when `NS_PROXY_DEBUG=true`, as
`SSE terminal_completed=true|false`.

## Configuration

| Variable | Default | Description |
|---|---:|---|
| `NS_PROXY_UPSTREAM` | required | HTTP(S) provider base URL, normally ending in `/v1` |
| `NS_PROXY_INBOUND_TOKEN` | required | Generated per-run bearer, at least 32 characters |
| `NS_PROXY_UPSTREAM_TOKEN` | unset | Optional independent upstream bearer |
| `NS_PROXY_HOST` | `127.0.0.1` | Loopback listen address |
| `NS_PROXY_PORT` | `18999` | Listen port |
| `NS_PROXY_MAX_BODY_BYTES` | `8388608` | Maximum request body size |
| `NS_PROXY_UPSTREAM_TIMEOUT` | `30` | Upstream connection/read timeout in seconds |
| `NS_PROXY_INBOUND_TIMEOUT` | `30` | Inbound socket timeout in seconds |
| `NS_PROXY_SSE_HEARTBEAT` | `15` | Positive SSE silence interval before sending a downstream comment heartbeat |
| `NS_PROXY_ADAPTER` | `identity` | `identity` or explicit `codex-namespace` adaptation |
| `NS_PROXY_NAMESPACE_MAP_CAPACITY` | `256` | Maximum remembered response-ID mappings |
| `NS_PROXY_DEBUG` | `false` | Enable sanitized structural diagnostics |

## Test

The regression suite uses only local fake HTTP and SSE upstreams:

```bash
python3 -m unittest discover -s tooling/codex-ns-proxy/tests -v
```
