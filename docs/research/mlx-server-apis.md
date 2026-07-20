# Historical MLX server HTTP API snapshot

This note preserves research originally performed for the historical `systools`
server adapter. It describes `mlx-lm` v0.31.3 with `mlx-optiq` v0.3.1 and does
not define MASTIC's current pinned runtime. See
[`optiq-0.3.3-sampling-compatibility.md`](optiq-0.3.3-sampling-compatibility.md)
for the shipped OptiQ 0.3.3 contract.

## `mlx_lm.server` (v0.31.3)

Implemented on the stdlib `http.server` (`ThreadingHTTPServer` +
`BaseHTTPRequestHandler` subclass `APIHandler`). No web framework.

| Method | Path | Response |
|--------|------|----------|
| `GET` | `/health` | `200 {"status": "ok"}` |
| `GET` | `/v1/models` | `200 {"object":"list","data":[{"id":"<repo_id or abs path>","object":"model","created":<ts>}]}` |
| `GET` | `/v1/models/<repo_id>` | filtered model list |
| `POST` | `/v1/completions` | text completion |
| `POST` | `/v1/chat/completions` | chat completion (supports `stream`) |
| `OPTIONS` | `*` | CORS preflight |

Non-streaming completion responses carry a `usage` object:

```json
"usage": {
  "prompt_tokens": <int>,
  "completion_tokens": <int>,
  "total_tokens": <int>,
  "prompt_tokens_details": {"cached_tokens": <int>}   // optional
}
```

CLI flags: `--model`, `--host`, `--port`, `--draft-model`,
`--prompt-cache-size`, `--prompt-concurrency`, `--pipeline`, plus sampling
(`--temp`, `--top-p`, `--top-k`, …).

**No `/stats`, `/metrics`, or process-info endpoint.**

## `optiq serve` (v0.3.1)

Wraps `mlx_lm.server` by monkey-patching `APIHandler.do_GET`/`do_POST` (same
stdlib `http.server`). Inherits every `mlx_lm` endpoint above, and adds:

| Method | Path | Source | Notes |
|--------|------|--------|-------|
| `POST` | `/v1/responses` | `optiq/responses_server.py` | OpenAI Responses API |
| `POST` | `/v1/messages` | `optiq/anthropic_shim.py` | Anthropic Messages API; only with `--anthropic` |
| `GET` | `/v1/adapters` | `optiq/serve.py` | lists mounted LoRA adapters |
| `GET` | `/v1/models` | `optiq/runtime/model_listing.py` | extended to advertise locally-built OptiQ quants |

Extra CLI flags: `--kv-bits`, `--kv-group-size`, `--quantized-kv-start`,
`--kv-config`, `--adapter` (repeatable; mounted LoRA),
`--anthropic`/`--no-anthropic`, `--allow-model-switch`/`--single-model`,
`--idle-timeout`, `--max-context`. All other args forward to `mlx_lm.server`.

**Still no `/stats` or `/metrics` endpoint.**

## Uniform probe surface

Both servers share:

- **Liveness:** `GET /health` → `{"status":"ok"}`.
- **Model introspection:** `GET /v1/models` → OpenAI-style model list.
- **Ready-check:** poll `GET /v1/models` until it returns `200` — exactly
  what `optiq`'s own `lab/api_supervisor.py` does when it spawns a server.

## Metrics implications

Neither server exposes a native stats or metrics endpoint, so `systools`
cannot simply scrape an aggregate-stats URL. The collectible dimensions are:

- **Per-request token counts for non-streaming responses** — from the `usage`
  object (`prompt_tokens`, `completion_tokens`, `total_tokens`,
  `cached_tokens`). Do not assume a streamed response includes usage; collect
  it only when the stream explicitly provides it or through separate proxy or
  client instrumentation. The servers do not report aggregate usage.
- **Process-level stats** — RSS memory, CPU% — from `psutil` on the server
  PID, not from the HTTP API.
- **Request latency** (TTFT, total) — the supervisor must time requests
  (proxy); there is no server-native latency reporting.

## Reference for the supervisor

`optiq/lab/api_supervisor.py` implements a ready-made supervisor pattern:
spawn the server subprocess, poll `GET /v1/models` for readiness with a
timeout, thread-safe apply. Useful reference for `systools`' supervisor
lifecycle and health-check design.
