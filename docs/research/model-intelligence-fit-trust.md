# Model intelligence, machine fit, and trust signals

Research for mastic model discovery and cache management. This note defines
which facts can be presented as observed, declared, derived, or validated. It
does not define the final CLI or TUI.

The research used public Hub metadata and small repository metadata files. It
did not download model weights or execute repository code. Sources were
checked on 2026-07-15.

## Conclusions

1. A model repository is not a runtime compatibility contract. Hub
   `library_name`, tags, and `pipeline_tag` are useful discovery declarations,
   but the publisher or Hub may supply them. A matching name, organization, or
   `mlx` tag must not be rendered as verified compatibility.
2. Compatibility is a relation among an exact model revision, an exact Server
   Type and runtime version, selected options, and the machine. There is no
   useful context-free `compatible: true` field.
3. Repository byte size is good for remote storage and download planning, but
   not for unified-memory fit. A fit estimate must separately account for
   selected weights, auxiliary weights, KV/state caches, concurrency,
   transient runtime overhead, and operating-system headroom.
4. `safetensors` avoids pickle's code-execution format, but it does not make a
   whole repository trustworthy. Custom Python, tokenizer remote code,
   dependencies, templates, runtime packages, and incomplete security scans
   remain separate signals.
5. Mutable references must be resolved to a commit SHA before inspection,
   download, verification, or launch. The resolved SHA and runtime version are
   part of every compatibility and fit result.
6. Remote discovery, local cache inventory, cache integrity, and successful
   launch are different observations. The interface should preserve those
   distinctions instead of collapsing them into “installed” or “available.”

## Evidence vocabulary

Every nontrivial field should carry an evidence state, source, observed
revision, and observation time. Absence is `unknown`, not `false`.

| State | Meaning | Examples |
| --- | --- | --- |
| **Observed** | Read directly from an API, exact-revision file, package metadata, or local machine without executing model code. | resolved SHA, file byte size, `config.json` `model_type`, cached revisions, machine memory |
| **Declared** | Asserted by a publisher or repository metadata; useful but not independently proven. | intended task, language, license, base model, coding/tool-use capability, `library_name`, tags |
| **Derived** | Calculated from named observed inputs and explicit assumptions. | download bytes remaining, static-weight lower bound, KV estimate, fit band |
| **Validated** | Confirmed by a checksum/integrity operation or a bounded runtime probe for the exact revision and runtime. | cache verification passed, runtime loaded the model, chat template rendered, smoke request succeeded |
| **Conflicting** | Sources disagree; preserve both and explain the conflict. | card says 32K context while config declares 128K |
| **Unknown** | No reliable evidence is available or a required source could not be checked. | tool calling inferred only from a model name |

“Authoritative” describes a source's authority over a fact, not whether the
fact is safe. The Hub is authoritative for the current repository SHA and file
metadata. The model publisher is authoritative for its own declarations. The
selected runtime version is authoritative for the architectures its code can
load. None of those sources alone proves good behavior or safe machine fit.

## Safe remote inventory

### Repository envelope

