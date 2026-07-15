import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mlxctl.application.catalogue import OperationKind, build_operation_catalogue
from mlxctl.application.config_schema import validate_config
from mlxctl.application.dispatch import ApplicationError, OperationRequest
from mlxctl.infrastructure.config_store import ConfigStore
from mlxctl.infrastructure.local_backend import LocalOperationBackend
from mlxctl.infrastructure.model_supply import (
    CacheInventory,
    CachedRevision,
    CatalogCandidate,
    ModelRevision,
    ModelSupply,
    VerificationResult,
)
from mlxctl.infrastructure.runtime_supply import RuntimeCatalogue
from mlxctl.infrastructure.state_store import OperationalStateStore


_EMPTY_CONFIG = """\
schema_version = 1

[gateway]
host = "127.0.0.1"
port = 8766
"""

_CONFIG = """\
schema_version = 1

[gateway]
host = "127.0.0.1"
port = 8766

[runtimes."optiq-0.2.18"]
definition = "optiq"
version = "0.2.18"
provenance = "tested"
root = "/Users/example/.local/share/mlxctl/runtimes/optiq-0.2.18"
launcher = ["/Users/example/.local/share/mlxctl/runtimes/optiq-0.2.18/bin/optiq", "serve"]
capabilities = ["model", "host", "port", "kv_config", "mtp"]
bundle_id = "optiq-0.2.18-py313-macos-arm64"

[models.qwen]
repository = "mlx-community/Qwen-OptiQ"
revision = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[aliases.coding]
installation = "qwen"

[services.coding]
model_alias = "coding"
runtime = "optiq-0.2.18"
route = "coding"
activation = "manual"
pinned = true

[services.chat]
model_alias = "coding"
runtime = "optiq-0.2.18"
route = "chat"
activation = "manual"
pinned = false

[clients.codex]
kind = "codex"
service = "coding"

[clients.codex.sampling.coding]
temperature = 0.0
"""

_MODEL_ONLY_CONFIG = """\
schema_version = 1

[models.qwen]
repository = "mlx-community/Qwen-OptiQ"
revision = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[aliases.coding]
installation = "qwen"
"""


class _NeverCalled:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected port call: {name}")


class _Port:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"state": "accepted"}

    def execute(self, operation, parameters):
        self.calls.append((operation, dict(parameters)))
        return self.result


class _SetupPort(_Port):
    def preview(self, parameters):
        return {
            "state": "review_required",
            "plan_fingerprint": "sha256:exact",
            "parameters": dict(parameters),
        }

    def preview_removal(self):
        return {
            "state": "review_required",
            "plan_fingerprint": "sha256:remove-exact",
            "steps": ({"id": "state.remove"},),
        }

    def remove(self, parameters):
        self.calls.append(("remove", dict(parameters)))
        return {"state": "complete", "removed": True}


class _Telemetry:
    def __init__(self, items=()):
        self.calls = []
        self.items = items

    def read(self, scope, resource=None):
        self.calls.append((scope, resource))
        return self.items

    def query(self, scope, resource=None):
        self.calls.append((scope, resource))
        return self.items


class _ModelSupply:
    revision_id = "mlx-community/Qwen-OptiQ@" + "a" * 40

    def __init__(self):
        self.calls = []

    def search(self, query, *, mode="curated", limit=20):
        self.calls.append(("search", query, mode, limit))
        return (
            CatalogCandidate(
                repo_id="mlx-community/Qwen-OptiQ",
                source="hub",
                evidence="hub-declared",
                reported_sha="a" * 40,
            ),
        )

    def resolve(self, repo_id, revision, *, offline=False):
        self.calls.append(("resolve", repo_id, revision, offline))
        return ModelRevision(repo_id, "c" * 40, revision, "hub-observed")

    def inventory(self):
        return CacheInventory(
            revisions=(
                CachedRevision(
                    revision_id=self.revision_id,
                    repo_id="mlx-community/Qwen-OptiQ",
                    commit_sha="a" * 40,
                    snapshot_path=Path("/cache/qwen"),
                    size_on_disk=42,
                    evidence="local-observed",
                    complete=True,
                ),
            ),
            evidence="local-observed",
            warnings=(),
        )

    def execute(self, operation, parameters):
        self.calls.append((operation, dict(parameters)))
        return {"job": "model-job", "state": "queued"}

    def verify(self, installation):
        self.calls.append(("verify", installation.installation_id))
        return VerificationResult("complete", "cache-completeness", ())


