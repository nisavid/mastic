# Runtime, model, and cache contracts

Research snapshot: 2026-07-15. This note compares current first-party
documentation and package metadata with the runtime packages installed on the
research machine. It does not treat model-card claims or filename heuristics as
proof that a model will load.

## Conclusions

1. A **Server Type** is a runtime family, not a model and not a port. The three
   relevant families have materially different lifecycle and capability
   contracts: `mlx_lm`, `mlx_vlm`, and `optiq`.
2. A server process has one listening endpoint and one resident base model at a
   time. `mlx_lm` and `mlx_vlm` can replace that resident model when a request
   names another model. That is hot replacement, not simultaneous residency.
   Simultaneously serving independent base models requires multiple processes
   and therefore multiple upstream ports. Same-model requests can be batched
   within one process when the selected runtime and generation mode permit it.
3. Hub discovery, local cache inventory, desired model assignments, and models
   advertised by a running endpoint are four different collections. A usable
   model-management surface must not collapse them into one `models` command.
4. Hugging Face metadata is authoritative for repository identity, immutable
   revision, files, declared tags, and publisher-supplied card data. It does not
   establish MLX runtime compatibility or a minimum-memory guarantee. Static
   configuration checks can produce an *inferred* compatibility result; an
   isolated load or serve probe is the authoritative runtime check.
5. The deployed runtime contract is already inconsistent. The managed install
   pins `mlx-optiq==0.2.15`, while mastic can emit `--max-context`,
   `--idle-timeout`, `--allow-model-switch`, and `--single-model`. None of those
   flags exists in installed OptiQ 0.2.15 or its installed `mlx-lm` 0.31.3
   delegate. Unknown OptiQ arguments are forwarded to `mlx_lm.server`, which
   also rejects them. The managed OptiQ definition currently sets
   `max_context`, so its prepared command is not valid for the pinned runtime.
   Current PyPI publishes `mlx-optiq` 0.3.3 and its documentation describes
   `--max-context` and `--idle-timeout`; runtime feature negotiation or a
   compatible version constraint is required.

## Evidence and version boundary

