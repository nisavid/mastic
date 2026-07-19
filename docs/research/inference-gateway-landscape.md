# Inference gateway and lifecycle landscape

Research date: 2026-07-16

This is a frozen pre-migration research record. It compares candidate gateways
and lifecycle projects before MASTIC selected its in-process Gateway and
Responses adapter. Its recommendations are historical evidence, not the
current implementation plan. For current contracts, start with
[`CONTEXT.md`](../../CONTEXT.md), [`PRODUCT.md`](../../PRODUCT.md),
[`DESIGN.md`](../../DESIGN.md), and the
[architecture explanation](../explanation/architecture.md).

## Question

Which existing projects should a unified, host-aware inference product reuse for
provider adaptation, agent-facing APIs, routing, and local model lifecycle, and
which responsibilities still need to be product-owned?

## Recommendation

Build one user-facing product, but keep three internal ownership boundaries:

1. **Controller:** product-owned discovery, recommendations, configuration,
   desired state, engine/application adapters, credentials references, and
   lifecycle orchestration.
2. **Gateway:** a generic client/provider compatibility data plane selected from
   an existing project. It owns wire protocols, upstream authentication,
   provider conversion, request routing, retries, and safe telemetry. It does
   not own engine installation or process lifecycle.
3. **Engine adapters:** engine-specific launch, readiness, capability discovery,
   model loading, unloading, and resource semantics. A shared supervisor may
   execute adapter plans, but the semantics remain engine-specific.

The components can ship and be operated as one product while remaining separate
processes and contracts. This avoids putting provider translation, process
supervision, and host activation into one failure domain.

Run a short conformance bake-off between **Bifrost** and **LiteLLM** before
choosing the default gateway. Bifrost is the provisional first candidate for a
local/private profile because it is a self-hosted Go gateway with an explicit
Codex Responses setup and request/response hooks. LiteLLM is the conservative
fallback because its provider and routing surface is broader and more mature.
Neither should be trusted for the existing Codex namespace behavior until it
passes the fixtures described below.

Reuse **llama-swap** as the first local lifecycle candidate, or reuse its process
contract if direct integration proves too constraining. Treat `llama-server`
router mode and vLLM as engine-specific backends, not the universal controller.
Keep Envoy AI Gateway as a cluster deployment profile; its new standalone CLI
is promising for development but is explicitly experimental.

Absorb the historical `codex-ns-proxy` transform into the gateway as a narrowly
scoped compatibility adapter only after conformance is proven. Its
`type: "namespace"` request,
history, and response round trip are not the same contract as an MCP gateway
prefixing tool names. Until the selected gateway can host that adapter without
semantic or credential regressions, retain the existing proxy as a sidecar.

## Responsibility boundary

| Responsibility | Owner | Reuse seam |
| --- | --- | --- |
| Host discovery and recommendations | Controller | Product logic |
| Profiles and user preferences | Controller | Product schema and UI |
| Install/activate engine or client integrations | Controller adapters | Host-specific adapters |
| Start, stop, health, idle unload | Engine adapter plus supervisor | Evaluate llama-swap |
| Engine-specific model/load semantics | Engine adapter | llama-server, vLLM, MLX, OptiQ |
| Agent-facing HTTP APIs | Gateway | Bifrost or LiteLLM |
| Provider schema conversion | Gateway | Bifrost or LiteLLM |
| Load balancing, retries, failover | Gateway | Existing gateway implementation |
| Caller and provider credential separation | Gateway, configured by controller | Virtual keys and stored provider keys |
| Codex namespace adaptation | Compatibility adapter | Port existing proven transform |
| Metrics and traces | Gateway plus controller | Metadata-only default; content opt-in |

## Candidate comparison

| Project | Lifecycle | Routing and APIs | Adaptation and extension | Operations, license, fit |
| --- | --- | --- | --- | --- |
| LiteLLM | Does not install or supervise inference engines | Proxy spans 100+ providers, OpenAI-compatible routes, Responses bridging, load balancing, retries, fallbacks, and virtual keys | Python provider adapters, callbacks, guardrails, and hooks; no source-backed proof of the required Codex namespace round trip | Python service; local OpenAI-compatible upstreams; MIT outside the separately licensed `enterprise/` tree; mature generic gateway candidate |
| Bifrost | Does not supervise engines | OpenAI, Anthropic, Gemini and provider routing; explicit Codex Responses setup; virtual keys can be separated from stored provider keys | Raw HTTP and typed request/response hooks; MCP tools use `clientName-toolName`, which is not the Codex namespace contract | Go; local NPX/Docker and SQLite; Apache-2.0; promising low-overhead local candidate, but dynamic plugins require a non-default build and exact platform/architecture/Go compatibility |
| llama-swap | Generic command lifecycle, health checks, TTL unload, model switching, concurrency groups, and eviction | Routes OpenAI-compatible requests, including Responses and Anthropic Messages, to selected local processes | Simple request filters and per-model environment; does not normalize unrelated provider protocols | Single Go binary and config; MIT; strongest direct overlap with the local supervisor, but its low-level command model needs a product-owned adapter/configuration layer |
| llama.cpp `llama-server` | GGUF-specific model load/unload, dynamic router mode, cache and presets | Chat Completions, Responses, Anthropic Messages, embeddings, rerank and related local endpoints | Jinja chat templates and model-specific tool parsing; not a generic provider or process plugin system | Fast C/C++; local/offline; MIT; use behind a GGUF adapter, not as the universal gateway |
| vLLM server and router | vLLM owns model serving; its separate router targets fleets of vLLM servers rather than arbitrary engine processes | Server exposes Responses, Chat Completions, Anthropic Messages and metrics; router focuses on high-throughput scheduling and load balancing | Tool-parser plugins and ASGI middleware in the server | Python/CUDA server plus Rust router; Apache-2.0; use in a vLLM deployment adapter, not as the cross-engine controller |
| Envoy AI Gateway | Does not own inference process lifecycle | Responses, Chat Completions, Anthropic Messages, provider fallback and policy through Envoy | External processor and Envoy extension points; MCP aggregation prefixes names but does not prove Codex namespace fidelity | Apache-2.0; strong Kubernetes data plane. Standalone `aigw run` works on macOS/Linux but is experimental and downloads/runs Envoy, so it is not yet the default personal-host path |

