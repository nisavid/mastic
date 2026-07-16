# Qwen3.6 sampling profiles for the OptiQ quant

Research snapshot: 2026-07-16. Repository context: `systools` commit
`ad56574aeb728522b937cac39ea6d740f36b19f2`.

## Question

Which sampling parameters do the model developers recommend for
`mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit`, and which profile fits coding,
agentic, general-thinking, and non-thinking clients?

This note uses first-party model repositories, model cards, configuration
files, and developer documentation. It records recommendations and conflicts;
it does not define the mlxctl configuration schema or select a thinking mode
for a client operation.

## Finding

The authoritative base-model card defines three complete profiles. None uses
`temperature=0.0`.

| Upstream profile | `temperature` | `top_p` | `top_k` | `min_p` | `presence_penalty` | `repetition_penalty` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Thinking, general tasks | 1.0 | 0.95 | 20 | 0.0 | 1.5 | 1.0 |
| Thinking, precise coding tasks such as WebDev | 0.6 | 0.95 | 20 | 0.0 | 0.0 | 1.0 |
| Instruct, or non-thinking | 0.7 | 0.80 | 20 | 0.0 | 1.5 | 1.0 |

Source: the Qwen team's pinned
[`Qwen/Qwen3.6-35B-A3B` model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B/blob/995ad96eacd98c81ed38be0c5b274b04031597b0/README.md#best-practices),
revision `995ad96eacd98c81ed38be0c5b274b04031597b0`. The same card presents the
profiles next to its OpenAI-compatible API examples and warns that framework
support varies. In those examples, `temperature`, `top_p`, and
`presence_penalty` are ordinary request fields, while `top_k` and chat-template
options are sent in `extra_body`.

The explicit `min_p=0.0` and `repetition_penalty=1.0` values should remain part
of a faithfully ingested profile. Omitting them loses the distinction between
a developer-declared value and an inference-framework default.

## Model and quant provenance

| Artifact | Immutable revision | Relevant evidence |
| --- | --- | --- |
| Base model | [`Qwen/Qwen3.6-35B-A3B@995ad96eacd98c81ed38be0c5b274b04031597b0`](https://huggingface.co/Qwen/Qwen3.6-35B-A3B/tree/995ad96eacd98c81ed38be0c5b274b04031597b0) | Qwen model card, generation config, and chat template |
| OptiQ quant | [`mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit@70a3aa32c7feef511182bf16aa332f37e8d82014`](https://huggingface.co/mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit/tree/70a3aa32c7feef511182bf16aa332f37e8d82014) | Declares the Qwen model as its base and points to the OptiQ Qwen3.6 family guide for sampling defaults |

The quant card describes the artifact as a mixed-precision quant of the exact
Qwen model family; it does not claim that quantization introduces a different
complete set of per-task sampling profiles. The OptiQ
[`Qwen3.6 family guide`](https://mlx-optiq.com/docs/qwen3.6), checked on
2026-07-16, recommends:

- a “strong reasoning baseline” of `temp=0.6`, `top_p=0.95`, `top_k=20`; and
- a “conversational” sampler of `temp=0.7`, `top_p=0.9`.

The guide also uses `--temp 0.6 --top-p 0.95` in its long-context serving
example. The published site does not expose an immutable source revision, and
these short forms omit `min_p`, `presence_penalty`, and
`repetition_penalty`. Its reasoning baseline exactly matches the three core
sampling values in Qwen's **precise-coding thinking** profile, not Qwen's
**general-thinking** profile. Its conversational `top_p=0.9` does not match
Qwen's non-thinking `top_p=0.8`. The Qwen card is therefore the stronger source
for complete, mode-specific profiles; the OptiQ guide is supporting evidence
that the quant is intended to use nonzero stochastic sampling.

## Embedded defaults are not a universal profile

The base model's pinned
[`generation_config.json`](https://huggingface.co/Qwen/Qwen3.6-35B-A3B/blob/995ad96eacd98c81ed38be0c5b274b04031597b0/generation_config.json)
contains `do_sample=true`, `temperature=1.0`, `top_p=0.95`, and `top_k=20`.
Those values are the general-thinking core, but that file omits the card's
explicit `min_p`, `presence_penalty`, and `repetition_penalty` values.

The quant's pinned
[`generation_config.json`](https://huggingface.co/mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit/blob/70a3aa32c7feef511182bf16aa332f37e8d82014/generation_config.json)
instead contains the full non-thinking profile:
`do_sample=true`, `temperature=0.7`, `top_p=0.8`, `top_k=20`, `min_p=0.0`,
`presence_penalty=1.5`, and `repetition_penalty=1.0`. That change entered the
quant repository in commit
[`63d520640ca7461f31ba66104612135770090340`](https://huggingface.co/mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit/commit/63d520640ca7461f31ba66104612135770090340),
whose title says it added Qwen-recommended sampling defaults.

That embedded quant default cannot safely stand for all uses. The pinned
[`chat_template.jinja`](https://huggingface.co/mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit/blob/70a3aa32c7feef511182bf16aa332f37e8d82014/chat_template.jinja)
starts a thinking block unless `enable_thinking` is explicitly false. Thus the
quant currently combines a default-thinking chat template with non-thinking
sampling values. A profile importer must ingest named profiles from the model
card and bind one to an explicit client mode instead of treating
`generation_config.json` as one mode-independent truth.

## Thinking and agentic controls

Qwen3.6 thinks by default. The model card's non-thinking example sets
`chat_template_kwargs={"enable_thinking": false}`; Qwen says the older `/think`
and `/nothink` soft switch is not officially supported. Sampling values alone
therefore do not select non-thinking behavior.

For multi-turn agents, Qwen separately documents
`chat_template_kwargs={"preserve_thinking": true}`. The official
[`Qwen3.6-35B-A3B release post`](https://qwen.ai/blog?id=qwen3.6-35b-a3b)
recommends preserving prior thinking content for agentic tasks, and the pinned
model card says this can improve decision consistency and KV-cache use. This
is a conversation-history/template control, not a fourth sampling profile.
The exact quant template retains current tool-loop thinking by default;
`preserve_thinking=true` additionally retains assistant reasoning from before
the latest real user message.

The developer sources do **not** publish a distinct numeric “tool calling” or
generic “agentic” sampler. Tool use remains in thinking mode by default, and a
coding agent maps most directly to the precise-coding thinking profile. Tool
protocol support and thinking-history preservation are additional requirements
outside the six sampling numbers.

## Client-profile implications

- **Codex:** the direct developer-labelled fit is the precise-coding thinking
  profile: `temperature=0.6`, `top_p=0.95`, `top_k=20`, `min_p=0.0`,
  `presence_penalty=0.0`, `repetition_penalty=1.0`. Agentic sessions should
  preserve thinking only if the full client/Gateway/runtime protocol can carry
  historical reasoning content correctly.
- **Hindsight:** no Qwen or OptiQ source names Hindsight or assigns one sampler
  to all memory operations. An operation explicitly using non-thinking output
  maps to the complete instruct profile. An operation explicitly using
  thinking for broad reflection maps to the general-thinking profile. Choosing
  between those modes is a Hindsight operation-contract decision, not a model
  metadata fact.

The sources do not support the existing idea of separate arbitrary Hindsight
temperatures such as `0.0`, `0.1`, or `0.9` as Qwen3.6 developer profiles.
If mlxctl retains operation-specific profile names, each name should resolve to
one of the three ingested upstream profiles plus an explicit thinking-mode
control, rather than modifying individual numeric fields ad hoc.

## Implementation questions at research time

1. Which Hindsight operations run in thinking mode, and which deliberately run
   in non-thinking mode? The model sources cannot answer this.
2. Does every request path preserve all six fields, including OpenAI extension
   fields such as `top_k` and framework-dependent penalties, or must the
   Gateway translate them?
3. Can Codex and the Responses adapter preserve historical reasoning content
   without exposing or dropping it? If not, `preserve_thinking` should not be
   claimed as active.
4. Should ingestion preserve the source revision and profile provenance so a
   future model update can be reviewed instead of silently replacing local
   behavior?

## Implemented mlxctl binding

mlxctl keeps all three upstream profiles as exact-revision knowledge. Guided
setup binds Codex `coding` to precise-coding thinking; Hindsight verification,
retain, and consolidation to non-thinking; and Hindsight reflect to general
thinking. This is an mlxctl workload policy, not a claim that Qwen publishes
Hindsight-specific recommendations.

Client-specific Gateway profile endpoints enforce the selected values. The
Codex Responses projection sends `temperature`, `top_p`, `top_k`, and the
thinking template control; its omitted numeric values are neutral in the
precise-coding profile. Hindsight uses Chat Completions, which carries the full
six-value profile and thinking control. The adjacent
[`optiq-0.3.3-sampling-compatibility.md`](optiq-0.3.3-sampling-compatibility.md)
records the exact transport evidence and remaining MTP acceptance risk.

The Gateway can project `preserve_thinking` inside `chat_template_kwargs`, but
guided setup leaves it unset for every current workload. OptiQ 0.3.3 drops
Responses reasoning items while replaying `previous_response_id`. Hindsight
0.8.4 reflect begins with one user turn, stores assistant tool calls without
reasoning content, and therefore has no older reasoning trace to preserve.
Enable preservation for a future integration only after its adapter retains
and replays reasoning from assistant turns preceding a newer user query.