[`HfApi.model_info`](https://huggingface.co/docs/huggingface_hub/main/en/package_reference/hf_api#huggingface_hub.HfApi.model_info)
can return the repository SHA, author, access state, model-card data, library,
pipeline tag, Hub-computed tags, safetensors summary, security status, and
siblings. Request one repository with `files_metadata=True` and
`securityStatus=True`; those options cannot be combined with `expand`.

Record at least:

- the user-supplied Model Reference and requested revision;
- canonical repository ID and resolved commit SHA;
- `private`, `gated`, and `disabled` state;
- author, creation and last-modified times;
- card data, `library_name`, `pipeline_tag`, tags, and declared base models;
- `safetensors` parameter counts by dtype;
- security scan completion and every reported file issue;
- each file's path, byte size, Git blob ID, and LFS SHA-256 when present.

The [ModelInfo contract](https://huggingface.co/docs/huggingface_hub/main/en/package_reference/hf_api#huggingface_hub.ModelInfo)
explicitly says most fields are optional and list queries return less detail
than a repository-specific query. A missing list field is therefore unknown.
Hub-computed tags also contain more than the model card's own tags, so retain
the two sources separately.

Resolve a branch or tag to `ModelInfo.sha`, then use that SHA for every
subsequent request. Store both the requested mutable name and resolved SHA so a
later refresh can report drift instead of silently changing the model.

### Bounded exact-revision files

Fetch only a small allowlist, from the resolved SHA, with a strict response
size limit, content-type check, JSON depth/collection limits, and timeout:

- `config.json`;
- `generation_config.json`;
- `tokenizer_config.json` and `chat_template.jinja`;
- `processor_config.json`, `preprocessor_config.json`, and modality-specific
  processor configs when listed;
- `model.safetensors.index.json`;
- runtime-specific declarative metadata such as `kv_config.json` and
  `optiq_metadata.json`;
- `README.md` only when model-card prose is requested.

Parse JSON and YAML as data. Treat Jinja as displayable text until a trusted
runtime renders it in a deliberate validation step. Never import a repository
module during inspection.

[`get_safetensors_metadata`](https://huggingface.co/docs/huggingface_hub/main/en/package_reference/hf_api#huggingface_hub.get_safetensors_metadata)
can parse tensor headers and indexes for a pinned revision without obtaining
the full tensor payload. Its parameter counts, dtypes, tensor names, shapes,
and file map are stronger structural evidence than filename conventions. It
only recognizes the conventional root `model.safetensors` or
`model.safetensors.index.json`; auxiliary tensor files must still be found in
the repository inventory.

### Search and catalog

[`HfApi.list_models`](https://huggingface.co/docs/huggingface_hub/main/en/package_reference/hf_api#huggingface_hub.HfApi.list_models)
supports author, text, pipeline, parameter-count, and tag filters. It is
suitable for browse/search pages, including `author="mlx-community"`, but its
results are discovery candidates, not a verified compatibility catalog.

Show filters in their source language:

- “published by mlx-community,” not “official MLX model”;
- “publisher/Hub tag: mlx,” not “works with mlx_lm”;
- “declared task: text-generation,” not “proven text generator”;
- download and like counts as popularity, never as trust or quality.

Capability claims such as coding, reasoning, tool use, RAG, agentic use,
vision, audio, or long-context quality should come from model-card declarations
or named evaluation results. The Hub supports evaluation entries with dataset
revision, source, notes, and optional verification tokens. Preserve that
provenance. Do not infer capabilities from a repository name, parameter count,
architecture family, chat template, or popularity.

## Runtime compatibility

Compatibility should be reported per Server Type, runtime version, model
revision, and option set with `supported`, `unsupported`, or `unknown` plus
evidence. “Candidate” is a useful pre-validation state; “validated” requires a
successful bounded load or request.

### `mlx_lm`

The current MLX-LM loader reads `config.json.model_type`, applies its internal
remapping, and imports the corresponding
`mlx_lm.models.<model_type>` module. If the module is absent it reports that
the model type is unsupported. This behavior and the concrete architecture
modules are visible in the exact
[`mlx-lm` source revision](https://github.com/ml-explore/mlx-lm/tree/15b522f593b7ca5fbc0cac6f7572d40859d2d8fe/mlx_lm/models).

For a selected installed version, a static preflight can therefore:

1. read `model_type` from the exact-revision config;
2. apply the installed runtime's explicit remapping table;
3. check for its built-in architecture module without importing model code;
4. inspect quantization keys against that runtime's supported loaders;
5. flag `model_file`, `auto_map`, Python files, or tokenizer remote-code
   requirements.

A module match is strong evidence that the runtime recognizes the architecture,
but not proof that this particular conversion, tokenizer, quantization layout,
or weights load successfully. MLX-LM's own documentation also notes that some
tokenizers require remote code and exposes an explicit trust option in its
[supported-model guidance](https://github.com/ml-explore/mlx-lm/blob/15b522f593b7ca5fbc0cac6f7572d40859d2d8fe/README.md#supported-models).

### `optiq`

An OptiQ candidate needs more than an `optiq` tag. Inspect the exact-revision
config, `optiq_metadata.json`, `kv_config.json`, auxiliary tensor paths, and
the installed `mlx-optiq` package version. Confirm that referenced auxiliary
files exist and that configuration layer indexes and tensor filenames are
internally consistent. OptiQ compatibility remains candidate-level until its
runtime loads the pinned revision with the selected KV configuration and MTP
choice.

[PyPI metadata](https://pypi.org/project/mlx-optiq/) is authoritative for a
published `mlx-optiq` artifact's version, dependency declarations, upload
time, yanked state, and artifact digests. Pin the version and artifact hash in
managed installation state. A package's declared dependency range does not
prove that every allowed combination works.

### Future multimodal Server Types

`mlx-vlm` is a separate runtime with its own architecture registry, processor
requirements, modality behavior, and remote-code option. Its project describes
text, image, audio, and video support in the
[`mlx-vlm` source](https://github.com/Blaizzy/mlx-vlm/tree/7fbc7bc9283ac34cc83ce526a3e6e76b4278acde).
If mastic adds an `mlx_vlm` Server Type, compatibility must be computed from
that installed version's registry and processor contract. A vision config or
image token in a repository is evidence of model structure, not evidence that
`mlx_lm` or `optiq` exposes a working vision API.

### Local-path Model References

For a local directory, inventory only files under the resolved directory;
reject traversal outside it. Compute file sizes and cryptographic hashes for
small metadata and executable files. For large tensors, hash only as an
explicit integrity operation because it may be expensive. If the directory is
a Hub snapshot, recover the cached commit SHA where possible. Otherwise show
the origin and revision as unknown unless mastic previously recorded them.

## Architecture, modalities, quantization, and context

| Concern | Strongest safe pre-load evidence | Important limit |
| --- | --- | --- |
| Architecture | exact-revision `config.json` `model_type` and nested text/vision config, matched against the selected runtime version | publisher-controlled config can be malformed or incompatible |
| Modalities | processor configs, nested vision/audio config, special modality tokens, and declared pipeline/card data | presence does not prove the selected server exposes that modality |
| Quantization | config quantization tables plus safetensors tensor dtypes/shapes and runtime-specific metadata | filename and `4bit` tag are declarations only; mixed precision cannot be summarized by one bit width |
| Context ceiling | model/tokenizer config values, runtime maximum, server option, and KV policy shown separately | the usable ceiling is the minimum of several limits and may still be impractical |
| Chat/tool formatting | tokenizer class and chat-template presence; bounded render validation | a template does not prove instruction following or tool-use quality |
| Base model | model-card `base_model` plus exact-revision conversion metadata | lineage is a publisher declaration unless independently reproduced |

When values disagree, show each named source. Do not silently choose the
largest context length or collapse a mixed-precision quantization table to the
lowest bit width.

## Disk planning and the local cache

There are three different sizes:

1. **Repository bytes**: sum of exact-revision sibling sizes. This includes
   cards, configs, tokenizers, all weight variants, and auxiliary files.
2. **Planned download bytes**: sum of `file_size` where `will_download` is true
   from
   [`snapshot_download(..., dry_run=True)`](https://huggingface.co/docs/huggingface_hub/main/en/package_reference/hf_api#huggingface_hub.snapshot_download),
   after applying the same allow/ignore patterns as the real runtime. The
   returned record also carries the resolved commit and cache state.
3. **Local bytes**: the cache's actual blob storage, accounting for shared
   blobs across revisions.

Use [`scan_cache_dir`](https://huggingface.co/docs/huggingface_hub/guides/manage-cache#inspect-your-cache)
for a read-only inventory of repositories, commit hashes, refs, files, byte
sizes, access/modified times, and corruption warnings. Cached snapshots may
share blobs, so summing snapshot sizes can overstate reclaimable storage.

Integrity is a separate operation. The official
[`hf cache verify`](https://huggingface.co/docs/huggingface_hub/guides/manage-cache#verify-your-cache)
compares a cached repository/revision with Hub checksums. Record the exact SHA,
verification time, result, and any changed/missing files. An inventory scan is
not verification.

Cache deletion should use the Hub library's revision-aware deletion strategy,
show `expected_freed_size` before confirmation, and execute only after the
user approves. Manual deletion can break shared blobs and refs. Remote models,
cached revisions, pinned Model Aliases, and active Server Runs should be shown
as related but distinct objects.

Before download, compare planned bytes plus a configurable safety margin with
filesystem free space. A full cache may contain unrelated applications' Hub
models, so mastic must not imply sole ownership of it.

## Unified-memory fit

No Hub field gives a safe fit answer. Report a transparent estimate with an
uncertainty band and the inputs below.

### Static model allocation

Prefer selected safetensors file bytes and runtime-specific auxiliary tensors
over `parameter_count * advertised_bits`. Quantized formats have scales,
indices, packing, mixed dtypes, and runtime metadata. Repository size can also
include files never mapped by the chosen Server Type.

The selected tensor bytes are a useful lower-bound proxy, not a peak-memory
prediction. The runtime may mmap, copy, dequantize, compile, or materialize
additional arrays.

### KV and recurrent state

For a conventional full-attention transformer, an explanatory baseline is:

```text
KV bytes ~= 2 * layers * KV heads * head dimension
            * bytes per cached element * cached tokens * concurrent sequences
```

Do not apply that formula blindly. Sliding-window attention, hybrid
attention/SSM architectures, paged caches, prompt caches, pipeline sharding,
speculative models, MTP, and per-layer KV quantization alter the state shape.
For OptiQ, derive KV bytes from the runtime's `kv_config.json` interpretation
when possible; otherwise mark KV size unknown and show only a scenario range.

### Runtime and system headroom

Add separately:

- draft/MTP/vision/audio/adapter weights selected by the Server Definition;
- tokenizer and processor state;
- Metal compiled graphs, temporary activations, request buffers, and runtime
  process overhead;
- metrics proxy and Supervisor overhead;
- other concurrent Server Runs;
- an explicit macOS/application reserve.

Use total physical memory only as capacity context. Current free memory is
volatile; memory pressure and swap are operational observations, not extra
capacity. “Fits” should mean the high estimate remains below a policy reserve,
not merely below `hw.memsize`.

Suggested result bands are `likely fits`, `borderline`, `does not fit`, and
`unknown`. Every band should show the low/high estimate, physical memory,
reserved headroom, context and concurrency assumptions, and which terms are
unknown. A validated Server Run can replace estimates with observed peak
resident/Metal memory for that exact model, runtime, context, and workload.

### Worked metadata example

At commit
[`70a3aa32c7feef511182bf16aa332f37e8d82014`](https://huggingface.co/mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit/tree/70a3aa32c7feef511182bf16aa332f37e8d82014),
`mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit` illustrates why the distinctions
matter:

- the Hub declares `library_name=mlx`, `pipeline_tag=text-generation`, OptiQ,
  mixed-precision, 4-bit and 8-bit tags, and an Apache-2.0 license;
- the resolved repository has completed Hub scans with no reported file
  issues at the observation time;
- exact file metadata totals about 23.00 GiB, while the five primary model
  shards total about 20.62 GiB;
- safetensors also include about 1.53 GiB of MTP weights and 0.83 GiB of OptiQ
  vision weights, which a filename-only primary-shard calculation would miss;
- `config.json` identifies a 40-layer hybrid architecture with ten
  full-attention layers, a 262,144-token declaration, extensive per-layer
  mixed 4/8-bit weight settings, and auxiliary MTP/vision paths;
- `kv_config.json` assigns 4-bit or 8-bit KV settings per full-attention layer.

Those are strong structural observations. They still do not prove that every
`mlx-optiq` version can load the revision, that vision works through an OptiQ
server, that 262,144 tokens fit, or that a particular Mac has enough headroom.
Those require version-specific preflight, fit assumptions, and runtime
validation.

## Trust and supply-chain presentation

### Repository signals

Present these independently:

- immutable resolved commit SHA and whether the requested branch/tag has
  drifted since pinning;
- public/private/gated/disabled state;
- exact file inventory, Git blob IDs, LFS SHA-256 values, and cache
  verification result;
- Hub malware/pickle scan state and file-level findings;
- weight serialization formats;
- executable/configuration files and declared dependencies;
- publisher identity and declared license;
- base-model lineage and conversion metadata, explicitly as declarations;
- last update and whether the local cache matches the pinned revision.

The Hub's
[malware-scanning contract](https://huggingface.co/docs/hub/security-malware)
says scanning runs on each commit, but an absent badge can mean pending, error,
or not yet scanned. “No issues reported” is therefore valid only when
`scansDone` is true. Even then it is a scan result, not a guarantee. Hugging
Face's
[pickle guidance](https://huggingface.co/docs/hub/security-pickle) warns that
loading pickle can execute arbitrary code and recommends trusted publishers,
signed commits, or safer formats. Default to safetensors-only weight selection
where the runtime permits it.

Flag at least these repository paths or config values before any launch:

- `*.py`, `*.pyc`, shared libraries, executables, notebooks with code, and
  shell scripts;
- `*.pkl`, `*.pickle`, `*.pt`, `*.pth`, `*.bin`, `*.ckpt`, and other formats
  whose loader may deserialize code-capable objects;
- `model_file`, `auto_map`, custom tokenizer/processor classes, or any
  `trust_remote_code` requirement;
- `requirements.txt`, `pyproject.toml`, `setup.py`, environment files, and
  model-card instructions to install packages or Git revisions;
- external URLs referenced for weights, adapters, templates, or plugins.

Do not treat a safetensors repository as safe if the selected runtime will
execute custom tokenizer or architecture code. Ask for explicit trust scoped
to repository ID plus immutable SHA, show the files to be trusted, and retain
the decision in local policy rather than silently enabling global remote code.

### Runtime package signals

The model repository is only half the chain. Record the Server Type package,
version, installer source, locked dependencies, artifact hashes, and local
executable path. Prefer hashes from the package index and a lockfile-controlled
install. Show yanked releases, unpinned VCS requirements, local path installs,
and dependency drift as warnings.

Package popularity and model downloads are not security evidence. A signed
source commit is helpful provenance but does not attest a separately published
wheel unless the build relationship is verifiable.

## Recommended query pipeline

```text
user query
  -> list/search candidates (declared discovery fields only)
  -> select Model Reference + optional revision
  -> resolve immutable SHA
  -> fetch repository envelope and bounded metadata
  -> classify executable/custom-code and serialization risks
  -> evaluate each installed Server Type/version statically
  -> dry-run the exact download selection
  -> join local cache inventory and integrity state
  -> derive disk and machine-fit ranges
  -> require explicit trust or unresolved choices
  -> download pinned files
  -> verify cache
  -> launch and record version-specific validation
```

Cache remote results by repository SHA, request shape, and client schema
version. Cache negative or unavailable responses briefly. Preserve the most
recent local inventory separately because it changes without the remote
repository changing.

Authentication tokens must stay in the Hub client's credential path and never
appear in JSON output, logs, subprocess arguments, or error details. Distinguish
not found, gated without access, private without access, authentication
failure, rate limit, timeout, offline mode, and malformed metadata.

## Decisions surfaced for the product map

This research leaves product-policy decisions rather than factual gaps:

1. **Evidence vocabulary and conflicts:** adopt the six states above or choose
   another user-visible confidence model; decide which conflicts block launch.
2. **Compatibility threshold:** decide whether static registry/config matching
   may be called “supported,” or whether only a bounded load/request earns that
   word; define how runtime version changes invalidate prior validation.
3. **Trust defaults:** decide which file formats, scan states, custom-code
   indicators, and dependency sources block by default versus require a
   warning/confirmation; define the scope and lifetime of trust grants.
4. **Fit policy:** choose macOS/application reserve, concurrency scenarios,
   context defaults, and the thresholds for likely/borderline/no fit.
5. **Catalog boundary:** decide whether the primary browse surface starts with
   all Hub models, `mlx-community`, declared `library_name=mlx`, curated
   runtime catalogs, or a union with explicit source facets.
6. **Shared cache ownership:** decide whether mastic may delete any Hub cache
   entry or only revisions it pinned/downloaded, and how active and externally
   referenced snapshots block deletion.
7. **Local-path provenance:** decide whether unpinned directories can become
   Model Aliases, and which manifest/hash record makes them reproducible.
8. **Capability claims:** decide which publisher declarations and evaluation
   provenance qualify for user-facing filters such as coding, tool use,
   reasoning, vision, and long-context.
