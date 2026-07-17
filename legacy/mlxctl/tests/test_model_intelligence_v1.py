from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from mlxctl.infrastructure.model_intelligence import (
    CacheObservation,
    EvidenceState,
    HuggingFaceModelRepository,
    MachineInventory,
    MetadataPayload,
    ModelIntelligence,
    ModelIntelligenceError,
    PsutilMachineInventory,
    RepositoryEnvelope,
    RepositoryFile,
    RuntimeObservation,
    optiq_kv_bytes,
)


GIB = 1024**3
SHA = "0123456789abcdef0123456789abcdef01234567"


class _Repository:
    def __init__(
        self,
        envelope: RepositoryEnvelope,
        payloads: dict[str, MetadataPayload],
        cache: CacheObservation | None = None,
    ) -> None:
        self.envelope = envelope
        self.payloads = payloads
        self.cache = cache or CacheObservation.absent()
        self.fetches: list[tuple[str, str, str, int]] = []

    def resolve(self, repo_id: str, revision: str) -> RepositoryEnvelope:
        return self.envelope

    def fetch_metadata(
        self, repo_id: str, commit_sha: str, path: str, *, max_bytes: int
    ) -> MetadataPayload | None:
        self.fetches.append((repo_id, commit_sha, path, max_bytes))
        return self.payloads.get(path)

    def cache_observation(self, repo_id: str, commit_sha: str) -> CacheObservation:
        return self.cache


class _Machine:
    def inspect(self) -> MachineInventory:
        return MachineInventory(
            total_memory_bytes=64 * GIB,
            source="sysctl hw.memsize",
        )


class _HubApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def model_info(self, repo_id: str, **kwargs: object) -> object:
        self.calls.append((repo_id, kwargs))
        sibling = type(
            "Sibling",
            (),
            {
                "rfilename": "config.json",
                "size": 123,
                "blob_id": "abc123",
                "lfs": None,
            },
        )()
        return type(
            "Info",
            (),
            {
                "id": "acme/Model",
                "sha": SHA,
                "siblings": (sibling,),
                "pipeline_tag": "text-generation",
                "library_name": "mlx",
                "tags": ("mlx",),
                "private": False,
                "gated": False,
                "disabled": False,
                "card_data": {"license": "apache-2.0"},
                "author": "acme",
                "created_at": "2026-07-01T00:00:00Z",
                "last_modified": "2026-07-15T00:00:00Z",
                "safetensors": type(
                    "SafeTensors",
                    (),
                    {"parameters": {"BF16": 10, "U32": 20}, "total": 30},
                )(),
                "security_repo_status": {
                    "scansDone": True,
                    "filesWithIssues": [],
                },
            },
        )()


class _MetadataFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []

    def fetch(
        self, repo_id: str, commit_sha: str, path: str, *, max_bytes: int
    ) -> MetadataPayload | None:
        self.calls.append((repo_id, commit_sha, path, max_bytes))
        return _json_payload(path, {"model_type": "qwen3"})


class _CacheInventory:
    def observe(self, repo_id: str, commit_sha: str) -> CacheObservation:
        return CacheObservation.absent()


def _json_payload(path: str, value: object) -> MetadataPayload:
    return MetadataPayload(
        path=path,
        content_type="application/json",
        body=json.dumps(value).encode(),
    )