## Evidence by project

### LiteLLM

LiteLLM's proxy documents provider unification, authentication, spend tracking,
load balancing, and connection to local OpenAI-compatible servers in its
[proxy quick start](https://docs.litellm.ai/docs/proxy/quick_start). Its
[Responses API documentation](https://docs.litellm.ai/docs/response_api)
documents native and bridged Responses calls, streaming, fallbacks, load
balancing, and guardrails. The
[routing documentation](https://docs.litellm.ai/docs/proxy/load_balancing)
describes shuffle, least-busy, usage-, latency-, and cost-based strategies, with
Redis-backed shared state for multiple proxy instances.

The proxy has broad observability integrations and explicit redaction/disable
controls in its [logging documentation](https://docs.litellm.ai/docs/proxy/logging).
The code is [MIT-licensed outside the separately licensed enterprise
directory](https://github.com/BerriAI/litellm/blob/main/LICENSE). Its strengths
are provider normalization and mature routing, not inference process lifecycle.
The documented UI
[plugin system](https://docs.litellm.ai/docs/proxy/plugins) is also not evidence
that an arbitrary bidirectional Responses wire transform can be installed; that
must be demonstrated against the actual hook/provider extension surface.

### Bifrost

Bifrost documents a self-hosted multi-provider gateway, virtual keys, routing,
MCP integration, Prometheus, OpenTelemetry, and custom plugins in its
[overview](https://docs.getbifrost.ai/overview). It can run locally through NPX
or Docker, binds to `localhost` by default, and stores file/UI configuration and
logs under an application directory, according to the
[gateway setup guide](https://docs.getbifrost.ai/quickstart/gateway/setting-up).

The [Codex CLI guide](https://docs.getbifrost.ai/cli-agents/codex-cli) configures
Codex against Bifrost's OpenAI endpoint with `wire_api = "responses"` and
documents translation to non-OpenAI providers. The
[streaming guide](https://docs.getbifrost.ai/quickstart/gateway/streaming)
documents Responses-style event SSE. The OpenAI provider reference lists
[`previous_response_id`](https://docs.getbifrost.ai/providers/supported-providers/openai),
but this does not prove equivalent stored-continuation semantics for every
translated provider; the bake-off must test it.

[Virtual keys](https://docs.getbifrost.ai/features/governance/virtual-keys) are
distinct from HTTP identity and can restrict which stored provider keys are
eligible, giving the required caller/upstream credential boundary. Bifrost's
[plugin hooks](https://docs.getbifrost.ai/plugins/getting-started) can intercept
raw HTTP and typed requests/responses. Dynamic plugins are disabled in the
default build and use Go shared objects that must match the target OS,
architecture, and Go version, which makes a maintained fork or statically
linked product build more plausible than drop-in user plugins.

The [MCP filtering contract](https://docs.getbifrost.ai/mcp/filtering) names
tools as `clientName-toolName`; it is useful for collision avoidance but is not
the existing Codex `type: "namespace"` contract. Built-in observability captures
request and response content when enabled; the
[logging documentation](https://docs.getbifrost.ai/features/observability/default)
provides a `disable_content_logging` setting that must be on by default in a
private/local profile. Bifrost is
[Apache-2.0 licensed](https://github.com/maximhq/bifrost/blob/main/LICENSE).
Vendor latency claims are not independent evidence and should be reproduced on
the target hosts.

### llama-swap

The [llama-swap repository](https://github.com/mostlygeek/llama-swap) describes
a single Go binary that starts OpenAI-compatible local servers on demand,
switches models, and proxies OpenAI and Anthropic-style routes. Its
[configuration reference](https://github.com/mostlygeek/llama-swap/blob/main/docs/configuration.md)
documents launch and stop commands, `${PORT}`, readiness checks, TTLs,
concurrent-resource groups, aliases, environment, request filters, front API
keys, and peer upstream keys. That is a useful supervisor contract, but the
product must still supply safe presets, host-aware resource policy, and
engine-specific capability validation. The project is MIT-licensed.

### llama.cpp

The current
[`llama-server` README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
documents a lightweight C/C++ server with Responses, Chat Completions,
Anthropic Messages, embeddings, tool use, continuous batching, API keys, SSE
ping, metrics, and dynamic router mode. Router mode can discover GGUF models,
load and unload model instances, apply presets, and enforce a model limit. Its
Responses endpoint is described as conversion through Chat Completions, so the
full stored Responses lifecycle cannot be assumed.

The server's
[development scope](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README-dev.md)
keeps inference, API compatibility, and model management in scope while leaving
external agent loops and a third-party plugin API out. It is therefore an
excellent GGUF engine adapter and a poor generic controller boundary. llama.cpp
is MIT-licensed.

### vLLM

The vLLM
[online serving documentation](https://docs.vllm.ai/en/latest/serving/online_serving/)
lists Responses create/retrieve/cancel, Chat Completions, Anthropic Messages,
health, load, and Prometheus endpoints. Its
[tool-calling documentation](https://docs.vllm.ai/en/latest/features/tool_calling/)
describes model-specific parsers and a tool-parser plugin seam. These are
engine/API-server capabilities, not generic provider routing or arbitrary local
process supervision.

The separate [vLLM Router](https://github.com/vllm-project/router) is a Rust,
Apache-2.0 router for high-throughput vLLM deployments, including load balancing,
prefill/decode disaggregation, discovery, health, retries, circuit breakers, and
metrics. That is valuable inside a vLLM adapter or cluster profile but does not
replace the product controller or cross-provider gateway.

### Envoy AI Gateway

Envoy AI Gateway documents provider authentication, normalization, routing,
policy, and observability across an Envoy-based data plane and control plane in
its [architecture](https://aigateway.envoyproxy.io/docs/architecture/). Its
[supported endpoints](https://aigateway.envoyproxy.io/docs/capabilities/llm-integrations/supported-endpoints/)
include streaming and non-streaming Responses, function/MCP tools, multimodal
input, fallback, and load balancing.

The experimental [`aigw` CLI](https://aigateway.envoyproxy.io/docs/cli/) can run
the gateway locally on macOS or Linux without Kubernetes, and
[`aigw run`](https://aigateway.envoyproxy.io/docs/cli/aigwrun/) can route to local
OpenAI-compatible services. It still operates Envoy plus an external processor,
persists runtime state under XDG directories, and is positioned for local
testing and development. The project is Apache-2.0. This makes it a strong
cluster profile and a useful standalone experiment, not the current default
personal-host dependency.

## Screened alternatives

Portkey now documents cross-provider
[Open Responses](https://portkey.ai/docs/product/ai-gateway/responses-api), a
[Codex/Claude setup CLI](https://portkey.ai/docs/guides/coding-agents/agent-cli),
and a local MIT-licensed
[gateway](https://github.com/Portkey-AI/gateway). It is worth revisiting after
Gateway 2.0 stabilizes. The repository currently labels 2.0 pre-release, the
agent CLI requires a Portkey account, and public product documentation does not
cleanly establish which new agent and Responses capabilities have parity in the
current self-hosted OSS build. It is not a safer immediate default than the two
finalists.

TensorZero's [gateway overview](https://www.tensorzero.com/docs/gateway/)
documents a high-performance multi-provider gateway, but the current primary
documentation reviewed here does not establish an equivalent Codex/Responses
and namespace-adaptation contract. Its optimization/evaluation platform is a
different center of gravity, so it does not displace the finalists for this
local agent-facing product.

## Required decision spikes

Use the same captured fixtures and host for Bifrost and LiteLLM:

1. **Responses fidelity:** non-streaming and SSE event ordering, terminal events,
   cancellation/disconnect behavior, error envelopes, and continuation through
   explicit history and `previous_response_id`.
2. **Namespace fidelity:** `type: "namespace"` request flattening, prior-history
   flattening, tool-call reconstruction, collision handling, and streaming tool
   arguments. MCP name prefixing alone does not pass.
3. **Credential isolation:** a caller credential must never reach the provider;
   provider credentials must never appear in client-visible errors, logs,
   diagnostics, or child process environments that do not need them.
4. **Privacy:** no prompts, outputs, tool arguments, tool results, authorization
   headers, or raw provider bytes at rest by default; retain only bounded
   operational metadata.
5. **Local operations:** cold start, idle RSS, CPU, added first-token latency,
   streaming throughput, clean shutdown, offline restart, upgrade/rollback, and
   failure isolation on macOS arm64 and Linux.
6. **Lifecycle seam:** drive the same MLX, llama.cpp, and vLLM launch/readiness/
   idle-unload scenarios through llama-swap and through the current supervisor
   contract. Prefer direct reuse only if product-owned safety and resource policy
   can remain declarative and testable.

The bake-off should produce one default gateway decision, one lifecycle reuse
decision, and explicit fallback profiles. Do not begin a gateway rewrite or
merge the namespace sidecar before those decisions are accepted.
