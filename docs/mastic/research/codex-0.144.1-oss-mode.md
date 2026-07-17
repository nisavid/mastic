# Codex 0.144.1 OSS mode

This note records the exact behavior of the Codex version inspected for the
mastic integration. The installed binary reported `codex-cli 0.144.1`; the
source checkpoint is OpenAI's
[`rust-v0.144.1`](https://github.com/openai/codex/tree/rust-v0.144.1).

## What `--oss` changes

The TUI and noninteractive entrypoints resolve `--local-provider` or the
configured `oss_provider`, override `model_provider` with that ID, choose a
default model only for the built-in `ollama` and `lmstudio` IDs, set
`show_raw_agent_reasoning=true`, and run provider readiness. Readiness is a
no-op for other provider IDs, so a custom `mlx-local` provider remains
valid. See the exact [TUI bootstrap](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/tui/src/lib.rs#L989-L1057),
[exec bootstrap](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/exec/src/lib.rs#L379-L445),
and [OSS utilities](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/utils/oss/src/lib.rs#L8-L38).

The flag does not select different model instructions, rewrite Responses
requests, flatten tools, or change provider capabilities. Both built-in OSS
providers use the ordinary Responses-compatible provider metadata. All custom
and OSS providers use `ConfiguredModelProvider`, whose default capabilities
leave namespace tools enabled. See the
[provider catalogue](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/model-provider-info/src/lib.rs#L430-L526)
and [configured-provider implementation](https://github.com/openai/codex/blob/rust-v0.144.1/codex-rs/model-provider/src/provider.rs#L215-L270).

## mastic policy

mastic writes `oss_provider = "mlx-local"` alongside the managed model and
provider. Users can run `codex --oss` to make the open-weight intent explicit
and show raw reasoning without reselecting the endpoint or model.

This does not replace `responses-adapter` when namespace adaptation is required.
The proxy's original compatibility job is to flatten Codex namespace tools and
reconstruct their calls. OSS mode still sends namespace tools because Codex
0.144.1 exposes no per-custom-provider capability override. The proxy's
authentication separation and SSE heartbeat behavior are also independent of
OSS mode.