class ModelIntelligenceTests(unittest.TestCase):
    def test_pinned_qwen_capacity_profiles_share_exact_kv_budget(self) -> None:
        config = {"head_dim": 256, "num_key_value_heads": 2}
        kv_config = [
            {"layer_idx": index, "bits": bits, "group_size": 64}
            for index, bits in zip(
                (3, 7, 11, 15, 19, 23, 27, 31, 35, 39),
                (4, 8, 4, 8, 8, 4, 4, 4, 4, 4),
                strict=True,
            )
        ]

        projections = {
            optiq_kv_bytes(config, kv_config, context_tokens=context, concurrency=count)
            for context, count in ((131_072, 6), (196_608, 4), (262_144, 3))
        }

        self.assertEqual(projections, {5_737_807_872})

    def test_machine_inventory_reports_unified_memory_capacity(self) -> None:
        inventory = PsutilMachineInventory(
            lambda: SimpleNamespace(total=64 * GIB, available=48 * GIB)
        ).inspect()

        self.assertEqual(inventory.total_memory_bytes, 64 * GIB)
        self.assertEqual(inventory.available_memory_bytes, 48 * GIB)
        self.assertEqual(inventory.source, "psutil virtual_memory")

    def test_hugging_face_adapter_requests_exact_bounded_metadata(self) -> None:
        api = _HubApi()
        fetcher = _MetadataFetcher()
        repository = HuggingFaceModelRepository(
            api=api, metadata_fetcher=fetcher, cache_inventory=_CacheInventory()
        )

        envelope = repository.resolve("acme/Model", "main")
        payload = repository.fetch_metadata(
            envelope.repo_id,
            envelope.commit_sha,
            "config.json",
            max_bytes=2048,
        )

        self.assertEqual(envelope.commit_sha, SHA)
        self.assertTrue(envelope.scans_done)
        self.assertEqual(envelope.files[0].size, 123)
        self.assertEqual(
            api.calls,
            [
                (
                    "acme/Model",
                    {
                        "revision": "main",
                        "files_metadata": True,
                        "securityStatus": True,
                        "timeout": 10.0,
                    },
                )
            ],
        )
        self.assertEqual(fetcher.calls[0][1], SHA)
        self.assertEqual(fetcher.calls[0][3], 2048)
        self.assertEqual(payload.path, "config.json")

    def test_inspects_arbitrary_repository_at_an_exact_revision_before_install(
        self,
    ) -> None:
        repository = _Repository(
            RepositoryEnvelope(
                repo_id="acme/Useful-Model",
                commit_sha=SHA,
                files=(RepositoryFile("config.json", 122),),
                pipeline_tag="text-generation",
                library_name="mlx",
                tags=("mlx", "conversational"),
                author="acme",
                safetensors={"parameters": {"BF16": 10, "U32": 20}, "total": 30},
            ),
            {"config.json": _json_payload("config.json", {"model_type": "qwen3"})},
        )

        report = ModelIntelligence(repository, _Machine()).inspect(
            "acme/Useful-Model", "main"
        )

        self.assertEqual(report.identity.repo_id, "acme/Useful-Model")
        self.assertEqual(report.identity.requested_revision, "main")
        self.assertEqual(report.identity.commit_sha, SHA)
        self.assertEqual(report.attributes["architecture"].value, "qwen3")
        self.assertEqual(
            report.attributes["architecture"].state, EvidenceState.OBSERVED
        )
        self.assertEqual(report.attributes["architecture"].source, f"config.json@{SHA}")
        self.assertEqual(report.attributes["task"].value, "text-generation")
        self.assertEqual(report.attributes["task"].state, EvidenceState.DECLARED)
        self.assertEqual(report.attributes["publisher"].value, "acme")
        self.assertEqual(report.attributes["parameters"].value["total"], 30)
        self.assertEqual(report.attributes["parameters"].state, EvidenceState.OBSERVED)
        self.assertEqual(report.cache.state, "absent")
        self.assertEqual(report.repository_files, repository.envelope.files)
        self.assertEqual(repository.fetches[0][1], SHA)

    def test_reports_optiq_kv_and_mtp_as_structural_evidence(self) -> None:
        repository = _Repository(
            RepositoryEnvelope(
                repo_id="mlx-community/Qwen-OptiQ",
                commit_sha=SHA,
                files=(
                    RepositoryFile("config.json", 512),
                    RepositoryFile("kv_config.json", 128),
                    RepositoryFile("aux/mtp.safetensors", 3 * GIB),
                ),
                tags=("optiq",),
            ),
            {
                "config.json": _json_payload(
                    "config.json",
                    {
                        "model_type": "qwen3_5_moe",
                        "mtp_model_path": "aux/mtp.safetensors",
                        "num_nextn_predict_layers": 1,
                    },
                ),
                "kv_config.json": _json_payload(
                    "kv_config.json", {"layers": {"0": {"bits": 4}}}
                ),
            },
        )

        report = ModelIntelligence(repository, _Machine()).inspect(
            "mlx-community/Qwen-OptiQ", SHA
        )

        artifacts = {item.role: item for item in report.artifacts}
        self.assertTrue(artifacts["optiq_kv_config"].present)
        self.assertEqual(artifacts["optiq_kv_config"].path, "kv_config.json")
        self.assertTrue(artifacts["mtp_weights"].present)
        self.assertEqual(artifacts["mtp_weights"].required_by, "config.json")
        self.assertEqual(report.capabilities["mtp"].value, True)
        self.assertEqual(report.capabilities["mtp"].state, EvidenceState.DERIVED)
        self.assertEqual(report.capabilities["optiq"].value, True)
        self.assertEqual(report.capabilities["optiq"].source, f"kv_config.json@{SHA}")

    def test_qualifies_compatibility_for_each_exact_runtime_installation(self) -> None:
        repository = _Repository(
            RepositoryEnvelope(
                repo_id="acme/Qwen-OptiQ",
                commit_sha=SHA,
                files=(
                    RepositoryFile("config.json", 128),
                    RepositoryFile("kv_config.json", 64),
                ),
            ),
            {
                "config.json": _json_payload("config.json", {"model_type": "qwen3"}),
                "kv_config.json": _json_payload("kv_config.json", {"layers": {}}),
            },
        )
        runtimes = (
            RuntimeObservation(
                installation_id="mlx-lm@0.31.3",
                runtime="mlx_lm",
                version="0.31.3",
                recognized_model_types=frozenset({"qwen3"}),
                capabilities=frozenset({"model", "host", "port"}),
                source="probed installation",
            ),
            RuntimeObservation(
                installation_id="optiq@0.3.3",
                runtime="optiq",
                version="0.3.3",
                recognized_model_types=frozenset({"qwen3"}),
                capabilities=frozenset({"kv_config", "mtp"}),
                source="probed installation",
            ),
            RuntimeObservation(
                installation_id="mlx-vlm@0.6.4",
                runtime="mlx_vlm",
                version="0.6.4",
                recognized_model_types=frozenset({"qwen2_vl"}),
                capabilities=frozenset({"image"}),
                source="probed installation",
            ),
            RuntimeObservation(
                installation_id="optiq@0.3.4-unclassified",
                runtime="optiq",
                version="0.3.4",
                recognized_model_types=frozenset(),
                capabilities=frozenset({"kv_config", "mtp"}),
                source="probed installation",
            ),
        )

        report = ModelIntelligence(repository, _Machine()).inspect(
            "acme/Qwen-OptiQ", SHA, runtimes=runtimes
        )

        compatibility = {item.installation_id: item for item in report.compatibility}
        self.assertEqual(compatibility["mlx-lm@0.31.3"].status, "candidate")
        self.assertEqual(compatibility["optiq@0.3.3"].status, "candidate")
        self.assertEqual(compatibility["mlx-vlm@0.6.4"].status, "unsupported")
        unclassified = compatibility["optiq@0.3.4-unclassified"]
        self.assertEqual(unclassified.status, "unknown")
        self.assertIn("recognition evidence is unavailable", unclassified.detail)
        self.assertEqual(
            compatibility["optiq@0.3.3"].capabilities,
            frozenset({"kv_config", "mtp"}),
        )
        self.assertIn("optiq@0.3.3", compatibility["optiq@0.3.3"].source)
        self.assertIn(SHA, compatibility["optiq@0.3.3"].source)

    def test_estimates_machine_fit_from_selected_weights_and_named_assumptions(
        self,
    ) -> None:
        repository = _Repository(
            RepositoryEnvelope(
                repo_id="acme/Standard-20B",
                commit_sha=SHA,
                files=(
                    RepositoryFile("config.json", 256),
                    RepositoryFile("model.safetensors.index.json", 128),
                    RepositoryFile("model-00001-of-00002.safetensors", 10 * GIB),
                    RepositoryFile("model-00002-of-00002.safetensors", 10 * GIB),
                    RepositoryFile("unused-adapter.safetensors", 2 * GIB),
                ),
            ),
            {
                "config.json": _json_payload(
                    "config.json",
                    {
                        "model_type": "qwen3",
                        "num_hidden_layers": 40,
                        "hidden_size": 4096,
                        "num_attention_heads": 32,
                        "num_key_value_heads": 8,
                    },
                ),
                "model.safetensors.index.json": _json_payload(
                    "model.safetensors.index.json",
                    {
                        "weight_map": {
                            "model.a": "model-00001-of-00002.safetensors",
                            "model.b": "model-00002-of-00002.safetensors",
                        }
                    },
                ),
            },
        )

        report = ModelIntelligence(repository, _Machine()).inspect(
            "acme/Standard-20B", SHA, context_tokens=32_768, concurrency=1
        )

        terms = {item.name: item for item in report.fit.terms}
        self.assertEqual(terms["selected tensor files"].low_bytes, 20 * GIB)
        self.assertEqual(terms["selected tensor files"].state, EvidenceState.OBSERVED)
        self.assertEqual(terms["KV cache"].low_bytes, 5 * GIB)
        self.assertEqual(terms["KV cache"].state, EvidenceState.DERIVED)
        self.assertIn("config.json", terms["KV cache"].source)
        self.assertEqual(report.fit.classification, "likely_fits")
        self.assertEqual(report.fit.context_tokens, 32_768)
        self.assertEqual(report.fit.concurrency, 1)
        self.assertGreater(report.fit.high_bytes, report.fit.low_bytes)
        self.assertEqual(report.fit.machine_memory_bytes, 64 * GIB)
        self.assertGreaterEqual(report.fit.reserved_headroom_bytes, 8 * GIB)

    def test_uses_per_layer_optiq_kv_metadata_for_hybrid_attention_fit(self) -> None:
        kv_layers = [
            {"layer_idx": layer, "bits": bits, "group_size": 64}
            for layer, bits in zip(
                (3, 7, 11, 15, 19, 23, 27, 31, 35, 39),
                (4, 8, 4, 8, 8, 4, 4, 4, 4, 4),
                strict=True,
            )
        ]
        repository = _Repository(
            RepositoryEnvelope(
                repo_id="mlx-community/Qwen-OptiQ",
                commit_sha=SHA,
                files=(
                    RepositoryFile("config.json", 512),
                    RepositoryFile("kv_config.json", 660),
                    RepositoryFile("model.safetensors", 20 * GIB),
                    RepositoryFile("optiq/mtp.safetensors", 1 * GIB),
                    RepositoryFile("optiq/optiq_vision.safetensors", 2 * GIB),
                ),
            ),
            {
                "config.json": _json_payload(
                    "config.json",
                    {
                        "model_type": "qwen3_5_moe",
                        "text_config": {
                            "num_hidden_layers": 40,
                            "head_dim": 256,
                            "num_attention_heads": 16,
                            "num_key_value_heads": 2,
                            "mtp_num_hidden_layers": 1,
                            "layer_types": ["linear_attention"] * 30
                            + ["full_attention"] * 10,
                        },
                        "mtp_file": "optiq/mtp.safetensors",
                        "optiq_vision": {"sidecar": "optiq/optiq_vision.safetensors"},
                    },
                ),
                "kv_config.json": _json_payload("kv_config.json", kv_layers),
            },
        )

        report = ModelIntelligence(repository, _Machine()).inspect(
            "mlx-community/Qwen-OptiQ", SHA, context_tokens=32_768
        )

        terms = {item.name: item for item in report.fit.terms}
        self.assertEqual(terms["KV cache"].low_bytes, 239_075_328)
        self.assertIn("kv_config.json", terms["KV cache"].source)
        artifacts = {item.role: item.path for item in report.artifacts}
        self.assertEqual(artifacts["mtp_weights"], "optiq/mtp.safetensors")
        self.assertEqual(
            artifacts["optiq_vision_weights"], "optiq/optiq_vision.safetensors"
        )

    def test_preserves_attribute_provenance_conflicts_and_unknown_capabilities(
        self,
    ) -> None:
        repository = _Repository(
            RepositoryEnvelope(
                repo_id="acme/Coder-Agent-Vision",
                commit_sha=SHA,
                files=(
                    RepositoryFile("config.json", 512),
                    RepositoryFile("tokenizer_config.json", 128),
                    RepositoryFile("custom_model.py", 64),
                    RepositoryFile("weights.pkl", 32),
                ),
                pipeline_tag="image-text-to-text",
                tags=("coding", "agents"),
                card_data={"license": "apache-2.0", "base_model": "acme/Base"},
                scans_done=True,
            ),
            {
                "config.json": _json_payload(
                    "config.json",
                    {
                        "model_type": "vision_text",
                        "max_position_embeddings": 131_072,
                        "vision_config": {"hidden_size": 1024},
                        "quantization": {"bits": 4, "group_size": 64},
                        "auto_map": {"AutoModel": "custom_model.Model"},
                    },
                ),
                "tokenizer_config.json": _json_payload(
                    "tokenizer_config.json", {"model_max_length": 32_768}
                ),
            },
        )

        report = ModelIntelligence(repository, _Machine()).inspect(
            "acme/Coder-Agent-Vision", SHA
        )

        self.assertEqual(report.attributes["license"].value, "apache-2.0")
        self.assertEqual(report.attributes["license"].state, EvidenceState.DECLARED)
        self.assertEqual(report.attributes["quantization"].value["bits"], 4)
        self.assertEqual(
            report.attributes["context_length"].state, EvidenceState.CONFLICTING
        )
        self.assertEqual(
            report.attributes["context_length"].value,
            {"config.json": 131_072, "tokenizer_config.json": 32_768},
        )
        self.assertEqual(report.capabilities["vision"].value, True)
        self.assertIsNone(report.capabilities["coding"].value)
        self.assertIsNone(report.capabilities["tool_use"].value)
        trust = {item.name: item for item in report.trust_signals}
        self.assertEqual(trust["hub_security_scan"].severity, "info")
        self.assertEqual(trust["repository_code"].severity, "warning")
        self.assertEqual(trust["unsafe_serialization"].severity, "warning")
        self.assertEqual(trust["remote_code_mapping"].severity, "warning")

    def test_rejects_unsafe_references_paths_and_unbounded_metadata(self) -> None:
        envelope = RepositoryEnvelope(
            repo_id="acme/Model",
            commit_sha=SHA,
            files=(RepositoryFile("config.json", 1),),
        )
        with self.subTest("URL reference"):
            with self.assertRaisesRegex(ModelIntelligenceError, "not a path or URL"):
                ModelIntelligence(_Repository(envelope, {}), _Machine()).inspect(
                    "https://huggingface.co/acme/Model", "main"
                )
        with self.subTest("traversal inventory"):
            unsafe = RepositoryEnvelope(
                repo_id="acme/Model",
                commit_sha=SHA,
                files=(RepositoryFile("../config.json", 1),),
            )
            with self.assertRaisesRegex(ModelIntelligenceError, "unsafe path"):
                ModelIntelligence(_Repository(unsafe, {}), _Machine()).inspect(
                    "acme/Model", "main"
                )
        with self.subTest("oversized payload"):
            payload = MetadataPayload(
                path="config.json",
                content_type="application/json",
                body=b" " * (2 * 1024 * 1024 + 1),
            )
            with self.assertRaisesRegex(ModelIntelligenceError, "byte limit"):
                ModelIntelligence(
                    _Repository(envelope, {"config.json": payload}), _Machine()
                ).inspect("acme/Model", "main")
        with self.subTest("artifact URL"):
            url_payload = _json_payload(
                "config.json", {"mtp_file": "https://example.invalid/mtp.safetensors"}
            )
            with self.assertRaisesRegex(ModelIntelligenceError, "must not be a URL"):
                ModelIntelligence(
                    _Repository(envelope, {"config.json": url_payload}), _Machine()
                ).inspect("acme/Model", "main")
        with self.subTest("deep JSON"):
            deep: object = "leaf"
            for _ in range(30):
                deep = {"next": deep}
            deep_payload = _json_payload("config.json", deep)
            with self.assertRaisesRegex(ModelIntelligenceError, "nesting limit"):
                ModelIntelligence(
                    _Repository(envelope, {"config.json": deep_payload}), _Machine()
                ).inspect("acme/Model", "main")
        with self.subTest("unbounded inventory"):
            huge_envelope = RepositoryEnvelope(
                repo_id="acme/Model",
                commit_sha=SHA,
                files=tuple(
                    RepositoryFile(f"files/{index}.json", 1) for index in range(20_001)
                ),
            )
            with self.assertRaisesRegex(ModelIntelligenceError, "file-count limit"):
                ModelIntelligence(_Repository(huge_envelope, {}), _Machine()).inspect(
                    "acme/Model", "main"
                )


if __name__ == "__main__":
    unittest.main()
    (HuggingFaceModelRepository,)