| Component | Installed | Current PyPI | Primary contract used |
| --- | ---: | ---: | --- |
| `mlx-lm` | 0.31.3 | 0.31.3 | Installed source and [MLX-LM server documentation](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md) |
| `mlx-vlm` | 0.6.3 | 0.6.4 | Installed source and [MLX-VLM documentation](https://github.com/Blaizzy/mlx-vlm) |
| `mlx-optiq` | 0.2.15 | 0.3.3 | Installed metadata/source and [current OptiQ CLI reference](https://mlx-optiq.com/docs/cli) |
| `huggingface-hub` / `hf` | 1.23.0 CLI; 1.22.0 inside some isolated runtime environments | 1.23.0 | Installed help and [Hugging Face Hub documentation](https://huggingface.co/docs/huggingface_hub/) |

The packages are independently versioned and can live in separate `uv tool`
environments. Discovering one executable on `PATH` says nothing about the
versions of its private dependencies. A manager therefore needs to report, at
minimum, the executable path, package version, Python version, supported
options, and compatibility result for each Server Type.

The local `--help` commands for MLX-LM and MLX-VLM fail in the research sandbox
because importing MLX tries to acquire a Metal device. Their installed Python
source was inspected instead. `optiq serve --help` succeeds and confirms the
0.2.15 flag set without starting a service.

## Runtime contracts

### MLX-LM

`mlx_lm.server` is a text-generation server. Its installed contract includes:

- a Hugging Face repository ID or local directory through `--model`;
- an optional single adapter path and optional draft model;
- prompt-cache sizing, decode/prompt concurrency, sampling defaults, and
  distributed pipeline or tensor execution;
- `GET /health`, `GET /v1/models`, `POST /v1/completions`, and
  `POST /v1/chat/completions`;
- no HTTP model-unload operation.

The loader downloads a repository snapshot when its argument is not a local
path. Compatibility is resolved from `config.json`: the installed loader maps
`model_type`, imports `mlx_lm.models.<model_type>`, builds the model, applies
quantization declared in configuration, loads `model*.safetensors`, then loads
the tokenizer. A repository can opt into a custom `model_file`, but executing
it requires explicit `trust_remote_code`; the default is refusal.

The server's `/v1/models` result is not a supported-model catalog. It scans the
local Hugging Face cache and admits a model only when the cached `main`
revision contains `config.json`, `model.safetensors.index.json`, and
`tokenizer_config.json`; it also adds the configured local path. This excludes
some valid single-file models and commit-only cached revisions, and can include
models whose configuration later fails to load. The endpoint is a convenient
cache heuristic.

The request body can select another model, adapter, or draft model. The
`ModelProvider` drops its current model references and loads the requested
tuple when the key changes. One target/draft tuple is resident through the
provider at a time. With a stable model, requests are batchable only when no
draft model is active and every prompt-cache component supports merging;
seeded requests also bypass batching.

### MLX-VLM

`mlx_vlm.server` is a FastAPI server for text, vision, audio, video, image
generation, and image editing, depending on the loaded model family. The
installed 0.6.3 contract includes:

- model preloading, a single adapter, vision-feature caching, continuous
  batching, uniform or TurboQuant KV cache, and DFlash/EAGLE3/Gemma-MTP draft
  families;
- OpenAI Chat Completions and Responses routes, Anthropic Messages routes,
  image generation/edit routes, `GET /health`, `GET /v1/models`, metrics, APC
  cache statistics/reset, and `POST /unload`;
- health data identifying the loaded model, adapter, native and effective
  context, tool parser, continuous batching, and APC state.

Its runtime state contains one `model_cache`. A request for a different
model/adapter/kind synchronously unloads the old model, clears generation and
vision/APC caches, then loads the new one. Independent simultaneous base models
again require independent server processes.

Compatibility is primarily the installed architecture registry. MLX-VLM reads
`model_type`, applies a small remapping table, and imports the corresponding
module below `mlx_vlm.models`; text-only configurations fall back to the text
loader. The installed tree contains many families, but module presence is only
an inferred result. Weight layout, processor construction, optional remote
code, and device memory can still make the actual load fail.

Like MLX-LM, `/v1/models` is a local-cache heuristic. MLX-VLM checks for
configuration, tokenizer configuration, and either an index or any
SafeTensors file, then adds the currently loaded model. It does not assert that
every listed model supports a particular modality or even loads successfully.

### OptiQ

`optiq serve` wraps and patches `mlx_lm.server`; it is not a fourth independent
model store. Installed 0.2.15 adds:

- uniform or per-layer mixed-precision KV cache;
- one or more resident LoRA adapters, selectable per request;
- OpenAI Responses and Anthropic Messages in addition to MLX-LM routes;
- bundled-head MTP or a separate drafter, which are mutually exclusive;
- automatic/explicit MoE expert streaming and optional local quant discovery;
- model variants such as `:think` and `:no-think`.

`--mtp` is capability-gated. The installed implementation refuses a model that
does not have an MTP head. Qwen3.5/Qwen3.6 models can declare an MTP sidecar;
Gemma uses a separate assistant drafter instead. A generic `optiq` label or
four-bit quantization tag is not enough to infer MTP support.

OptiQ inherits MLX-LM's request-time model replacement. Installed 0.2.15 can
extend `/v1/models` with locally built quants under `--models-dir`; cached Hub
models and the served model remain visible. Multiple adapters can stay
resident against one base model, but multiple independent base models do not.

Current OptiQ documentation adds memory-aware `--max-context`, idle unload,
resilient downloads, safe concurrency defaults, and prefix-cache behavior.
Those current docs must not be applied blindly to installed 0.2.15. The local
help is the authoritative option contract for that installed executable.

## Model discovery and acquisition

Hugging Face Hub already supplies the primitives needed for a first-class
model catalog:

- `hf models list` and `HfApi.list_models` search and filter by author, text,
  tags, pipeline task, parameter range, gated state, popularity, and selected
  expanded metadata;
- `model_info` and the repository file tree provide the current immutable SHA,
  declared library, pipeline task, base model, SafeTensors parameter summary,
  card metadata, configuration, and exact files/sizes;
- `hf download --dry-run` or `snapshot_download(..., dry_run=True)` reports the
  files and bytes missing locally without downloading them;
- `snapshot_download` accepts an immutable revision plus include/exclude
  patterns and returns the snapshot directory.

A managed acquisition should resolve a branch or tag to an immutable commit
SHA, show the license/gating state and projected bytes, then record that SHA.
The revision belongs to the local model installation identity. An unpinned
repository ID is a moving source reference, not a reproducible installation.

Model cards are publisher-authored documentation. Their YAML powers Hub search
and can declare `library_name`, `pipeline_tag`, `base_model`, license, datasets,
and free-form tags. Those fields are useful facts with clear provenance, but
free-form tags such as `mlx`, `optiq`, `4bit`, `conversational`, or
`apple-silicon` are not a validated runtime-capability schema.

The pinned
[`mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit`](https://huggingface.co/mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit/tree/70a3aa32c7feef511182bf16aa332f37e8d82014)
snapshot illustrates the richer facts that can be surfaced without loading
weights:

- immutable revision `70a3aa32c7feef511182bf16aa332f37e8d82014`;
- declared `mlx` library, text-generation pipeline, Apache-2.0 license, base
  `Qwen/Qwen3.6-35B-A3B`, and `qwen3_5_moe` architecture;
- five main weight shards, a bundled MTP sidecar, an OptiQ vision sidecar,
  `kv_config.json`, and publisher-recommended generation parameters;
- the configuration declares a 262,144-token native context and one MTP hidden
  layer; the card reports a 22.1 GB primary quant.

These declarations strongly suggest MLX-LM/OptiQ text compatibility, OptiQ MTP,
and OptiQ vision support. They do not prove that a particular installed runtime
version recognizes every sidecar, that the snapshot fits available memory, or
that its full native context is safe on a given Mac.

## Local cache contract

The Hugging Face cache is content-addressed and shared across tools. A cached
repository has refs, immutable snapshots, and shared blobs. `hf cache ls`
summarizes repositories; `hf cache ls --revisions` selects revision-level
aggregation with revision sizes, timestamps, and paths. `scan_cache_dir()` is
the source for file/blob-level inventory, refs, and corruption details. A model
manager needs the revision boundary rather than aggregate repository rows alone.

Management operations already exist:

- `hf cache verify` verifies checksums for one cached revision or local tree;
- `hf cache rm` calculates and applies deletion for repositories or revisions;
- `hf cache prune` removes detached revisions and incomplete downloads;
- the Python `HFCacheInfo.delete_revisions()` API produces a deletion strategy
  whose expected reclaimed space can be presented before execution.

mastic should call library APIs or reproduce their structured contract rather
than parse directory names. Cache removal needs reference awareness: multiple
revisions and repositories can share blobs, so apparent file sizes do not equal
reclaimable bytes.

Online and offline behavior is explicit. `HF_HUB_OFFLINE=1` or
`local_files_only=True` prevents Hub access. A commit SHA can be resolved
directly if its complete snapshot is cached; a branch or tag needs its cached
ref mapping. Current `huggingface-hub` also detects incomplete offline
snapshots and raises instead of silently returning a partial directory. An
offline UI should therefore distinguish *cached*, *complete and verified*,
*incomplete*, *corrupt*, and *unknown because metadata is unavailable*.

The research machine's offline cache inventory succeeded without network
access and reported repositories, revisions, sizes, and an incomplete download.
No cache mutation was performed.

## Capability and requirement model

The user-facing result should preserve evidence instead of manufacturing a
single `supported: true` bit.

| Fact | Best authority | Confidence |
| --- | --- | --- |
| Repository, revision, files, byte sizes | Hub API/file tree or local verified snapshot | Reported/verified |
| License, task, modalities, base model | Model card and config, with publisher shown | Reported |
| Architecture recognized by installed runtime | Installed registry plus remapping | Inferred |
| Required remote code | Config plus runtime policy | Inferred until load |
| MTP, drafter, OptiQ vision/KV sidecars | Exact config/files plus runtime version | Inferred until probe |
| Adapter compatibility | Adapter config, base-model identity, runtime version | Inferred until probe |
| Runtime compatibility | Isolated metadata/load probe using the selected executable | Verified |
| RAM/context feasibility | Weight bytes + architecture/KV estimate + live available memory | Estimate |
| Actual throughput and peak memory | Timed local serve/generate probe | Measured |

There is no universal authoritative minimum-RAM field. On-disk weights are a
lower bound, not a working-set prediction. KV cache grows with context and
concurrency; adapters, draft/MTP state, vision towers, prompt caches, temporary
load buffers, and non-model applications also consume unified memory. A useful
planner must show assumptions and headroom rather than promise that a model
"fits."

## Concurrent-use contract

Three distinct forms of concurrency must remain visible:

1. **Same model, many requests:** MLX-LM and MLX-VLM can batch compatible
   requests; every in-flight sequence still owns KV state, so context and
   concurrency multiply memory pressure.
2. **One endpoint, different requested models:** supported as lazy replacement
   by MLX-LM and MLX-VLM, inherited by OptiQ. A switch unloads/replaces the
   resident base and has a large latency and memory transition. It is not a
   promise of safe overlapping generations across model switches.
3. **Different models simultaneously:** requires separate Server Definitions
   and Server Runs, each with its own upstream endpoint and process. A stable
   client endpoint can front one run, but cannot route simultaneous independent
   models through one single-resident runtime without an explicit routing
   layer.

Ports therefore belong to Server Runs/endpoints, not Model Aliases. A Model
Alias can be assigned to several Server Definitions, and several Server
Definitions can use the same Server Type on different endpoints.

## Decisions exposed for the product map

This research makes the following questions sharp enough for separate
decisions:

- Is a managed server pinned to one Model Alias, a runtime gateway that permits
  expensive request-time model replacement, or an explicit mode chosen per
  Server Definition?
- Does mastic add `mlx_vlm` as a first-class Server Type, and how does automatic
  runtime selection handle models that both MLX-VLM and OptiQ can serve?
- What are the durable identities and lifecycle states for remote catalog
  entries, pinned local model installations, Model Aliases, and cache
  revisions?
- Which compatibility states and evidence must the CLI/TUI display before
  download, before assignment, and after an isolated verification probe?
- Does mastic own runtime installation/upgrades, merely diagnose external
  runtimes, or support both modes with an explicit provenance field?
- What version/feature negotiation prevents configuration options from being
  emitted to runtimes that do not implement them?
- What safety and confirmation policy governs model downloads, revision
  updates, cache deletion/pruning, remote-code trust, and gated repositories?
- How are capacity, endpoint allocation, and process topology presented when a
  user asks to serve multiple base models simultaneously?

## Primary sources

- [MLX-LM](https://github.com/ml-explore/mlx-lm) and its
  [server contract](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md)
- [MLX-VLM](https://github.com/Blaizzy/mlx-vlm)
- [OptiQ documentation](https://mlx-optiq.com/docs/),
  [CLI reference](https://mlx-optiq.com/docs/cli),
  [serving guide](https://mlx-optiq.com/docs/serve), and
  [MTP guide](https://mlx-optiq.com/docs/mtp)
- [Hugging Face Hub CLI](https://huggingface.co/docs/huggingface_hub/guides/cli),
  [downloads](https://huggingface.co/docs/huggingface_hub/guides/download),
  [cache management](https://huggingface.co/docs/huggingface_hub/guides/manage-cache),
  and [model cards](https://huggingface.co/docs/hub/model-cards)
