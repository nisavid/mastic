# Bifrost and LiteLLM gateway conformance bake-off

Research date: 2026-07-17 (America/New_York)

Repository baseline: `67e9aa43624f48026e46c93d317332676cd009d2`

## Question

Can a current Bifrost or LiteLLM release replace the compatibility-gateway
contract inherited from `legacy/codex-ns-proxy`, while remaining a replaceable
data plane behind MASTIC's controller?

This note answers the measurement question in [issue 5](https://github.com/nisavid/mastic/issues/5).
It supplies evidence for, but does not make, the human selection in
[issue 6](https://github.com/nisavid/mastic/issues/6). The product and process
boundaries remain those in [issue 1](https://github.com/nisavid/mastic/issues/1).

## Result

**Neither release is a drop-in replacement for `codex-ns-proxy`.** Both handled
a basic OpenAI Responses request, forwarded explicit history and
`previous_response_id`, streamed ordered Responses events, and substituted a
configured upstream bearer. Bifrost's native `/v1/responses` route flattened
namespace definitions, but did not reconstruct namespaced calls or fail closed
on flat-name collisions; its `/openai/v1/responses` integration and LiteLLM
passed namespace structures through unchanged. Neither candidate therefore
provided the required bidirectional namespace adapter. Neither emitted a
heartbeat during 16.2 seconds of provider silence or ended the downstream
stream when a valid
`response.completed` arrived but the provider kept its socket open.

**Inference:** Bifrost remains the better provisional **local/private-profile
candidate**: its measured macOS process was substantially smaller, its official
Linux image was substantially smaller, and it has a documented direct
[Codex/Responses route](https://docs.getbifrost.ai/cli-agents/codex-cli).
LiteLLM remains the stronger **breadth fallback**: its documented
[provider](https://docs.litellm.ai/docs/providers),
[routing](https://docs.litellm.ai/docs/proxy/load_balancing), and deployment
surface is wider, and its encrypted Responses IDs survived a process restart in
this fixture. Those are candidacy findings, not acceptance.

The safe Phase 1 shape is therefore:

1. retain `codex-ns-proxy` as the fail-closed compatibility sidecar;
2. continue Bifrost first and LiteLLM second into issue 6;
3. do not absorb the sidecar until the selected gateway passes namespace,
   terminal-stream, heartbeat, credential, privacy, and cancellation fixtures;
4. keep the gateway replaceable and outside inference-engine lifecycle.

## Evidence labels and limits

- **Measured**: directly observed against the fixtures and versions below.
- **Source**: stated in first-party documentation or tagged source.
- **Inference**: a design consequence of measured/source evidence, not a direct
  observation.
- **Not measured**: intentionally left open.

Every disposition and recommendation below is an **Inference** unless the text
explicitly labels it otherwise.

The upstream was a deterministic loopback fixture, not a paid provider or local
inference engine. The run measures gateway behavior at the wire boundary. It
does not establish cross-provider semantic quality or production throughput.

## Contract matrix

| Contract surface | Bifrost transport 1.6.4 | LiteLLM 1.92.0 | Disposition |
| --- | --- | --- | --- |
| Basic Responses JSON | **Measured:** accepted both `/openai/v1/responses` and the native `/v1/responses`; forwarded to the provider's `/v1/responses`; returned the fixture response. It normalized input into a message array and added defaults, routing metadata, and trace headers. | **Measured:** accepted `/v1/responses`; returned the fixture function call. It rewrote the response ID and normalized the response body. | Both are viable Responses data planes, but neither is byte-preserving. |
| Namespace tool definitions | **Measured:** `/openai/v1/responses` forwarded `type: "namespace"` and nested tools unchanged. The native `/v1/responses` flattened the definition to `type: "function", name: "add"`, but returned the fixture's flat call without `namespace`. Two namespaces containing `run` became duplicate flat `run` definitions and the request succeeded rather than failing closed. | **Measured:** namespace definitions were forwarded unchanged; a flat upstream `alpha__echo` call remained flat on return. | **Fail for sidecar replacement.** Pass-through is not adaptation; Bifrost's native flattening lacks reversible naming, reconstruction, and collision safety. |
| Namespace-bearing full history | **Measured:** forwarded `namespace: "math", name: "add"` unchanged with its function output. | **Measured:** forwarded the same history unchanged. | **Fail for providers that require flat tool names.** Neither owns the required bidirectional transform. |
| `previous_response_id` | **Measured:** forwarded the provider ID unchanged. | **Measured:** encrypted a returned ID at the client boundary, decrypted it to the original provider ID on the next request, and repeated that successfully after a clean proxy restart with the same master key. The tagged implementation documents the ID security hook in [source](https://github.com/BerriAI/litellm/blob/b3086ccd74553565c9a39716e72303ae985555f9/litellm/proxy/hooks/responses_id_security.py). | Both can preserve provider-owned continuation. LiteLLM adds a stable credential-bound routing/security envelope. Neither result supplies namespace-map inheritance. |
| Explicit full-history continuation | **Measured:** function call and output items reached the fixture in order. | **Measured:** function call and output items reached the fixture in order. | Pass for transport; cross-provider interpretation was **not measured**. |
| SSE ordering and shape | **Measured:** preserved event names and ordered `response.created`, `response.output_item.added`, `response.completed`; normalized every JSON payload. | **Measured:** preserved ordered data event types, removed the upstream `event:` lines, rewrote response IDs, and normalized payloads. | Both preserve the logical sequence; neither preserves exact wire bytes. Client tolerance for LiteLLM's data-only form must remain a fixture. |
| Heartbeat | **Measured:** no downstream bytes between 0.007 s and 16.208 s. | **Measured:** no downstream bytes between 0.051 s and 16.249 s. | **Fail.** The legacy contract emits bounded SSE comments during silence. |
| Terminal completion | **Measured:** delivered `response.completed`, but `curl --max-time 10` timed out while the fixture kept the provider connection open. | **Measured:** delivered `response.completed`, but `curl --max-time 5` timed out in the same condition. | **Fail.** Neither demonstrated the sidecar's terminal-frame closure behavior. |
| Caller/provider credential separation | **Measured:** with inference auth disabled for the protocol fixture, an arbitrary caller bearer did not reach the provider; the configured `upstream-secret-bifrost` did. A second run enabled governance and a persisted virtual key: missing and unknown keys returned 401 without provider contact, the valid key returned 200, and only the configured provider bearer reached the fixture. **Source:** Bifrost virtual keys are designed to authenticate callers and constrain stored provider keys ([virtual-key documentation](https://docs.getbifrost.ai/features/governance/virtual-keys)). | **Measured:** the configured master key admitted the request; the fixture saw only `upstream-secret-litellm`. A missing bearer failed closed, but returned HTTP 500 because the measured `litellm[proxy]` environment lacked `prisma` on that error path. **Source:** LiteLLM documents master/virtual keys separately from provider credentials ([virtual-key documentation](https://docs.litellm.ai/docs/proxy/virtual_keys)). | Header substitution and Bifrost's authenticated gate passed. LiteLLM's missing-key 401 behavior still needs a release-image fixture. |
| Client-visible diagnostic safety | **Measured:** no credential appeared in the response. Trace/provider/model headers and non-secret routing metadata were present. | **Measured:** no credential appeared, but default response headers exposed the configured upstream base URL and upstream server/date headers. | Add an explicit response-header allowlist to conformance. Do not assume observability headers are safe merely because they contain no token. |
| Privacy at rest | **Measured:** with logging and both stores disabled plus content logging disabled, the unauthenticated application directory gained no file beyond the supplied config. The authenticated virtual-key path required a config store; when literal fixture secrets were supplied in JSON, the resulting SQLite file contained both caller and provider fixture secrets as `plain_text`. | **Measured:** with spend logs disabled and message logging off, the fixture directory gained no file beyond the supplied config. | These are hardened profiles, not bare defaults. The controller must render privacy-safe settings and secret references explicitly. Bifrost documents content suppression ([observability](https://docs.getbifrost.ai/features/observability/default)); LiteLLM documents logging controls ([logging](https://docs.litellm.ai/docs/proxy/logging)). |
| Background network defaults | **Measured:** the authenticated first boot synchronized 9,921 model-parameter records and 123 MCP-library entries from Bifrost-hosted services despite request/log content persistence being disabled. | **Not measured.** | A private/offline profile must explicitly disable or redirect catalog and pricing synchronization, then prove restart without network access. |
| Observability | **Source/Measured:** documented Prometheus/OpenTelemetry and request logging; measured trace, provider, model, request-type, and routing metadata without content persistence. Unauthenticated `/health` and `/metrics` returned 200 on the loopback listener; metrics included non-secret virtual-key and provider-key names. | **Source/Measured:** documented callbacks, metrics, and logging; measured call/model IDs, duration, cost, retry/fallback, and provider headers. Authenticated `/health` actively sent a Chat Completions probe and returned detailed failure context when the Responses-only fixture rejected it; `/metrics` returned 404 in the measured default profile. | Both have operational seams. MASTIC still owns endpoint authentication, privacy-safe health semantics, metrics policy, and a safe client-visible header set. |
| Replaceable lifecycle | **Measured:** standalone process accepted config/app-dir/host/port flags; native process was independently startable; official container ran on Linux arm64. The NPX wrapper defaults the transport to `latest`, but supports an explicit `--transport-version` in [tagged source](https://github.com/maximhq/bifrost/blob/c4cd51af26e0e870d4d16d006d1257c08822fd13/npx/bifrost/bin.js). | **Measured:** standalone `uvx` process accepted config/host/port, shut down cleanly, restarted, and resumed an encoded continuation ID. Official release images are published and signed as documented in the [repository](https://github.com/BerriAI/litellm/blob/b3086ccd74553565c9a39716e72303ae985555f9/README.md#verify-docker-image-signatures). | Both can remain controller-supervised external processes. Pin the Bifrost transport itself, not only the NPX package. |
| Inference lifecycle ownership | **Source:** gateway/SDK surface; no engine install, load, unload, or host activation contract in the inspected release ([overview](https://docs.getbifrost.ai/overview)). | **Source:** SDK/proxy surface; production docs cover proxy deployment rather than inference-engine lifecycle ([production deployment](https://docs.litellm.ai/docs/proxy/prod)). | Pass for the architecture: neither should become the controller or engine adapter. |

## Operational footprint

### Release identity

| Item | Exact observation |
| --- | --- |
| Bifrost NPX wrapper | `@maximhq/bifrost@1.6.3`; package source declares 1.6.3. Its default `latest` lookup downloaded transport 1.6.4. |
| Bifrost transport | Banner `v1.6.4`; [official transport release](https://github.com/maximhq/bifrost/releases/tag/transports/v1.6.4); tagged source commit `c4cd51af26e0e870d4d16d006d1257c08822fd13`. |
| Bifrost macOS binary | Mach-O arm64; 111 MiB file; SHA-256 `039a491b995d5835eaf4d30ef2da13bb9059ba77f2e462a3bb509bdd13005051`. |
| LiteLLM | PyPI and CLI version 1.92.0; [official release](https://github.com/BerriAI/litellm/releases/tag/v1.92.0); tagged source commit `b3086ccd74553565c9a39716e72303ae985555f9`. |

### Measured host and footprint

The native host was Apple Silicon arm64 on macOS 26.5.2. Linux measurements
used Podman 5.8.3 with a Linux arm64 6.1.0-dev server. Docker Desktop/OrbStack's
daemon was unavailable, so Docker-specific behavior was not measured.

| Measure | Bifrost | LiteLLM |
| --- | ---: | ---: |
| Native idle RSS after requests | 18,976 KiB | 133,008 KiB |
| Native installed/cache footprint | 113 MiB transport cache | 445 MiB isolated `uvx` environment, 11,811 files |
| Official Linux arm64 image size | 233,393,048 bytes | 1,115,011,552 bytes |
| Official image architectures | arm64 and amd64 manifests | arm64 and amd64 manifests, plus attestations |
| Linux arm64 idle memory | 89.9-90.2 MiB for Bifrost with file/log stores disabled | **Not measured** |
| Clean shutdown | **Not timed** | Uvicorn completed shutdown in roughly 0.1 s after SIGINT in this run |
| Cold start | **Not measured** | **Not measured** |
| Added first-token latency | **Not measured** | **Not measured** |
| Sustained throughput | **Not measured** | **Not measured** |

The Linux Bifrost image initially refused a read-only `/app/data` mount because
its entrypoint requires a writable app directory. With config/log stores
disabled, `BIFROST_SKIP_WRITE_CHECK=1` allowed that read-only profile and
`/health` reported OK. This is direct measurement, not a recommendation to
bypass the check in a stateful profile.

### Supplemental loopback JSON latency

A second native verification used a deterministic Python 3.9 loopback fixture,
the authenticated Bifrost native `/v1/responses` route, and the authenticated
LiteLLM `/v1/responses` route. Each case was warmed with 10 requests, then timed
for 100 sequential requests. The fixture closed each connection; this is
single-request proxy overhead, not sustained throughput or streaming latency.

| Path | p50 | p95 | Mean |
| --- | ---: | ---: | ---: |
| Direct fixture | 0.299 ms | 0.478 ms | 0.320 ms |
| Bifrost | 0.739 ms | 1.033 ms | 0.859 ms |
| LiteLLM | 6.282 ms | 6.770 ms | 6.284 ms |

**Inference:** Bifrost's measured median added roughly 0.44 ms over the direct
fixture; LiteLLM's added roughly 5.98 ms. These figures support the local
footprint comparison but do not replace concurrent, streaming, translated-
provider, or real-engine benchmarks.

## Reproduction contract

### Fixture

The loopback fixture listened on `127.0.0.1:19080` and recorded method, path,
selected headers, and decoded body. It implemented:

- `POST /v1/responses`, returning one completed `function_call` named
  `alpha__echo` for non-streaming requests;
- streaming `response.created`, `response.output_item.added`, and
  `response.completed` frames, with a 16.2-second pause after the created
  frame when input was `delay`;
- an intentionally open provider connection after `response.completed`, so
  terminal-frame recognition could be distinguished from provider EOF;
- `GET` and `DELETE /_requests` to inspect and reset captures.

The primary request fixtures were:

```json
{"model":"fixture-model","input":"hello"}
```

```json
{
  "model": "fixture-model",
  "input": "hello",
  "tools": [{
    "type": "namespace",
    "name": "math",
    "description": "Math tools",
    "tools": [{
      "type": "function",
      "name": "add",
      "description": "Add",
      "parameters": {"type": "object"}
    }]
  }]
}
```

```json
{
  "model": "fixture-model",
  "previous_response_id": "resp_parent",
  "input": [
    {"type":"function_call","call_id":"call_one","namespace":"math","name":"add","arguments":"{}"},
    {"type":"function_call_output","call_id":"call_one","output":"5"}
  ]
}
```

The namespace fixtures come from the current legacy behavior and tests:
[`legacy/codex-ns-proxy/tests/test_proxy.py`](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/codex-ns-proxy/tests/test_proxy.py).
The required auth, mapping, heartbeat, terminal, and safe-diagnostic behavior is
described in the legacy
[`README.md`](https://github.com/nisavid/mastic/blob/67e9aa43624f48026e46c93d317332676cd009d2/legacy/codex-ns-proxy/README.md).

### Gateway configurations

Bifrost used this effective shape. Protocol fixtures set
`enforce_auth_on_inference: false`; authenticated virtual-key behavior remains
an explicit follow-up.

```json
{
  "client": {
    "disable_content_logging": true,
    "enable_logging": false,
    "enforce_auth_on_inference": false,
    "initial_pool_size": 1,
    "max_request_body_size_mb": 1
  },
  "config_store": {"enabled": false},
  "logs_store": {"enabled": false},
  "providers": {
    "openai": {
      "keys": [{
        "name": "fixture-upstream",
        "value": "upstream-secret-bifrost",
        "weight": 1,
        "models": ["fixture-model"]
      }],
      "network_config": {
        "base_url": "http://127.0.0.1:19080",
        "max_retries": 0,
        "stream_idle_timeout_in_seconds": 30
      }
    }
  }
}
```

The supplemental authenticated/native-route run used a custom OpenAI-compatible
provider named `fixture`, enabled the governance plugin with
`is_vk_mandatory: true`, set `client.enforce_auth_on_inference: true`, enabled
an explicit SQLite config store, and kept the log store and content logging
disabled. Bifrost refused to initialize that auth path with the config store
disabled. Its virtual key had the required `sk-bf-` prefix. The SQLite
`plain_text` observation applies to literal secret values in this explicit
fixture; environment-backed or encrypted secret-store behavior was **not
measured**.

LiteLLM used this effective shape:

```yaml
model_list:
  - model_name: fixture-model
    litellm_params:
      model: openai/fixture-model
      api_base: http://127.0.0.1:19080/v1
      api_key: upstream-secret-litellm
general_settings:
  disable_spend_logs: true
  master_key: inbound-secret-litellm
  store_model_in_db: false
litellm_settings:
  json_logs: true
  set_verbose: false
  turn_off_message_logging: true
```

Values above are fixture markers, not real credentials.

### Commands

```sh
# Resolve and identify releases.
npx -y @maximhq/bifrost@1.6.3 --help
uvx --from 'litellm[proxy]==1.92.0' litellm --help

# Start pinned native processes after writing the configs above.
npx -y @maximhq/bifrost@1.6.3 --transport-version v1.6.4 \
  -app-dir "$TMPDIR/mastic-gateway-bakeoff/bifrost-app" \
  -host 127.0.0.1 -port 19081
uvx --from 'litellm[proxy]==1.92.0' litellm \
  --config "$TMPDIR/mastic-gateway-bakeoff/litellm/config.yaml" \
  --host 127.0.0.1 --port 19082 --telemetry false

# Inspect exact multi-architecture release manifests.
podman manifest inspect docker.io/maximhq/bifrost:v1.6.4
podman manifest inspect ghcr.io/berriai/litellm:v1.92.0

# Make a request and inspect exactly what the fixture received.
curl -sS -H 'Authorization: Bearer inbound-secret-litellm' \
  -H 'Content-Type: application/json' \
  --data '{"model":"fixture-model","input":"hello"}' \
  http://127.0.0.1:19082/v1/responses
curl -sS http://127.0.0.1:19080/_requests

# Terminal handling: fixture emits completion quickly but keeps upstream open.
curl -sS -N --max-time 5 \
  -H 'Authorization: Bearer inbound-secret-litellm' \
  -H 'Content-Type: application/json' \
  --data '{"model":"fixture-model","input":"hello","stream":true}' \
  http://127.0.0.1:19082/v1/responses
```

The actual first Bifrost invocation omitted `--transport-version`, which is why
wrapper 1.6.3 resolved transport 1.6.4. The reproduction command pins both.

## Follow-up acceptance gates

These are required before issue 6 can accept either candidate as the default:

1. Run the selected gateway with real inbound virtual keys and assert 401 for
   missing/unknown/revoked keys, exact caller/upstream separation, and no secret
   in any client response, stdout/stderr, store, trace, or process environment.
2. Host the exact legacy namespace adapter in the candidate's supported
   extension seam, or retain the sidecar. Re-run definitions, history,
   collisions, bounded `previous_response_id` inheritance, JSON, and SSE.
3. Add or configure SSE heartbeat and terminal-frame closure. Verify partial
   frames, `[DONE]`, malformed terminal objects, provider EOF, idle timeout,
   cancellation, disconnect, and shutdown cleanup.
4. Repeat on Linux amd64 and run the LiteLLM arm64 image. Measure cold start,
   idle RSS after background work settles, first-event overhead, concurrency,
   throughput, and offline restart.
5. Exercise at least OpenAI Responses, one translated hosted provider, and one
   local OpenAI-compatible engine. Provider pass-through is not proof of
   translated continuation or tool fidelity.
6. Pin images by digest and the Bifrost transport by tag/checksum. Exercise
   upgrade, rollback, corrupt state, read-only/private profile, and controller
   restart without coupling inference lifecycle to the gateway.

## Primary sources

- Bifrost: [transport 1.6.4 release](https://github.com/maximhq/bifrost/releases/tag/transports/v1.6.4), [Codex CLI](https://docs.getbifrost.ai/cli-agents/codex-cli), [gateway setup](https://docs.getbifrost.ai/quickstart/gateway/setting-up), [streaming](https://docs.getbifrost.ai/quickstart/gateway/streaming), [virtual keys](https://docs.getbifrost.ai/features/governance/virtual-keys), [observability](https://docs.getbifrost.ai/features/observability/default), and [plugins](https://docs.getbifrost.ai/plugins/getting-started).
- LiteLLM: [1.92.0 release](https://github.com/BerriAI/litellm/releases/tag/v1.92.0), [Responses API](https://docs.litellm.ai/docs/response_api), [routing](https://docs.litellm.ai/docs/proxy/load_balancing), [virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys), [logging](https://docs.litellm.ai/docs/proxy/logging), and [production deployment](https://docs.litellm.ai/docs/proxy/prod).
