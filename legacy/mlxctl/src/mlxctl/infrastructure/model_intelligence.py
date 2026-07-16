"""Bounded, evidence-qualified model inspection for exact Hub revisions."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Callable, Mapping, Protocol
from urllib.parse import quote, urlparse


MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_TOTAL_METADATA_BYTES = 6 * 1024 * 1024
MAX_REPOSITORY_FILES = 20_000
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 100_000
MAX_JSON_STRING_LENGTH = 1024 * 1024


class ModelIntelligenceError(ValueError):
    """A model-intelligence request or response violated the safe contract."""


class EvidenceState(StrEnum):
    """How directly a reported value is supported."""

    OBSERVED = "observed"
    DECLARED = "declared"
    DERIVED = "derived"
    VALIDATED = "validated"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RepositoryFile:
    path: str
    size: int | None
    blob_id: str | None = None
    lfs_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RepositoryEnvelope:
    repo_id: str
    commit_sha: str
    files: tuple[RepositoryFile, ...]
    pipeline_tag: str | None = None
    library_name: str | None = None
    tags: tuple[str, ...] = ()
    private: bool | None = None
    gated: bool | str | None = None
    disabled: bool | None = None
    card_data: Mapping[str, object] | None = None
    scans_done: bool | None = None
    security_issues: tuple[str, ...] = ()
    author: str | None = None
    created_at: str | None = None
    last_modified: str | None = None
    safetensors: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class MetadataPayload:
    path: str
    content_type: str
    body: bytes


@dataclass(frozen=True, slots=True)
class CacheObservation:
    state: str
    source: str
    snapshot_path: str | None = None
    size_bytes: int | None = None
    verified: bool | None = None

    @classmethod
    def absent(cls) -> CacheObservation:
        return cls(state="absent", source="local-cache-inventory")


@dataclass(frozen=True, slots=True)
class MachineInventory:
    total_memory_bytes: int
    source: str
    available_memory_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    installation_id: str
    runtime: str
    version: str
    recognized_model_types: frozenset[str]
    capabilities: frozenset[str]
    source: str


@dataclass(frozen=True, slots=True)
class RuntimeCompatibility:
    installation_id: str
    runtime: str
    version: str
    status: str
    capabilities: frozenset[str]
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class FitTerm:
    name: str
    low_bytes: int | None
    high_bytes: int | None
    state: EvidenceState
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class MachineFit:
    classification: str
    low_bytes: int
    high_bytes: int
    machine_memory_bytes: int
    reserved_headroom_bytes: int
    context_tokens: int
    concurrency: int
    terms: tuple[FitTerm, ...]
    source: str


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    repo_id: str
    requested_revision: str
    commit_sha: str
    source: str = "hugging-face-model-info"


@dataclass(frozen=True, slots=True)
class EvidenceValue:
    value: object | None
    state: EvidenceState
    source: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactAssessment:
    role: str
    path: str
    present: bool
    required_by: str | None
    state: EvidenceState
    source: str


@dataclass(frozen=True, slots=True)
class TrustSignal:
    name: str
    severity: str
    state: EvidenceState
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class ModelIntelligenceReport:
    identity: ModelIdentity
    attributes: Mapping[str, EvidenceValue]
    cache: CacheObservation
    artifacts: tuple[ArtifactAssessment, ...] = ()
    capabilities: Mapping[str, EvidenceValue] = field(default_factory=dict)
    compatibility: tuple[RuntimeCompatibility, ...] = ()
    fit: MachineFit | None = None
    trust_signals: tuple[TrustSignal, ...] = ()
    repository_files: tuple[RepositoryFile, ...] = ()


class ModelRepositoryPort(Protocol):
    """Exact-revision remote metadata and local-cache boundary."""

    def resolve(self, repo_id: str, revision: str) -> RepositoryEnvelope: ...

    def fetch_metadata(
        self, repo_id: str, commit_sha: str, path: str, *, max_bytes: int
    ) -> MetadataPayload | None: ...

    def cache_observation(self, repo_id: str, commit_sha: str) -> CacheObservation: ...


class MachineInventoryPort(Protocol):
    def inspect(self) -> MachineInventory: ...


class PsutilMachineInventory:
    """Observe unified-memory capacity without mutating host state."""

    def __init__(self, sample: Callable[[], object] | None = None) -> None:
        if sample is None:
            import psutil

            sample = psutil.virtual_memory
        self._sample = sample

    def inspect(self) -> MachineInventory:
        value = self._sample()
        total = getattr(value, "total", None)
        available = getattr(value, "available", None)
        if type(total) is not int or total <= 0:
            raise ModelIntelligenceError("machine memory inventory is unavailable")
        if type(available) is not int or available < 0:
            available = None
        return MachineInventory(
            total_memory_bytes=total,
            available_memory_bytes=available,
            source="psutil virtual_memory",
        )


class HubApiPort(Protocol):
    def model_info(self, repo_id: str, **kwargs: object) -> object: ...


class MetadataFetchPort(Protocol):
    def fetch(
        self, repo_id: str, commit_sha: str, path: str, *, max_bytes: int
    ) -> MetadataPayload | None: ...


class CacheInventoryPort(Protocol):
    def observe(self, repo_id: str, commit_sha: str) -> CacheObservation: ...


class HuggingFaceModelRepository:
    """Official Hub adapter for exact, bounded model metadata inspection."""

    def __init__(
        self,
        *,
        api: HubApiPort | None = None,
        metadata_fetcher: MetadataFetchPort | None = None,
        cache_inventory: CacheInventoryPort | None = None,
    ) -> None:
        if api is None:
            from huggingface_hub import HfApi

            api = HfApi()
        self._api = api
        self._metadata_fetcher = metadata_fetcher or _HuggingFaceMetadataFetcher()
        self._cache_inventory = cache_inventory or _HuggingFaceCacheInventory()

    def resolve(self, repo_id: str, revision: str) -> RepositoryEnvelope:
        _validate_repo_id(repo_id)
        _validate_revision(revision)
        info = self._api.model_info(
            repo_id,
            revision=revision,
            files_metadata=True,
            securityStatus=True,
            timeout=10.0,
        )
        canonical_id = getattr(info, "id", None)
        commit_sha = getattr(info, "sha", None)
        if not isinstance(canonical_id, str) or not isinstance(commit_sha, str):
            raise ModelIntelligenceError("Hub model metadata omitted exact identity")
        siblings = tuple(getattr(info, "siblings", ()))
        if len(siblings) > MAX_REPOSITORY_FILES:
            raise ModelIntelligenceError(
                "Hub repository inventory exceeded the file-count limit"
            )
        files = tuple(_repository_file(item) for item in siblings)
        security = getattr(info, "securityStatus", None)
        if security is None:
            security = getattr(info, "security_status", None)
        if security is None:
            security = getattr(info, "security_repo_status", None)
        scans_done, issues = _security_status(security)
        card_data = _card_data(getattr(info, "card_data", None))
        if card_data is not None:
            _validate_json_structure(card_data)
        tags = tuple(
            item
            for item in (getattr(info, "tags", None) or ())
            if isinstance(item, str)
        )
        if len(tags) > 10_000 or any(len(item) > 1024 for item in tags):
            raise ModelIntelligenceError("Hub tags exceeded the response limit")
        return RepositoryEnvelope(
            repo_id=canonical_id,
            commit_sha=commit_sha,
            files=files,
            pipeline_tag=_optional_string(getattr(info, "pipeline_tag", None)),
            library_name=_optional_string(getattr(info, "library_name", None)),
            tags=tags,
            private=_optional_bool(getattr(info, "private", None)),
            gated=getattr(info, "gated", None),
            disabled=_optional_bool(getattr(info, "disabled", None)),
            card_data=card_data,
            scans_done=scans_done,
            security_issues=issues,
            author=_optional_string(getattr(info, "author", None)),
            created_at=_datetime_string(getattr(info, "created_at", None)),
            last_modified=_datetime_string(getattr(info, "last_modified", None)),
            safetensors=_safetensors_data(getattr(info, "safetensors", None)),
        )

    def fetch_metadata(
        self, repo_id: str, commit_sha: str, path: str, *, max_bytes: int
    ) -> MetadataPayload | None:
        _validate_repo_id(repo_id)
        if not _COMMIT_SHA.fullmatch(commit_sha):
            raise ModelIntelligenceError("metadata fetch requires an exact commit SHA")
        _validate_file(RepositoryFile(path, None))
        if max_bytes < 1 or max_bytes > MAX_METADATA_BYTES:
            raise ModelIntelligenceError("metadata fetch byte limit is out of bounds")
        return self._metadata_fetcher.fetch(
            repo_id, commit_sha, path, max_bytes=max_bytes
        )

    def cache_observation(self, repo_id: str, commit_sha: str) -> CacheObservation:
        return self._cache_inventory.observe(repo_id, commit_sha)


class ModelIntelligence:
    """Inspect a candidate without downloading weights or executing model code."""

    def __init__(
        self, repository: ModelRepositoryPort, machine: MachineInventoryPort
    ) -> None:
        self._repository = repository
        self._machine = machine

    def inspect(
        self,
        repo_id: str,
        revision: str = "main",
        *,
        runtimes: tuple[RuntimeObservation, ...] = (),
        context_tokens: int = 32_768,
        concurrency: int = 1,
    ) -> ModelIntelligenceReport:
        _validate_repo_id(repo_id)
        _validate_revision(revision)
        envelope = self._repository.resolve(repo_id, revision)
        _validate_repo_id(envelope.repo_id)
        if not _COMMIT_SHA.fullmatch(envelope.commit_sha):
            raise ModelIntelligenceError(
                "repository did not resolve to an exact commit SHA"
            )
        if len(envelope.files) > MAX_REPOSITORY_FILES:
            raise ModelIntelligenceError(
                "repository inventory exceeded the file-count limit"
            )
        for item in envelope.files:
            _validate_file(item)
        if (
            type(context_tokens) is not int
            or context_tokens < 1
            or context_tokens > 16_777_216
        ):
            raise ModelIntelligenceError("context token scenario is out of bounds")
        if type(concurrency) is not int or concurrency < 1 or concurrency > 128:
            raise ModelIntelligenceError("concurrency scenario is out of bounds")

        metadata_budget = [0]
        config_payload = self._metadata_json(envelope, "config.json", metadata_budget)
        if config_payload is not None and not isinstance(config_payload, dict):
            raise ModelIntelligenceError("metadata root must be an object: config.json")
        config = config_payload or {}
        kv_config = self._metadata_json(envelope, "kv_config.json", metadata_budget)
        tokenizer_payload = self._metadata_json(
            envelope, "tokenizer_config.json", metadata_budget
        )
        if tokenizer_payload is not None and not isinstance(tokenizer_payload, dict):
            raise ModelIntelligenceError(
                "metadata root must be an object: tokenizer_config.json"
            )
        tokenizer_config = tokenizer_payload or {}
        weight_index_payload = self._metadata_json(
            envelope, "model.safetensors.index.json", metadata_budget
        )
        if weight_index_payload is not None and not isinstance(
            weight_index_payload, dict
        ):
            raise ModelIntelligenceError(
                "metadata root must be an object: model.safetensors.index.json"
            )
        weight_index = weight_index_payload
        architecture = config.get("model_type")
        card_data = envelope.card_data or {}
        config_context = _first_positive_int(
            config.get("max_position_embeddings"),
            _nested(config, "text_config", "max_position_embeddings"),
        )
        tokenizer_context = _first_positive_int(
            tokenizer_config.get("model_max_length")
        )
        context_value, context_state, context_source = _context_attribute(
            config_context, tokenizer_context, envelope.commit_sha
        )
        quantization = config.get("quantization")
        if quantization is None:
            quantization = config.get("quantization_config")
        attributes = {
            "architecture": EvidenceValue(
                architecture if isinstance(architecture, str) else None,
                EvidenceState.OBSERVED
                if isinstance(architecture, str)
                else EvidenceState.UNKNOWN,
                f"config.json@{envelope.commit_sha}",
            ),
            "task": EvidenceValue(
                envelope.pipeline_tag,
                EvidenceState.DECLARED
                if envelope.pipeline_tag is not None
                else EvidenceState.UNKNOWN,
                f"Hub model metadata@{envelope.commit_sha}",
            ),
            "library": EvidenceValue(
                envelope.library_name,
                EvidenceState.DECLARED
                if envelope.library_name is not None
                else EvidenceState.UNKNOWN,
                f"Hub model metadata@{envelope.commit_sha}",
            ),
            "publisher": EvidenceValue(
                envelope.author,
                EvidenceState.OBSERVED
                if envelope.author is not None
                else EvidenceState.UNKNOWN,
                f"Hub model metadata@{envelope.commit_sha}",
            ),
            "updated_at": EvidenceValue(
                envelope.last_modified,
                EvidenceState.OBSERVED
                if envelope.last_modified is not None
                else EvidenceState.UNKNOWN,
                f"Hub model metadata@{envelope.commit_sha}",
            ),
            "parameters": EvidenceValue(
                envelope.safetensors,
                EvidenceState.OBSERVED
                if envelope.safetensors is not None
                else EvidenceState.UNKNOWN,
                f"Hub safetensors summary@{envelope.commit_sha}",
            ),
            "repository_bytes": _repository_bytes_attribute(envelope),
            "tags": EvidenceValue(
                envelope.tags if envelope.tags else None,
                EvidenceState.DECLARED if envelope.tags else EvidenceState.UNKNOWN,
                f"Hub model metadata@{envelope.commit_sha}",
            ),
            "license": EvidenceValue(
                card_data.get("license"),
                EvidenceState.DECLARED
                if card_data.get("license") is not None
                else EvidenceState.UNKNOWN,
                f"model card data@{envelope.commit_sha}",
            ),
            "base_model": EvidenceValue(
                card_data.get("base_model"),
                EvidenceState.DECLARED
                if card_data.get("base_model") is not None
                else EvidenceState.UNKNOWN,
                f"model card data@{envelope.commit_sha}",
            ),
            "quantization": EvidenceValue(
                quantization,
                EvidenceState.OBSERVED
                if isinstance(quantization, dict)
                else EvidenceState.UNKNOWN,
                f"config.json@{envelope.commit_sha}",
            ),
            "context_length": EvidenceValue(
                context_value,
                context_state,
                context_source,
                "source values disagree; no ceiling was selected"
                if context_state is EvidenceState.CONFLICTING
                else None,
            ),
            "access": EvidenceValue(
                {
                    "private": envelope.private,
                    "gated": envelope.gated,
                    "disabled": envelope.disabled,
                },
                EvidenceState.OBSERVED
                if any(
                    value is not None
                    for value in (
                        envelope.private,
                        envelope.gated,
                        envelope.disabled,
                    )
                )
                else EvidenceState.UNKNOWN,
                f"Hub model metadata@{envelope.commit_sha}",
            ),
        }
        cache = self._repository.cache_observation(
            envelope.repo_id, envelope.commit_sha
        )
        _validate_cache_observation(cache)
        file_paths = {item.path for item in envelope.files}
        artifacts: list[ArtifactAssessment] = []
        if "kv_config.json" in file_paths:
            artifacts.append(
                ArtifactAssessment(
                    role="optiq_kv_config",
                    path="kv_config.json",
                    present=kv_config is not None,
                    required_by=None,
                    state=EvidenceState.OBSERVED
                    if kv_config is not None
                    else EvidenceState.CONFLICTING,
                    source=f"repository inventory@{envelope.commit_sha}",
                )
            )
        mtp_path = _first_artifact_reference(
            config.get("mtp_file"),
            config.get("mtp_model_path"),
            _nested(config, "mlx_lm_extra_tensors", "mtp_file"),
        )
        mtp_layers = config.get("mtp_num_hidden_layers")
        if mtp_layers is None:
            mtp_layers = config.get("num_nextn_predict_layers")
        if mtp_layers is None:
            mtp_layers = _nested(config, "text_config", "mtp_num_hidden_layers")
        mtp_declared = type(mtp_layers) is int and mtp_layers > 0
        if mtp_path is not None:
            mtp_present = mtp_path in file_paths
            artifacts.append(
                ArtifactAssessment(
                    role="mtp_weights",
                    path=mtp_path,
                    present=mtp_present,
                    required_by="config.json",
                    state=EvidenceState.OBSERVED
                    if mtp_present
                    else EvidenceState.CONFLICTING,
                    source=f"config.json@{envelope.commit_sha}",
                )
            )
        vision_path = _local_artifact_reference(
            _nested(config, "optiq_vision", "sidecar")
        )
        if vision_path is not None:
            vision_present = vision_path in file_paths
            artifacts.append(
                ArtifactAssessment(
                    role="optiq_vision_weights",
                    path=vision_path,
                    present=vision_present,
                    required_by="config.json",
                    state=EvidenceState.OBSERVED
                    if vision_present
                    else EvidenceState.CONFLICTING,
                    source=f"config.json@{envelope.commit_sha}",
                )
            )
        mtp_signal = mtp_declared or mtp_path is not None
        has_vision_structure = isinstance(config.get("vision_config"), dict)
        has_audio_structure = isinstance(config.get("audio_config"), dict)
        capabilities = {
            "text_generation": EvidenceValue(
                True
                if envelope.pipeline_tag in {"text-generation", "text2text-generation"}
                else None,
                EvidenceState.DECLARED
                if envelope.pipeline_tag in {"text-generation", "text2text-generation"}
                else EvidenceState.UNKNOWN,
                f"Hub pipeline tag@{envelope.commit_sha}",
            ),
            "vision": EvidenceValue(
                True if has_vision_structure else None,
                EvidenceState.DERIVED
                if has_vision_structure
                else EvidenceState.UNKNOWN,
                f"config.json@{envelope.commit_sha}",
                "vision structure is present; serving behavior is not validated"
                if has_vision_structure
                else None,
            ),
            "audio": EvidenceValue(
                True if has_audio_structure else None,
                EvidenceState.DERIVED if has_audio_structure else EvidenceState.UNKNOWN,
                f"config.json@{envelope.commit_sha}",
            ),
            "coding": EvidenceValue(
                None,
                EvidenceState.UNKNOWN,
                f"model card evaluations@{envelope.commit_sha}",
                "repository names and tags do not prove coding behavior",
            ),
            "tool_use": EvidenceValue(
                None,
                EvidenceState.UNKNOWN,
                f"model card evaluations@{envelope.commit_sha}",
                "chat templates and tags do not prove tool-use behavior",
            ),
            "mtp": EvidenceValue(
                True if mtp_signal else None,
                EvidenceState.DERIVED if mtp_signal else EvidenceState.UNKNOWN,
                f"config.json@{envelope.commit_sha}",
                "structural MTP signal; runtime behavior is not validated"
                if mtp_signal
                else None,
            ),
            "optiq": EvidenceValue(
                True if kv_config is not None else None,
                EvidenceState.DERIVED
                if kv_config is not None
                else EvidenceState.UNKNOWN,
                f"kv_config.json@{envelope.commit_sha}"
                if kv_config is not None
                else f"repository inventory@{envelope.commit_sha}",
                "OptiQ metadata is present; load compatibility is not validated"
                if kv_config is not None
                else None,
            ),
        }
        trust_signals = _trust_signals(envelope, config)
        compatibility = tuple(
            _assess_runtime(
                runtime,
                architecture if isinstance(architecture, str) else None,
                has_optiq_config=kv_config is not None,
                commit_sha=envelope.commit_sha,
            )
            for runtime in runtimes
        )
        machine = self._machine.inspect()
        _validate_machine(machine)
        fit = _estimate_fit(
            envelope,
            config,
            weight_index,
            kv_config,
            machine=machine,
            context_tokens=context_tokens,
            concurrency=concurrency,
            artifact_paths=tuple(
                item.path for item in artifacts if item.role.endswith("weights")
            ),
        )
        return ModelIntelligenceReport(
            identity=ModelIdentity(
                repo_id=envelope.repo_id,
                requested_revision=revision,
                commit_sha=envelope.commit_sha,
            ),
            attributes=attributes,
            cache=cache,
            artifacts=tuple(artifacts),
            capabilities=capabilities,
            compatibility=compatibility,
            fit=fit,
            trust_signals=trust_signals,
            repository_files=envelope.files,
        )

    def _metadata_json(
        self, envelope: RepositoryEnvelope, path: str, budget: list[int]
    ) -> object | None:
        if path not in {item.path for item in envelope.files}:
            return None
        payload = self._repository.fetch_metadata(
            envelope.repo_id,
            envelope.commit_sha,
            path,
            max_bytes=MAX_METADATA_BYTES,
        )
        if payload is None:
            return None
        if payload.path != path:
            raise ModelIntelligenceError("metadata response path did not match request")
        if len(payload.body) > MAX_METADATA_BYTES:
            raise ModelIntelligenceError("metadata response exceeded the byte limit")
        budget[0] += len(payload.body)
        if budget[0] > MAX_TOTAL_METADATA_BYTES:
            raise ModelIntelligenceError(
                "metadata responses exceeded the total byte limit"
            )
        media_type = payload.content_type.partition(";")[0].strip().lower()
        if media_type not in {"application/json", "text/json", "text/plain"}:
            raise ModelIntelligenceError("metadata response was not JSON content")
        try:
            value = json.loads(payload.body, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelIntelligenceError(f"invalid JSON metadata: {path}") from error
        if not isinstance(value, (dict, list)):
            raise ModelIntelligenceError(
                f"metadata root must be an object or array: {path}"
            )
        _validate_json_structure(value)
        return value


class _HuggingFaceMetadataFetcher:
    """Stream one allowlisted exact-revision metadata file with hard bounds."""

    def fetch(
        self, repo_id: str, commit_sha: str, path: str, *, max_bytes: int
    ) -> MetadataPayload | None:
        import httpx
        from huggingface_hub.utils import build_hf_headers

        url = (
            "https://huggingface.co/"
            f"{quote(repo_id, safe='/')}/resolve/{quote(commit_sha, safe='')}/"
            f"{quote(path, safe='/')}"
        )
        timeout = httpx.Timeout(10.0, connect=5.0)
        with httpx.Client(
            follow_redirects=True, timeout=timeout, max_redirects=5
        ) as client:
            with client.stream(
                "GET", url, headers=build_hf_headers(token=None)
            ) as response:
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                _validate_hugging_face_url(str(response.url))
                declared_size = response.headers.get("content-length")
                if declared_size is not None:
                    try:
                        if int(declared_size) > max_bytes:
                            raise ModelIntelligenceError(
                                "metadata response exceeded the byte limit"
                            )
                    except ValueError as error:
                        raise ModelIntelligenceError(
                            "metadata response had an invalid content length"
                        ) from error
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ModelIntelligenceError(
                            "metadata response exceeded the byte limit"
                        )
                return MetadataPayload(
                    path=path,
                    content_type=response.headers.get("content-type", ""),
                    body=bytes(body),
                )


class _HuggingFaceCacheInventory:
    def observe(self, repo_id: str, commit_sha: str) -> CacheObservation:
        from huggingface_hub import scan_cache_dir
        from huggingface_hub.errors import CacheNotFound

        try:
            cache = scan_cache_dir()
        except (CacheNotFound, FileNotFoundError):
            return CacheObservation.absent()
        for repo in cache.repos:
            if (
                repo.repo_id != repo_id
                or getattr(repo, "repo_type", "model") != "model"
            ):
                continue
            for revision in repo.revisions:
                if revision.commit_hash == commit_sha:
                    return CacheObservation(
                        state="present",
                        source="huggingface_hub scan_cache_dir",
                        snapshot_path=str(revision.snapshot_path),
                        size_bytes=revision.size_on_disk,
                        verified=None,
                    )
        return CacheObservation.absent()


def _repository_file(item: object) -> RepositoryFile:
    path = getattr(item, "rfilename", None)
    if not isinstance(path, str):
        raise ModelIntelligenceError("Hub file metadata omitted its path")
    size = getattr(item, "size", None)
    if type(size) is not int:
        size = None
    blob_id = _optional_string(getattr(item, "blob_id", None))
    lfs = getattr(item, "lfs", None)
    if isinstance(lfs, Mapping):
        lfs_sha256 = _optional_string(lfs.get("sha256"))
    else:
        lfs_sha256 = _optional_string(getattr(lfs, "sha256", None))
    return RepositoryFile(
        path=path,
        size=size,
        blob_id=blob_id,
        lfs_sha256=lfs_sha256,
    )


def _card_data(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    raise ModelIntelligenceError("Hub model card data had an unsupported shape")


def _safetensors_data(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    parameters = getattr(value, "parameters", None)
    total = getattr(value, "total", None)
    if not isinstance(parameters, Mapping) or type(total) is not int:
        raise ModelIntelligenceError("Hub safetensors summary had an unsupported shape")
    return {
        "parameters": {
            key: count
            for key, count in parameters.items()
            if isinstance(key, str) and type(count) is int
        },
        "total": total,
    }


def _datetime_string(value: object) -> str | None:
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        result = isoformat()
        return result if isinstance(result, str) else None
    return None


def _security_status(value: object) -> tuple[bool | None, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        return None, ()
    scans_done = _optional_bool(value.get("scansDone"))
    raw_issues = value.get("filesWithIssues") or ()
    if not isinstance(raw_issues, (list, tuple)):
        raise ModelIntelligenceError("Hub security status had an unsupported shape")
    return scans_done, tuple(_security_issue(item) for item in raw_issues)


def _security_issue(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        path = value.get("path") or value.get("filename") or "unknown file"
        issue = value.get("issue") or value.get("status") or "reported issue"
        return f"{path}: {issue}"
    return "Hub reported an unstructured file issue"


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _validate_hugging_face_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "huggingface.co" or host.endswith(".huggingface.co")
    ):
        raise ModelIntelligenceError("Hub metadata redirected to an untrusted URL")


_REPO_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z"
)
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40,64}\Z")


def _validate_repo_id(repo_id: str) -> None:
    if not _REPO_ID.fullmatch(repo_id) or "://" in repo_id:
        raise ModelIntelligenceError(
            "model reference must be a Hugging Face repository ID, not a path or URL"
        )


def _validate_revision(revision: str) -> None:
    if not _REVISION.fullmatch(revision):
        raise ModelIntelligenceError("invalid model revision")
    if any(part in {"", ".", ".."} for part in revision.split("/")):
        raise ModelIntelligenceError("model revision contains an unsafe path segment")


def _validate_file(item: RepositoryFile) -> None:
    path = PurePosixPath(item.path)
    if (
        not item.path
        or len(item.path) > 1024
        or item.path.startswith("/")
        or "\\" in item.path
        or any(part in {"", ".", ".."} for part in item.path.split("/"))
        or path.is_absolute()
    ):
        raise ModelIntelligenceError(
            f"repository inventory contains an unsafe path: {item.path!r}"
        )
    if item.size is not None and (
        type(item.size) is not int or item.size < 0 or item.size > 2**63 - 1
    ):
        raise ModelIntelligenceError("repository inventory contains an invalid size")


def _local_artifact_reference(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelIntelligenceError("artifact reference must be a repository path")
    if "://" in value or value.startswith(("file:", "javascript:", "data:")):
        raise ModelIntelligenceError("artifact reference must not be a URL")
    item = RepositoryFile(value, 0)
    _validate_file(item)
    return value


def _assess_runtime(
    runtime: RuntimeObservation,
    architecture: str | None,
    *,
    has_optiq_config: bool,
    commit_sha: str,
) -> RuntimeCompatibility:
    if (
        not runtime.installation_id
        or not runtime.runtime
        or not runtime.version
        or not runtime.source
    ):
        raise ModelIntelligenceError("runtime observation is missing exact provenance")
    source = (
        f"{runtime.source}: {runtime.installation_id} ({runtime.version}); "
        f"config.json@{commit_sha}"
    )
    if architecture is None:
        status = "unknown"
        detail = "model architecture is unknown"
    elif (
        runtime.recognized_model_types
        and architecture not in runtime.recognized_model_types
    ):
        status = "unsupported"
        detail = (
            f"{runtime.runtime} {runtime.version} does not list architecture "
            f"{architecture!r} in its observed registry"
        )
    elif runtime.runtime == "optiq" and (
        not has_optiq_config or "kv_config" not in runtime.capabilities
    ):
        status = "unknown"
        detail = "architecture matches, but exact OptiQ KV support is not evidenced"
    elif not runtime.recognized_model_types:
        status = "unknown"
        detail = (
            "runtime architecture recognition evidence is unavailable; exact option "
            "preflight and a bounded readiness probe are still required"
        )
    else:
        status = "candidate"
        detail = (
            "static architecture and required metadata preflight passed; "
            "a bounded load or request is still required for validation"
        )
    return RuntimeCompatibility(
        installation_id=runtime.installation_id,
        runtime=runtime.runtime,
        version=runtime.version,
        status=status,
        capabilities=runtime.capabilities,
        source=source,
        detail=detail,
    )


def _estimate_fit(
    envelope: RepositoryEnvelope,
    config: Mapping[str, object],
    weight_index: Mapping[str, object] | None,
    kv_config: object | None,
    *,
    machine: MachineInventory,
    context_tokens: int,
    concurrency: int,
    artifact_paths: tuple[str, ...],
) -> MachineFit:
    files = {item.path: item for item in envelope.files}
    terms: list[FitTerm] = []
    selected_paths = _selected_weight_paths(weight_index)
    if selected_paths is None and "model.safetensors" in files:
        selected_paths = ("model.safetensors",)
    missing_selected = (
        tuple(
            path
            for path in selected_paths
            if path not in files or files[path].size is None
        )
        if selected_paths is not None
        else ()
    )
    if selected_paths is None or missing_selected:
        weights = None
        detail = (
            "no exact safetensors selection was available"
            if selected_paths is None
            else f"index references missing files: {', '.join(missing_selected)}"
        )
        terms.append(
            FitTerm(
                "selected tensor files",
                None,
                None,
                EvidenceState.UNKNOWN
                if selected_paths is None
                else EvidenceState.CONFLICTING,
                f"repository inventory@{envelope.commit_sha}",
                detail,
            )
        )
    else:
        weights = sum(files[path].size or 0 for path in selected_paths)
        terms.append(
            FitTerm(
                "selected tensor files",
                weights,
                weights,
                EvidenceState.OBSERVED,
                f"model.safetensors.index.json and repository inventory@{envelope.commit_sha}",
                "selected file bytes are a static-allocation lower bound",
            )
        )

    auxiliary_paths = tuple(
        path
        for path in artifact_paths
        if selected_paths is None or path not in selected_paths
    )
    auxiliary_known = all(
        path in files and files[path].size is not None for path in auxiliary_paths
    )
    auxiliary = sum(files[path].size or 0 for path in auxiliary_paths if path in files)
    if auxiliary:
        terms.append(
            FitTerm(
                "required auxiliary weights",
                auxiliary,
                auxiliary,
                EvidenceState.OBSERVED,
                f"config.json and repository inventory@{envelope.commit_sha}",
                "exact bytes for referenced auxiliary tensor artifacts",
            )
        )
    if auxiliary_paths and not auxiliary_known:
        terms.append(
            FitTerm(
                "required auxiliary weights",
                None,
                None,
                EvidenceState.UNKNOWN,
                f"config.json and repository inventory@{envelope.commit_sha}",
                "one or more referenced auxiliary files has no observed byte size",
            )
        )

    if kv_config is None:
        kv_bytes = _standard_kv_bytes(
            config, context_tokens=context_tokens, concurrency=concurrency
        )
        kv_source = f"config.json@{envelope.commit_sha}; fp16/bf16 KV scenario"
    else:
        kv_bytes = optiq_kv_bytes(
            config,
            kv_config,
            context_tokens=context_tokens,
            concurrency=concurrency,
        )
        kv_source = (
            f"config.json and kv_config.json@{envelope.commit_sha}; "
            "per-layer OptiQ bit widths"
        )
    if kv_bytes is None:
        terms.append(
            FitTerm(
                "KV cache",
                None,
                None,
                EvidenceState.UNKNOWN,
                f"config.json@{envelope.commit_sha}",
                "required dimensions or a safe cache-layout model are unavailable",
            )
        )
    else:
        terms.append(
            FitTerm(
                "KV cache",
                kv_bytes,
                kv_bytes,
                EvidenceState.DERIVED,
                kv_source,
                f"cache scenario at {context_tokens} tokens and concurrency {concurrency}",
            )
        )

    known_static = (weights or 0) + auxiliary + (kv_bytes or 0)
    runtime_high = max(2 * 1024**3, int((weights or 0) * 0.20))
    terms.append(
        FitTerm(
            "runtime and transient overhead",
            0,
            runtime_high,
            EvidenceState.DERIVED,
            "mlxctl conservative fit policy v1",
            "uncertainty allowance; replace with observed peak after validation",
        )
    )
    low = known_static
    high = known_static + runtime_high
    reserve = max(8 * 1024**3, (machine.total_memory_bytes + 4) // 5)
    usable = max(0, machine.total_memory_bytes - reserve)
    incomplete = weights is None or kv_bytes is None or not auxiliary_known
    if low > usable:
        classification = "does_not_fit"
    elif incomplete:
        classification = "unknown"
    elif high <= usable:
        classification = "likely_fits"
    else:
        classification = "borderline"
    return MachineFit(
        classification=classification,
        low_bytes=low,
        high_bytes=high,
        machine_memory_bytes=machine.total_memory_bytes,
        reserved_headroom_bytes=reserve,
        context_tokens=context_tokens,
        concurrency=concurrency,
        terms=tuple(terms),
        source=f"{machine.source}; mlxctl conservative fit policy v1",
    )


def _selected_weight_paths(
    weight_index: Mapping[str, object] | None,
) -> tuple[str, ...] | None:
    if weight_index is None:
        return None
    weight_map = weight_index.get("weight_map")
    if not isinstance(weight_map, dict) or not weight_map:
        return None
    paths: list[str] = []
    for value in weight_map.values():
        path = _local_artifact_reference(value)
        if path is None:
            return None
        paths.append(path)
    return tuple(dict.fromkeys(paths))


def _standard_kv_bytes(
    config: Mapping[str, object], *, context_tokens: int, concurrency: int
) -> int | None:
    fields = (
        config.get("num_hidden_layers"),
        config.get("hidden_size"),
        config.get("num_attention_heads"),
        config.get("num_key_value_heads"),
    )
    if not all(type(value) is int and value > 0 for value in fields):
        return None
    layers, hidden_size, attention_heads, kv_heads = fields
    if hidden_size % attention_heads:
        return None
    head_dimension = hidden_size // attention_heads
    return 2 * layers * kv_heads * head_dimension * 2 * context_tokens * concurrency


def optiq_kv_bytes(
    config: Mapping[str, object],
    kv_config: object,
    *,
    context_tokens: int,
    concurrency: int,
) -> int | None:
    text_config = config.get("text_config")
    dimensions = text_config if isinstance(text_config, dict) else config
    head_dimension = dimensions.get("head_dim")
    if type(head_dimension) is not int or head_dimension <= 0:
        hidden_size = dimensions.get("hidden_size")
        attention_heads = dimensions.get("num_attention_heads")
        if (
            type(hidden_size) is not int
            or type(attention_heads) is not int
            or hidden_size <= 0
            or attention_heads <= 0
            or hidden_size % attention_heads
        ):
            return None
        head_dimension = hidden_size // attention_heads
    kv_heads = dimensions.get("num_key_value_heads")
    if type(kv_heads) is not int or kv_heads <= 0:
        return None
    layers: object
    if isinstance(kv_config, dict):
        layers = kv_config.get("layers")
        if isinstance(layers, dict):
            layers = list(layers.values())
    else:
        layers = kv_config
    if not isinstance(layers, list) or not layers:
        return None
    bit_sum = 0
    metadata_bytes_per_token = 0
    layer_indexes: set[int] = set()
    for position, layer in enumerate(layers):
        if not isinstance(layer, dict):
            return None
        bits = layer.get("bits")
        layer_index = layer.get("layer_idx", position)
        group_size = layer.get("group_size")
        if (
            type(bits) is not int
            or bits not in {2, 3, 4, 5, 6, 8, 16}
            or type(layer_index) is not int
            or layer_index < 0
            or layer_index in layer_indexes
        ):
            return None
        layer_indexes.add(layer_index)
        bit_sum += bits
        if group_size is not None:
            values_per_token = 2 * kv_heads * head_dimension
            if (
                type(group_size) is not int
                or group_size <= 0
                or values_per_token % group_size
            ):
                return None
            # One float16 scale and bias per quantization group for K and V.
            metadata_bytes_per_token += values_per_token // group_size * 4
    quantized_bytes_per_token = 2 * bit_sum * kv_heads * head_dimension // 8
    return (
        (quantized_bytes_per_token + metadata_bytes_per_token)
        * context_tokens
        * concurrency
    )


def _nested(value: Mapping[str, object], *keys: str) -> object | None:
    current: object = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_artifact_reference(*values: object) -> str | None:
    for value in values:
        if value is not None:
            return _local_artifact_reference(value)
    return None


def _first_positive_int(*values: object) -> int | None:
    for value in values:
        if type(value) is int and value > 0:
            return value
    return None


def _context_attribute(
    config_value: int | None, tokenizer_value: int | None, commit_sha: str
) -> tuple[object | None, EvidenceState, str]:
    if config_value is not None and tokenizer_value is not None:
        if config_value != tokenizer_value:
            return (
                {
                    "config.json": config_value,
                    "tokenizer_config.json": tokenizer_value,
                },
                EvidenceState.CONFLICTING,
                f"config.json and tokenizer_config.json@{commit_sha}",
            )
        return (
            config_value,
            EvidenceState.OBSERVED,
            f"config.json and tokenizer_config.json@{commit_sha}",
        )
    if config_value is not None:
        return config_value, EvidenceState.OBSERVED, f"config.json@{commit_sha}"
    if tokenizer_value is not None:
        return (
            tokenizer_value,
            EvidenceState.OBSERVED,
            f"tokenizer_config.json@{commit_sha}",
        )
    return None, EvidenceState.UNKNOWN, f"exact-revision metadata@{commit_sha}"


def _repository_bytes_attribute(envelope: RepositoryEnvelope) -> EvidenceValue:
    if any(item.size is None for item in envelope.files):
        return EvidenceValue(
            None,
            EvidenceState.UNKNOWN,
            f"Hub file metadata@{envelope.commit_sha}",
            "one or more repository files has no observed byte size",
        )
    return EvidenceValue(
        sum(item.size or 0 for item in envelope.files),
        EvidenceState.OBSERVED,
        f"Hub file metadata@{envelope.commit_sha}",
        "repository bytes include every exact-revision file, not only selected weights",
    )


def _trust_signals(
    envelope: RepositoryEnvelope, config: Mapping[str, object]
) -> tuple[TrustSignal, ...]:
    source = f"Hub repository inventory@{envelope.commit_sha}"
    signals: list[TrustSignal] = []
    if envelope.security_issues:
        signals.append(
            TrustSignal(
                "hub_security_scan",
                "danger",
                EvidenceState.CONFLICTING,
                f"Hub security status@{envelope.commit_sha}",
                "; ".join(envelope.security_issues),
            )
        )
    elif envelope.scans_done is True:
        signals.append(
            TrustSignal(
                "hub_security_scan",
                "info",
                EvidenceState.OBSERVED,
                f"Hub security status@{envelope.commit_sha}",
                "Hub scans completed with no reported file issues",
            )
        )
    else:
        signals.append(
            TrustSignal(
                "hub_security_scan",
                "unknown",
                EvidenceState.UNKNOWN,
                f"Hub security status@{envelope.commit_sha}",
                "scan completion or result is unavailable",
            )
        )
    paths = tuple(item.path for item in envelope.files)
    code_paths = tuple(
        path
        for path in paths
        if path.lower().endswith((".py", ".pyc", ".so", ".dylib", ".sh"))
    )
    if code_paths:
        signals.append(
            TrustSignal(
                "repository_code",
                "warning",
                EvidenceState.OBSERVED,
                source,
                f"repository contains executable-code paths: {', '.join(code_paths)}",
            )
        )
    unsafe_weights = tuple(
        path
        for path in paths
        if path.lower().endswith((".pkl", ".pickle", ".pt", ".pth", ".bin", ".ckpt"))
    )
    if unsafe_weights:
        signals.append(
            TrustSignal(
                "unsafe_serialization",
                "warning",
                EvidenceState.OBSERVED,
                source,
                "repository contains code-capable serialization paths: "
                + ", ".join(unsafe_weights),
            )
        )
    if config.get("auto_map") is not None or config.get("model_file") is not None:
        signals.append(
            TrustSignal(
                "remote_code_mapping",
                "warning",
                EvidenceState.OBSERVED,
                f"config.json@{envelope.commit_sha}",
                "configuration declares custom model code; revision-scoped trust is required",
            )
        )
    return tuple(signals)


def _validate_cache_observation(cache: CacheObservation) -> None:
    if cache.state not in {
        "absent",
        "present",
        "partial",
        "complete",
        "verified",
        "unknown",
    }:
        raise ModelIntelligenceError("local cache observation has an invalid state")
    if not cache.source:
        raise ModelIntelligenceError("local cache observation has no source")
    if cache.size_bytes is not None and (
        type(cache.size_bytes) is not int or cache.size_bytes < 0
    ):
        raise ModelIntelligenceError("local cache observation has an invalid size")


def _validate_machine(machine: MachineInventory) -> None:
    if (
        type(machine.total_memory_bytes) is not int
        or machine.total_memory_bytes <= 0
        or not machine.source
    ):
        raise ModelIntelligenceError("machine memory inventory is invalid")
    if machine.available_memory_bytes is not None and (
        type(machine.available_memory_bytes) is not int
        or machine.available_memory_bytes < 0
        or machine.available_memory_bytes > machine.total_memory_bytes
    ):
        raise ModelIntelligenceError("machine available-memory observation is invalid")


def _reject_json_constant(value: str) -> object:
    raise ModelIntelligenceError(f"non-finite JSON number is not allowed: {value}")


def _validate_json_structure(value: object) -> None:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ModelIntelligenceError("metadata exceeded the JSON node limit")
        if depth > MAX_JSON_DEPTH:
            raise ModelIntelligenceError("metadata exceeded the JSON nesting limit")
        if isinstance(item, str):
            if len(item) > MAX_JSON_STRING_LENGTH:
                raise ModelIntelligenceError("metadata exceeded the JSON string limit")
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > MAX_JSON_STRING_LENGTH:
                    raise ModelIntelligenceError(
                        "metadata contains an invalid JSON key"
                    )
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ModelIntelligenceError("metadata contains a non-finite JSON number")
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise ModelIntelligenceError("metadata contains an invalid JSON value")
