# OptiQ 0.3.3 sampling compatibility

Research snapshot: 2026-07-16. Repository context: `systools` commit
`ad56574aeb728522b937cac39ea6d740f36b19f2`.

This note records which sampling controls the pinned OptiQ runtime accepts and
actually carries through each API and generation path. The companion
[`qwen36-sampling-profiles.md`](qwen36-sampling-profiles.md) records the
model-developer profiles and their provenance.

## Runtime provenance

The tested bundle lock at
`tools/mastic/src/mastic/runtime_definitions/locks/optiq.lock` pins
`mlx-optiq==0.3.3` and `mlx-lm==0.31.3` exactly.

- Published OptiQ source artifact: `mlx_optiq-0.3.3.tar.gz`, SHA-256
  `2a6e78702fb5d444ed19d206f883ccf9983d32dd0ff23e69ca0656076a5aa3cf`.
  See the [PyPI release](https://pypi.org/project/mlx-optiq/0.3.3/) and its
  [immutable source artifact](https://files.pythonhosted.org/packages/c0/f2/6a3922cf967bd57691a549bb7eb0432be9a40299b50b1c85351e2737d35f/mlx_optiq-0.3.3.tar.gz).
- OptiQ's [public changelog](https://mlx-optiq.com/changelog) says the source
  lives in a private monorepo, so the PyPI artifact is the public immutable
  source record; no public source commit exists for version 0.3.3.
- The relevant mlx-lm tag is
  [`v0.31.3`](https://github.com/ml-explore/mlx-lm/tree/ed1fca4cef15a824c5f1702c80f70b4cffc8e4dd),
  commit `ed1fca4cef15a824c5f1702c80f70b4cffc8e4dd`.

OptiQ source paths and line numbers below refer to the published 0.3.3 source
artifact. mlx-lm references use public immutable GitHub links.

## Qwen profile projection

At immutable Qwen commit `995ad96eacd98c81ed38be0c5b274b04031597b0`, the
model card specifies three profiles:

| Mode | `temperature` | `top_p` | `top_k` | `min_p` | `presence_penalty` | `repetition_penalty` | Thinking |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Thinking, general | 1.0 | 0.95 | 20 | 0.0 | 1.5 | 1.0 | Default/on |
| Thinking, precise coding | 0.6 | 0.95 | 20 | 0.0 | 0.0 | 1.0 | Default/on |
| Instruct/non-thinking | 0.7 | 0.80 | 20 | 0.0 | 1.5 | 1.0 | `chat_template_kwargs.enable_thinking=false` |

See the Qwen model card's
[profile summary](https://huggingface.co/Qwen/Qwen3.6-35B-A3B/blob/995ad96eacd98c81ed38be0c5b274b04031597b0/README.md#L661-L670)
and
[Best Practices](https://huggingface.co/Qwen/Qwen3.6-35B-A3B/blob/995ad96eacd98c81ed38be0c5b274b04031597b0/README.md#L998-L1010).
The card says Qwen3.6 thinks by default and uses
`chat_template_kwargs={"enable_thinking": false}` for non-thinking requests;
the older `/think` and `/nothink` soft switch is not supported. See the
[non-thinking example](https://huggingface.co/Qwen/Qwen3.6-35B-A3B/blob/995ad96eacd98c81ed38be0c5b274b04031597b0/README.md#L787-L834).

The exact target quant at revision
`70a3aa32c7feef511182bf16aa332f37e8d82014` already contains the non-thinking
profile in its
[`generation_config.json`](https://huggingface.co/mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit/blob/70a3aa32c7feef511182bf16aa332f37e8d82014/generation_config.json#L1-L15):
`temperature=0.7`, `top_p=0.8`, `top_k=20`, `min_p=0.0`,
`repetition_penalty=1.0`, and `presence_penalty=1.5`.

## Boot-time `generation_config.json` handling

In the OptiQ 0.3.3 source artifact:

- `optiq/runtime/gen_config.py:23-59` reads only `temperature`, `top_p`,
  `top_k`, `min_p`, and `repetition_penalty`. It does not read
  `presence_penalty`, despite the later docstring claiming that it does.
- `optiq/runtime/gen_config.py:103-147` forwards only the first four as
  `--temp`, `--top-p`, `--top-k`, and `--min-p`. It reads but does not forward
  `repetition_penalty`; mlx-lm exposes no server CLI flag for either penalty.
  Explicit CLI flags win.
- `optiq/cli.py:1866-1884` applies this merge automatically when `optiq serve`
  starts.

For the pinned quant, an explicit mastic `temperature=0.0` therefore prevents
the model's `0.7` from being selected. OptiQ still adds `top_p=0.8`,
`top_k=20`, and `min_p=0.0` when those flags are absent. It does not install
`presence_penalty=1.5` or `repetition_penalty=1.0` as service defaults.

## `/v1/chat/completions`

The direct Chat Completions endpoint comes from mlx-lm 0.31.3. It accepts and
validates all six requested fields:

- [request extraction and defaults](https://github.com/ml-explore/mlx-lm/blob/ed1fca4cef15a824c5f1702c80f70b4cffc8e4dd/mlx_lm/server.py#L1160-L1193);
- [validation](https://github.com/ml-explore/mlx-lm/blob/ed1fca4cef15a824c5f1702c80f70b4cffc8e4dd/mlx_lm/server.py#L1229-L1247);
- [generation argument propagation](https://github.com/ml-explore/mlx-lm/blob/ed1fca4cef15a824c5f1702c80f70b4cffc8e4dd/mlx_lm/server.py#L1376-L1406);
- [sampler construction](https://github.com/ml-explore/mlx-lm/blob/ed1fca4cef15a824c5f1702c80f70b4cffc8e4dd/mlx_lm/server.py#L399-L411); and
- [penalty processor construction](https://github.com/ml-explore/mlx-lm/blob/ed1fca4cef15a824c5f1702c80f70b4cffc8e4dd/mlx_lm/server.py#L414-L423).

When a request omits them:

- `temperature`, `top_p`, `top_k`, and `min_p` inherit the service CLI values.
  mlx-lm's unmodified CLI defaults are `0.0`, `1.0`, `0`, and `0.0`.
- `repetition_penalty` and `presence_penalty` default to `0.0`, each with a
  20-token context.
- `chat_template_kwargs` defaults to `None` and is merged over global
  `--chat-template-args` during tokenization.

`do_sample` is not extracted. Temperature zero is the effective greedy switch.
At temperature zero,
[`make_sampler`](https://github.com/ml-explore/mlx-lm/blob/ed1fca4cef15a824c5f1702c80f70b4cffc8e4dd/mlx_lm/sample_utils.py#L10-L60)
immediately returns argmax, so `top_p`, `top_k`, and `min_p` have no effect.

## `/v1/responses`

OptiQ translates a Responses request to Chat Completions before generation. In
the 0.3.3 source artifact:

- `optiq/responses_shim.py:274-309` forwards only `temperature`, `top_p`, the
  non-standard `top_k`, and `chat_template_kwargs`.
- It does not forward `min_p`, `presence_penalty`, `repetition_penalty`, or
  `do_sample`.
- `preserve_thinking` works only inside `chat_template_kwargs`, not as a
  top-level request field.
- OptiQ accepts that nested field, but `output_items_to_input_items()` drops
  Responses reasoning items before `previous_response_id` replay. It therefore
  cannot preserve historical Codex reasoning end to end in version 0.3.3.
- `optiq/responses_server.py:165-198` then extracts the translated fields.
  Because the other fields are absent, `min_p` falls back to the service CLI
  value and both penalties become `0.0`.

The Qwen precise-coding profile is representable through Responses during
ordinary mlx-lm generation: send `temperature=0.6`, `top_p=0.95`, and
`top_k=20`, and keep the service `min_p=0.0`. The ignored
`presence_penalty=0.0` and `repetition_penalty=1.0` are mathematically neutral,
so their omission does not alter decoding. The thinking-general and
non-thinking profiles require `presence_penalty=1.5`, which the OptiQ 0.3.3
Responses shim cannot carry per request.

## `--mtp` execution-path risks

The OptiQ 0.3.3 MTP adapter has two important source-level compatibility risks:

1. `optiq/serve.py:229-317` patches only `mlx_lm.server.stream_generate`. The
   patch looks for raw `temperature`, `top_p`, `top_k`, and `min_p` keyword
   arguments and ignores `logits_processors`.
2. mlx-lm's
   [single-request path](https://github.com/ml-explore/mlx-lm/blob/ed1fca4cef15a824c5f1702c80f70b4cffc8e4dd/mlx_lm/server.py#L959-L988)
   calls `stream_generate` with a compiled `sampler` and compiled
   `logits_processors`, not raw sampling values. When the patched single path
   is used, OptiQ therefore sees none of the raw values and falls back to
   `temperature=0`, `top_p=0`, `top_k=0`, and `min_p=0`; it also drops both
   penalties.
3. Normal no-seed text requests on a batchable model use mlx-lm's
   [`BatchGenerator` path](https://github.com/ml-explore/mlx-lm/blob/ed1fca4cef15a824c5f1702c80f70b4cffc8e4dd/mlx_lm/server.py#L685-L829),
   which bypasses `stream_generate` entirely. In that path the sampler and
   penalties work, but the MTP patch is also bypassed.

The target model's actual cache batchability must be verified live before
claiming which path is active. Configuring correct profiles is necessary but
does not prove that MTP generation uses them. Acceptance should prove both the
effective sampler and actual MTP counters or path, or disable MTP for the
sampling acceptance run.

## Request projection

- **Codex:** select Qwen's thinking precise-coding profile:
  `temperature=0.6`, `top_p=0.95`, `top_k=20`, `min_p=0.0`,
  `presence_penalty=0.0`, and `repetition_penalty=1.0`; leave thinking enabled.
  Project `temperature`, `top_p`, and `top_k` through Responses and retain
  `min_p=0.0` as the service default. Preserve all six values and provenance in
  desired state even though neutral fields need not appear in the wire body.
- **Hindsight:** select an upstream profile by operation mode. Direct,
  non-thinking operations map to `temperature=0.7`, `top_p=0.8`, `top_k=20`,
  `min_p=0.0`, `presence_penalty=1.5`, `repetition_penalty=1.0`, plus
  `chat_template_kwargs.enable_thinking=false`. An operation explicitly
  contracted to reason maps to the thinking-general profile. Direct Chat
  Completions can carry all six values.
- Do not model `do_sample` as an effective OptiQ 0.3.3 API field.
- Do not rely on boot-time automatic ingestion for either penalty.
- Preserve the base-model commit, quant revision, OptiQ artifact version and
  hash, profile name, and thinking mode as profile provenance.