class _ModelIntelligence:
    def __init__(self):
        self.calls = []

    def inspect(self, repository, revision, **scenario):
        self.calls.append((repository, revision, scenario))
        return {"identity": {"repo_id": repository, "commit_sha": "b" * 40}}


class _DirectInstallSupply(_ModelSupply):
    execute = None

    def install(self, **parameters):
        self.calls.append(("install", parameters))
        return {"alias": parameters["alias"]}


class LocalOperationBackendTests(unittest.TestCase):
    def _backend(self, root: Path, config: str = _CONFIG, **ports):
        config_path = root / "config.toml"
        config_path.write_text(config, encoding="utf-8")
        state = OperationalStateStore(root / "state.sqlite3")
        backend = LocalOperationBackend(
            catalogue=build_operation_catalogue(),
            config_store=ConfigStore(config_path, validate_config),
            state_store=state,
            runtime_catalogue=RuntimeCatalogue.load_builtin(),
            runtime_supply=ports.get("runtime_supply", _NeverCalled()),
            model_supply=ports.get("model_supply", ModelSupply(_NeverCalled())),
            supervisor=ports.get("supervisor", _NeverCalled()),
            logs=ports.get("logs", _NeverCalled()),
            metrics=ports.get("metrics", _NeverCalled()),
            setup=ports.get("setup", _NeverCalled()),
            clients=ports.get("clients", _NeverCalled()),
            config_path=config_path,
            model_intelligence=ports.get("model_intelligence", _ModelIntelligence()),
        )
        return backend, state

    def test_empty_status_reports_stopped_without_activating_supervisor(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text(_EMPTY_CONFIG, encoding="utf-8")
            backend = LocalOperationBackend(
                catalogue=build_operation_catalogue(),
                config_store=ConfigStore(config_path, validate_config),
                state_store=OperationalStateStore(root / "state.sqlite3"),
                runtime_catalogue=RuntimeCatalogue.load_builtin(),
                runtime_supply=_NeverCalled(),
                model_supply=ModelSupply(_NeverCalled()),
                supervisor=_NeverCalled(),
                logs=_NeverCalled(),
                metrics=_NeverCalled(),
                setup=_NeverCalled(),
                clients=_NeverCalled(),
                config_path=config_path,
            )

            prepared = backend.prepare(OperationRequest("status"))
            result = prepared.execute()

            self.assertFalse(prepared.requires_supervisor)
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["supervisor"]["state"], "stopped")
            self.assertEqual(result["services"], [])
            self.assertEqual(result["operations"], [])
            self.assertEqual(result["active_operations"], 0)
            self.assertEqual(result["pressure"], "unknown")
            self.assertIn("mlxctl supervisor start", result["next_actions"])

    def test_uninitialized_status_and_config_are_actionable_without_a_file(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            state = OperationalStateStore(root / "state.sqlite3")
            backend = LocalOperationBackend(
                catalogue=build_operation_catalogue(),
                config_store=ConfigStore(config_path, validate_config),
                state_store=state,
                runtime_catalogue=RuntimeCatalogue.load_builtin(),
                runtime_supply=_NeverCalled(),
                model_supply=ModelSupply(_NeverCalled()),
                supervisor=_NeverCalled(),
                logs=_NeverCalled(),
                metrics=_NeverCalled(),
                setup=_NeverCalled(),
                clients=_NeverCalled(),
                config_path=config_path,
            )

            status = backend.prepare(OperationRequest("status")).execute()
            shown = backend.prepare(OperationRequest("config.show")).execute()

            self.assertEqual(status["services"], [])
            self.assertEqual(shown["state"], "uninitialized")
            self.assertIn("mlxctl setup", shown["next_actions"])
            self.assertFalse(config_path.exists())

    def test_service_list_keeps_desired_and_run_state_distinct(self) -> None:
        with TemporaryDirectory() as directory:
            backend, state = self._backend(Path(directory))
            state.put_snapshot(
                {
                    "kind": "service_run",
                    "id": "run-1",
                    "version": 1,
                    "service": "coding",
                    "state": "ready",
                    "upstream_port": 49152,
                }
            )

            result = backend.prepare(OperationRequest("service.list")).execute()

            self.assertEqual(
                [item["name"] for item in result["items"]], ["chat", "coding"]
            )
            coding = next(item for item in result["items"] if item["name"] == "coding")
            self.assertTrue(coding["desired"]["pinned"])
            self.assertEqual(coding["run"]["id"], "run-1")
            chat = next(item for item in result["items"] if item["name"] == "chat")
            self.assertIsNone(chat["run"])

    def test_diagnostic_queries_have_distinct_user_facing_results(self) -> None:
        with TemporaryDirectory() as directory:
            backend, state = self._backend(Path(directory))
            state.put_snapshot(
                {
                    "kind": "supervisor",
                    "id": "supervisor",
                    "version": 1,
                    "state": "running",
                }
            )
            state.put_snapshot(
                {
                    "kind": "gateway",
                    "id": "gateway",
                    "version": 1,
                    "state": "running",
                    "port": 9999,
                }
            )
            state.put_snapshot(
                {
                    "kind": "service_run",
                    "id": "run-1",
                    "version": 1,
                    "service": "coding",
                    "state": "unhealthy",
                }
            )
            state.put_operation({"id": "job-1", "status": "running"})

            status = backend.prepare(OperationRequest("status")).execute()
            check = backend.prepare(OperationRequest("check")).execute()
            doctor = backend.prepare(OperationRequest("doctor")).execute()
            supervisor = backend.prepare(
                OperationRequest("supervisor.inspect")
            ).execute()
            gateway_status = backend.prepare(
                OperationRequest("gateway.status")
            ).execute()
            gateway_inspect = backend.prepare(
                OperationRequest("gateway.inspect")
            ).execute()
            routes = backend.prepare(OperationRequest("gateway.routes")).execute()
            runtime = backend.prepare(OperationRequest("runtime.doctor")).execute()
            service = backend.prepare(
                OperationRequest("service.check", {"resource": "coding"})
            ).execute()

            self.assertNotIn("checks", status)
            self.assertEqual(check["checks"][0]["name"], "supervisor")
            self.assertFalse(doctor["healthy"])
            self.assertEqual(
                {issue["code"] for issue in doctor["issues"]},
                {"gateway_drift", "service_unhealthy"},
            )
            self.assertEqual(supervisor["operations"][0]["id"], "job-1")
            self.assertEqual(gateway_status["route_count"], 2)
            self.assertNotIn("routes", gateway_status)
            self.assertEqual(len(gateway_inspect["routes"]), 2)
            self.assertEqual(
                {item["service"] for item in routes["items"]}, {"chat", "coding"}
            )
            self.assertEqual(runtime["items"][0]["state"], "missing")
            self.assertEqual(service["checks"][2]["state"], "unavailable")

    def test_strict_resource_lookup_reports_unknown_service(self) -> None:
        with TemporaryDirectory() as directory:
            backend, _ = self._backend(Path(directory))

            with self.assertRaises(ApplicationError) as raised:
                backend.prepare(
                    OperationRequest("service.inspect", {"resource": "missing"})
                ).execute()

            self.assertEqual(raised.exception.code, "resource_not_found")

    def test_empty_metrics_are_reported_as_absent_not_invented(self) -> None:
        with TemporaryDirectory() as directory:
            metrics = _Telemetry()
            backend, _ = self._backend(Path(directory), metrics=metrics)

            result = backend.prepare(OperationRequest("metrics")).execute()

            self.assertEqual(result["items"], [])
            self.assertEqual(result["evidence"], ["no-metrics-observed"])
            self.assertEqual(metrics.calls, [("all", None)])

            backend.prepare(
                OperationRequest("metrics", {"resource": "coding"})
            ).execute()
            self.assertEqual(metrics.calls[-1], ("all", "coding"))

    def test_model_search_uses_the_cli_source_and_install_derives_alias(self) -> None:
        with TemporaryDirectory() as directory:
            supply = _DirectInstallSupply()
            backend, _ = self._backend(Path(directory), model_supply=supply)

            backend.prepare(
                OperationRequest(
                    "model.search",
                    {"query": "Qwen", "source": "broad", "limit": 3},
                )
            ).execute()
            backend.prepare(
                OperationRequest(
                    "model.install",
                    {
                        "repository": "mlx-community/Qwen-OptiQ",
                        "revision": "a" * 40,
                    },
                )
            ).execute()

            self.assertIn(("search", "Qwen", "broad", 3), supply.calls)
            install = next(call for call in supply.calls if call[0] == "install")
            self.assertEqual(install[1]["alias"], "Qwen-OptiQ")

    def test_model_install_executes_the_exact_revision_bound_in_preview(self) -> None:
        with TemporaryDirectory() as directory:
            supply = _ModelSupply()
            backend, _ = self._backend(Path(directory), model_supply=supply)
            request = OperationRequest(
                "model.install",
                {
                    "repository": "mlx-community/Qwen-OptiQ",
                    "revision": "main",
                },
            )
            preview = backend.prepare(request).events[-1]

            backend.prepare(
                OperationRequest(
                    "model.install",
                    {
                        **dict(request.parameters),
                        "confirmed": True,
                        "plan_fingerprint": preview["plan_fingerprint"],
                    },
                )
            ).execute()

            execution = next(
                call for call in supply.calls if call[0] == "model.install"
            )
            self.assertEqual(execution[1]["revision"], "c" * 40)

    def test_model_trust_is_exact_revision_and_runtime_scoped(self) -> None:
        with TemporaryDirectory() as directory:
            backend, state = self._backend(Path(directory))

            result = backend.prepare(
                OperationRequest(
                    "model.trust",
                    {
                        "resource": "coding",
                        "runtime": "optiq-0.2.18",
                        "accepted_risks": ["custom_code"],
                    },
                )
            ).execute()

            trust = result["resource"]
            self.assertEqual(trust["model_installation"], "qwen")
            self.assertEqual(trust["runtime_installation"], "optiq-0.2.18")
            self.assertEqual(trust["revision"], "a" * 40)
            self.assertEqual(trust["accepted_risks"], ["custom_code"])
            self.assertIsNotNone(
                state.snapshot("trust", "qwen@optiq-0.2.18", version="a" * 40)
            )

            with self.assertRaisesRegex(ApplicationError, "JSON array"):
                backend.prepare(
                    OperationRequest(
                        "model.trust",
                        {
                            "resource": "coding",
                            "runtime": "optiq-0.2.18",
                            "accepted_risks": "custom_code",
                        },
                    )
                ).execute()

    def test_model_inspect_accepts_arbitrary_repository_before_install(self) -> None:
        with TemporaryDirectory() as directory:
            intelligence = _ModelIntelligence()
            backend, _ = self._backend(Path(directory), model_intelligence=intelligence)

            result = backend.prepare(
                OperationRequest(
                    "model.inspect",
                    {
                        "repository": "mlx-community/New-OptiQ",
                        "revision": "main",
                        "context_tokens": 65536,
                        "concurrency": 2,
                    },
                )
            ).execute()

            self.assertEqual(intelligence.calls[0][0], "mlx-community/New-OptiQ")
            self.assertEqual(intelligence.calls[0][2]["context_tokens"], 65536)
            self.assertEqual(result["resource"]["identity"]["commit_sha"], "b" * 40)

    def test_config_import_reads_a_bounded_explicit_source(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            backend, _ = self._backend(root)
            source = root / "candidate.toml"
            source.write_text(_CONFIG.replace("port = 8766", "port = 9000"))

            request = OperationRequest("config.import", {"source": str(source)})
            preview = backend.prepare(request).events[-1]
            result = backend.prepare(
                OperationRequest(
                    "config.import",
                    {
                        "source": str(source),
                        "confirmed": True,
                        "plan_fingerprint": preview["plan_fingerprint"],
                    },
                )
            ).execute()

            self.assertEqual(result["resource"]["value"]["gateway"]["port"], 9000)

    def test_every_confirmed_mutation_is_bound_to_current_config_revision(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            backend, _ = self._backend(Path(directory))
            request = OperationRequest("service.remove", {"resource": "chat"})
            fingerprint = backend.prepare(request).events[-1]["plan_fingerprint"]

            backend._config_store.edit(  # noqa: SLF001 - verifies stale-plan boundary.
                lambda document: document["gateway"].update({"port": 9000})
            )

            with self.assertRaisesRegex(ApplicationError, "plan changed") as caught:
                backend.prepare(
                    OperationRequest(
                        "service.remove",
                        {
                            "resource": "chat",
                            "confirmed": True,
                            "plan_fingerprint": fingerprint,
                        },
                    )
                )
            self.assertEqual(caught.exception.code, "stale_plan")

    def test_local_service_edit_has_preview_and_never_uses_supervisor(self) -> None:
        with TemporaryDirectory() as directory:
            supervisor = _Port()
            backend, _ = self._backend(Path(directory), supervisor=supervisor)

            prepared = backend.prepare(
                OperationRequest(
                    "service.edit",
                    {"resource": "chat", "pinned": True},
                )
            )
            result = prepared.execute()

            self.assertFalse(prepared.requires_supervisor)
            self.assertTrue(prepared.events[0]["confirmation_required"])
            self.assertEqual(result["preview"]["operation"], "service.edit")
            self.assertEqual(supervisor.calls, [])
            inspected = backend.prepare(
                OperationRequest("service.inspect", {"resource": "chat"})
            ).execute()
            self.assertTrue(inspected["resource"]["desired"]["pinned"])

    def test_first_local_mutation_initializes_minimal_desired_state(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            backend, _ = self._backend(root, config=_EMPTY_CONFIG)
            (root / "config.toml").unlink()

            backend.prepare(
                OperationRequest("gateway.configure", {"port": 9001})
            ).execute()

            self.assertTrue((root / "config.toml").exists())
            shown = backend.prepare(OperationRequest("config.show")).execute()
            self.assertEqual(shown["resource"]["gateway"]["port"], 9001)

    def test_service_remove_drains_and_stops_before_deleting_desired_state(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            supervisor = _Port()
            backend, _ = self._backend(Path(directory), supervisor=supervisor)

            prepared = backend.prepare(
                OperationRequest("service.remove", {"resource": "chat"})
            )
            result = prepared.execute()

            self.assertTrue(prepared.requires_supervisor)
            self.assertEqual(
                [call[0] for call in supervisor.calls],
                ["service.remove"],
            )
            self.assertEqual(result["resource"]["service"], "chat")
            remaining = backend.prepare(OperationRequest("service.list")).execute()
            self.assertNotIn("chat", {item["name"] for item in remaining["items"]})

    def test_model_uninstall_removes_unreferenced_alias_with_installation(self) -> None:
        with TemporaryDirectory() as directory:
            backend, _ = self._backend(Path(directory), config=_MODEL_ONLY_CONFIG)

            backend.prepare(
                OperationRequest("model.uninstall", {"resource": "coding"})
            ).execute()

            shown = backend.prepare(OperationRequest("config.show")).execute()
            self.assertEqual(shown["resource"]["models"], {})
            self.assertEqual(shown["resource"]["aliases"], {})

    def test_service_create_uses_the_public_service_argument(self) -> None:
        with TemporaryDirectory() as directory:
            backend, _ = self._backend(Path(directory))

            backend.prepare(
                OperationRequest(
                    "service.create",
                    {
                        "service": "assistant",
                        "model_alias": "coding",
                        "runtime": "optiq-0.2.18",
                        "route": "assistant",
                    },
                )
            ).execute()

            listed = backend.prepare(OperationRequest("service.list")).execute()
            self.assertIn("assistant", [item["name"] for item in listed["items"]])

    def test_live_lifecycle_calls_only_supervisor_port(self) -> None:
        with TemporaryDirectory() as directory:
            supervisor = _Port({"run_id": "run-2", "state": "starting"})
            runtime = _Port()
            model = _ModelSupply()
            backend, _ = self._backend(
                Path(directory),
                supervisor=supervisor,
                runtime_supply=runtime,
                model_supply=model,
            )

            prepared = backend.prepare(
                OperationRequest("service.start", {"resource": "coding"})
            )
            result = prepared.execute()

            self.assertTrue(prepared.requires_supervisor)
            self.assertEqual(
                supervisor.calls, [("service.start", {"resource": "coding"})]
            )
            self.assertEqual(runtime.calls, [])
            self.assertEqual(model.calls, [])
            self.assertEqual(result["resource"]["run_id"], "run-2")

    def test_client_probe_content_is_forwarded_but_never_persisted(self) -> None:
        with TemporaryDirectory() as directory:
            clients = _Port({"response": "ephemeral"})
            backend, state = self._backend(Path(directory), clients=clients)

            result = backend.prepare(
                OperationRequest(
                    "client.test",
                    {"resource": "codex", "prompt": "say hello"},
                )
            ).execute()

            self.assertEqual(result["resource"]["response"], "ephemeral")
            self.assertEqual(state.operations(), ())
            self.assertEqual(state.events(), ())

    def test_setup_prepares_exact_plan_and_defers_activation_to_remote_steps(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            setup = _SetupPort()
            backend, _ = self._backend(Path(directory), setup=setup)

            prepared = backend.prepare(OperationRequest("setup"))

            self.assertFalse(prepared.requires_supervisor)
            self.assertEqual(prepared.events[0]["plan_fingerprint"], "sha256:exact")
            self.assertEqual(setup.calls, [])

    def test_product_removal_previews_exact_plan_without_starting_supervisor(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            setup = _SetupPort()
            backend, _ = self._backend(Path(directory), setup=setup)

            prepared = backend.prepare(OperationRequest("remove"))

            self.assertFalse(prepared.requires_supervisor)
            self.assertEqual(
                prepared.events[0]["plan_fingerprint"], "sha256:remove-exact"
            )
            result = prepared.execute()
            self.assertTrue(result["resource"]["removed"])
            self.assertEqual(setup.calls, [("remove", {})])

    def test_runtime_and_model_jobs_call_only_their_supply_ports(self) -> None:
        with TemporaryDirectory() as directory:
            runtime = _Port({"job": "runtime-job"})
            model = _ModelSupply()
            supervisor = _Port()
            backend, _ = self._backend(
                Path(directory),
                runtime_supply=runtime,
                model_supply=model,
                supervisor=supervisor,
            )

            runtime_result = backend.prepare(
                OperationRequest("runtime.update", {"resource": "optiq-0.2.18"})
            ).execute()
            model_result = backend.prepare(
                OperationRequest(
                    "model.repair",
                    {"resource": "qwen"},
                )
            ).execute()

            self.assertEqual(runtime_result["resource"]["job"], "runtime-job")
            self.assertEqual(model_result["resource"]["job"], "model-job")
            self.assertEqual(runtime.calls[0][0], "runtime.update")
            self.assertEqual(model.calls[0][0], "model.repair")
            self.assertEqual(supervisor.calls, [])

    def test_model_verify_uses_exact_installed_revision_and_cache(self) -> None:
        with TemporaryDirectory() as directory:
            model = _ModelSupply()
            backend, _ = self._backend(Path(directory), model_supply=model)

            result = backend.prepare(
                OperationRequest("model.verify", {"resource": "coding"})
            ).execute()

            self.assertEqual(result["resource"]["status"], "complete")
            self.assertIn(("verify", "qwen"), model.calls)
            self.assertEqual(result["evidence"], ["cache-completeness"])

    def test_every_catalogue_entry_prepares_with_realistic_prerequisites(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            backend, state = self._backend(
                root,
                runtime_supply=_Port(),
                model_supply=_ModelSupply(),
                supervisor=_Port(),
                logs=_Telemetry(),
                metrics=_Telemetry(),
                setup=_Port(),
                clients=_Port(),
            )
            state.put_operation({"id": "job-1", "state": "running"})
            for name in build_operation_catalogue():
                with self.subTest(operation=name):
                    prepared = backend.prepare(
                        OperationRequest(name, self._parameters(name))
                    )
                    self.assertIsNotNone(prepared.execute)

    def test_every_query_executes_without_supervisor_activation(self) -> None:
        with TemporaryDirectory() as directory:
            backend, state = self._backend(
                Path(directory),
                runtime_supply=_Port(),
                model_supply=_ModelSupply(),
                supervisor=_NeverCalled(),
                logs=_Telemetry(),
                metrics=_Telemetry(),
                setup=_Port(),
                clients=_Port(),
            )
            state.put_operation({"id": "job-1", "state": "running"})
            for name, operation in build_operation_catalogue().items():
                if operation.kind is not OperationKind.QUERY:
                    continue
                with self.subTest(operation=name):
                    prepared = backend.prepare(
                        OperationRequest(name, self._parameters(name))
                    )
                    result = prepared.execute()
                    self.assertFalse(prepared.requires_supervisor)
                    self.assertEqual(result["schema_version"], 1)
                    self.assertEqual(result["operation"], name)

    def test_mutation_categories_have_preview_and_exact_activation_policy(self) -> None:
        supervisor_backed = {
            "supervisor.start",
            "supervisor.stop",
            "supervisor.restart",
            "gateway.restart",
            "runtime.install",
            "runtime.adopt",
            "runtime.update",
            "runtime.rollback",
            "runtime.remove",
            "runtime.prune",
            "model.install",
            "model.repair",
            "model.update",
            "model.rollback",
            "model.cache.evict",
            "model.cache.prune",
            "service.start",
            "service.stop",
            "service.restart",
            "service.remove",
        }
        with TemporaryDirectory() as directory:
            backend, state = self._backend(
                Path(directory),
                model_supply=_ModelSupply(),
                setup=_SetupPort(),
            )
            state.put_operation({"id": "job-1", "state": "running"})
            for name, operation in build_operation_catalogue().items():
                if operation.kind is not OperationKind.MUTATION:
                    continue
                with self.subTest(operation=name):
                    prepared = backend.prepare(
                        OperationRequest(name, self._parameters(name))
                    )
                    self.assertEqual(
                        prepared.requires_supervisor, name in supervisor_backed
                    )
                    self.assertEqual(prepared.events[0]["phase"], "preview")
                    self.assertTrue(
                        str(prepared.events[0]["plan_fingerprint"]).startswith(
                            "sha256:"
                        )
                    )
                    self.assertEqual(
                        prepared.events[0]["confirmation_required"],
                        operation.confirmation,
                    )

    @staticmethod
    def _parameters(name):
        if name.startswith("runtime."):
            if name in {"runtime.install", "runtime.adopt", "runtime.available"}:
                return {"runtime": "optiq"}
            return {"resource": "optiq-0.2.18"}
        if name == "model.search":
            return {"query": "Qwen"}
        if name.startswith("model.cache."):
            return {"resource": _ModelSupply.revision_id}
        if name.startswith("model."):
            return {
                "resource": "qwen",
                "alias": "coding",
                "repository": "mlx-community/Qwen-OptiQ",
                "revision": "a" * 40,
            }
        if name.startswith("service."):
            return {"resource": "coding"}
        if name.startswith("operation."):
            return {"resource": "job-1"}
        if name.startswith("client."):
            return {"resource": "codex"}
        if name == "config.diff":
            return {"text": _CONFIG}
        if name == "config.import":
            return {"text": _CONFIG}
        if name == "config.restore":
            return {"revision": "a" * 64}
        return {}


if __name__ == "__main__":
    unittest.main()
