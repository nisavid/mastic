from __future__ import annotations

import asyncio
import json
import os
import plistlib
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mastic.application.config_schema import validate_config
from mastic.application.dispatch import ApplicationError, OperationRequest
from mastic.application.setup import SetupIntent, SetupPreflight, SetupRequest
from mastic.infrastructure.config_store import ConfigStore
from mastic.infrastructure.model_supply import CacheInventory, CachedRevision
from mastic.infrastructure.gateway_credential import GatewayCredential
from mastic.infrastructure.paths_v1 import MasticPaths
from mastic.infrastructure.daemon_service import DaemonOperationRouter, DaemonService
from mastic.infrastructure.production import (
    _ActivatingOperationOwner,
    _GatewayMutationGuard,
    _LocalSupervisorOwner,
    _LocalModelSupply,
    _SetupSupervisorOwner,
    _sampling_matches_service_model,
    _setup_resolver,
    compose_daemon,
    compose_local,
    make_launchd,
)
from mastic.infrastructure.production_host import (
    AbsoluteUvRunner,
    GatewayVerificationPort,
    OwnedStateRemover,
    coherent_application_target_context,
    configured_model_installations,
    default_sampling,
    removal_inventory,
    resolve_uv,
    sampling_profile,
)
from mastic.infrastructure.state_store import OperationalStateStore


class _Port:
    def __init__(self, result=None) -> None:
        self.calls = []
        self.result = result or {"state": "running"}

    def execute(self, operation, parameters):
        self.calls.append((operation, dict(parameters)))
        return dict(self.result)


class _Activator:
    def __init__(self) -> None:
        self.calls = 0

    def activate(self) -> None:
        self.calls += 1


class _LaunchdStatus:
    def __init__(self, running: bool, registered: bool = True) -> None:
        self.running = running
        self.registered = registered


class _Launchd:
    def __init__(self, running: bool) -> None:
        self.running = running
        self.registered = True
        self.bootout_calls = 0

    def status(self):
        return _LaunchdStatus(self.running, self.registered)

    def bootout(self):
        self.bootout_calls += 1
        self.running = False
        self.registered = False
        return self.status()


class ProductionCompositionTests(unittest.TestCase):
    @staticmethod
    def _profile_binding_config(*, revision: str):
        return validate_config(
            {
                "schema_version": 1,
                "runtimes": {
                    "optiq@0.3.3": {
                        "definition": "optiq",
                        "version": "0.3.3",
                        "provenance": "tested",
                        "root": "/tmp/optiq",
                        "launcher": ["/tmp/optiq/bin/optiq", "serve"],
                        "capabilities": ["model"],
                    }
                },
                "models": {
                    "qwen": {
                        "repository": "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit",
                        "revision": revision,
                    }
                },
                "aliases": {"qwen": {"installation": "qwen"}},
                "services": {
                    "coding": {
                        "model_alias": "qwen",
                        "runtime": "optiq@0.3.3",
                        "route": "coding",
                    }
                },
            }
        )

    def test_application_target_context_defaults_to_and_enforces_service_cap(
        self,
    ) -> None:
        self.assertEqual(coherent_application_target_context(131_072, None), 131_072)
        self.assertEqual(coherent_application_target_context(131_072, 131_072), 131_072)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            coherent_application_target_context(None, 0)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            coherent_application_target_context(None, -1)
        with self.assertRaisesRegex(ValueError, "explicit null"):
            coherent_application_target_context(
                131_072, None, requested_context_present=True
            )
        with self.assertRaisesRegex(ValueError, "must match"):
            coherent_application_target_context(131_072, 196_608)

    def test_local_model_resolution_is_side_effect_free_and_stays_local(self) -> None:
        class Supply:
            def resolve(self, repo_id, revision, *, offline=False):
                return (repo_id, revision, offline)

        remote = _Port()
        model = _LocalModelSupply(Supply(), remote, object())

        self.assertEqual(
            model.resolve("owner/model", "main", offline=True),
            ("owner/model", "main", True),
        )
        self.assertEqual(remote.calls, [])

    def test_local_composition_prepares_private_paths_before_store_construction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )

            with patch.object(
                paths.__class__, "prepare", wraps=paths.prepare
            ) as prepare:
                compose_local(
                    paths=paths, home=root, executable=Path("/usr/bin/python3")
                )

            self.assertGreaterEqual(prepare.call_count, 1)

    def test_setup_remote_owner_activates_only_at_execution_boundary(self) -> None:
        activator = _Activator()
        remote = _Port({"state": "complete"})
        owner = _ActivatingOperationOwner(activator, remote)

        self.assertEqual(activator.calls, 0)
        owner.execute("runtime.install", {"runtime": "optiq"})

        self.assertEqual(activator.calls, 1)
        self.assertEqual(remote.calls[0][0], "runtime.install")

    def test_setup_supervisor_activation_is_visible_and_idempotently_forwarded(
        self,
    ) -> None:
        activator = _Activator()
        remote = _Port({"state": "running"})

        owner = _SetupSupervisorOwner(remote, _Launchd(running=False), activator)
        result = owner.execute("supervisor.start", {})

        self.assertEqual(activator.calls, 1)
        self.assertEqual(remote.calls, [("supervisor.start", {})])
        self.assertEqual(result["state"], "running")

    def test_setup_recycles_a_running_supervisor_before_loading_new_code(self) -> None:
        activator = _Activator()
        remote = _Port({"state": "running"})
        launchd = _Launchd(running=True)

        owner = _SetupSupervisorOwner(remote, launchd, activator)
        result = owner.execute("supervisor.start", {})

        self.assertEqual(launchd.bootout_calls, 1)
        self.assertEqual(activator.calls, 1)
        self.assertEqual(remote.calls, [("supervisor.start", {})])
        self.assertEqual(result["state"], "running")

    def test_request_profile_is_bound_to_the_service_exact_model_revision(self) -> None:
        exact_revision = "70a3aa32c7feef511182bf16aa332f37e8d82014"
        config = self._profile_binding_config(revision=exact_revision)
        sampling = default_sampling(
            "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit",
            exact_revision,
            "codex",
        )["coding"]

        self.assertTrue(
            _sampling_matches_service_model(config, config.services["coding"], sampling)
        )

        other = self._profile_binding_config(revision="a" * 40)
        self.assertFalse(
            _sampling_matches_service_model(other, other.services["coding"], sampling)
        )

    def test_local_supervisor_stop_is_idempotent_without_remote_activation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _Port({"state": "stopping"})
            owner = _LocalSupervisorOwner(
                remote,
                _Launchd(running=False),
                root / "masticd.sock",
                OperationalStateStore(root / "state.db"),
                ConfigStore(root / "config.toml", validate_config),
            )

            result = owner.execute("supervisor.stop", {})

        self.assertEqual(result, {"state": "stopped", "already_stopped": True})
        self.assertEqual(remote.calls, [])

    def test_local_supervisor_stop_forwards_when_launchd_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _Port({"state": "stopping"})
            owner = _LocalSupervisorOwner(
                remote,
                _Launchd(running=True),
                root / "masticd.sock",
                OperationalStateStore(root / "state.db"),
                ConfigStore(root / "config.toml", validate_config),
            )

            result = owner.execute("supervisor.stop", {})

        self.assertEqual(result, {"state": "stopping"})
        self.assertEqual(remote.calls, [("supervisor.stop", {})])

    def test_local_supervisor_stop_forwards_to_foreground_socket_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "masticd.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(str(path))
            listener.listen(1)
            remote = _Port({"state": "stopping"})
            owner = _LocalSupervisorOwner(
                remote,
                _Launchd(running=False),
                path,
                OperationalStateStore(root / "state.db"),
                ConfigStore(root / "config.toml", validate_config),
            )

            result = owner.execute("supervisor.stop", {})

        self.assertEqual(result, {"state": "stopping"})
        self.assertEqual(remote.calls, [("supervisor.stop", {})])

    def test_local_supervisor_stop_reconciles_a_stale_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "masticd.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))
            stale.close()
            remote = _Port({"state": "stopping"})
            owner = _LocalSupervisorOwner(
                remote,
                _Launchd(running=False),
                path,
                OperationalStateStore(root / "state.db"),
                ConfigStore(root / "config.toml", validate_config),
            )

            result = owner.execute("supervisor.stop", {})

        self.assertEqual(result, {"state": "stopped", "already_stopped": True})
        self.assertEqual(remote.calls, [])

    def test_inactive_supervisor_stop_reconciles_stale_running_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = OperationalStateStore(root / "state.db")
            config = ConfigStore(root / "config.toml", validate_config)
            config.import_text(
                "schema_version = 1\n[gateway]\nhost = '127.0.0.1'\nport = 9876\n"
            )
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
                    "version": 2,
                    "state": "running",
                    "host": "127.0.0.1",
                    "port": 9876,
                }
            )
            state.put_snapshot(
                {
                    "kind": "service_run",
                    "id": "coding/run-1",
                    "version": 3,
                    "service": "coding",
                    "run_id": "run-1",
                    "state": "ready",
                    "pid": 123,
                }
            )
            state.put_operation(
                {
                    "id": "operation-1",
                    "kind": "service.start",
                    "resource": "coding",
                    "status": "running",
                }
            )
            versions = iter(range(10, 20))
            owner = _LocalSupervisorOwner(
                _Port(),
                _Launchd(running=False),
                root / "masticd.sock",
                state,
                config,
                clock=lambda: next(versions),
            )

            owner.execute("supervisor.stop", {})

            self.assertEqual(
                state.snapshot("supervisor", "supervisor")["state"], "stopped"
            )
            gateway = state.snapshot("gateway", "gateway")
            self.assertEqual(gateway["state"], "stopped")
            self.assertEqual(gateway["port"], 9876)
            service = state.snapshot("service_run", "coding/run-1")
            self.assertEqual(service["state"], "stopped")
            self.assertNotIn("pid", service)
            operation = state.operation("operation-1")
            self.assertEqual(operation["status"], "failed")
            self.assertEqual(operation["outcome"], "interrupted")
            self.assertEqual(state.events("operation-1")[-1]["kind"], "interrupted")
            self.assertFalse(
                {
                    item.get("status")
                    for item in state.operations()
                    if item.get("status") in {"queued", "running", "resuming"}
                }
            )

    def test_running_gateway_endpoint_edit_fails_before_preview_or_execution(
        self,
    ) -> None:
        dispatcher = _Port()
        guard = _GatewayMutationGuard(dispatcher, _Launchd(running=True))

        with self.assertRaisesRegex(ApplicationError, "Stop the Supervisor"):
            guard.preview(OperationRequest("gateway.configure", {"port": 9000}))

        self.assertEqual(dispatcher.calls, [])

    def test_live_control_socket_blocks_gateway_endpoint_edit(self) -> None:
        dispatcher = _Port()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "masticd.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(str(path))
            listener.listen(1)
            guard = _GatewayMutationGuard(dispatcher, _Launchd(running=False), path)

            with self.assertRaisesRegex(ApplicationError, "Stop the Supervisor"):
                guard.execute(OperationRequest("gateway.configure", {"port": 9000}))

        self.assertEqual(dispatcher.calls, [])

    def test_running_supervisor_allows_reconcilable_service_edit(self) -> None:
        class Dispatcher:
            def __init__(self):
                self.calls = []

            def execute(self, request):
                self.calls.append(request)
                return {"edited": True}

        dispatcher = Dispatcher()
        guard = _GatewayMutationGuard(dispatcher, _Launchd(running=True))
        request = OperationRequest("service.edit", {"resource": "coding"})

        self.assertEqual(guard.execute(request), {"edited": True})
        self.assertEqual(dispatcher.calls, [request])

    def test_application_target_sampling_defaults_cover_coding_and_memory_operations(
        self,
    ) -> None:
        repository = "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit"
        revision = "70a3aa32c7feef511182bf16aa332f37e8d82014"
        coding = default_sampling(repository, revision, "codex")["coding"]
        self.assertIs(sampling_profile(coding), coding)
        with self.assertRaisesRegex(ValueError, "unknown sampling profile fields"):
            sampling_profile({"temprature": 0.6})
        self.assertEqual(coding.temperature, 0.6)
        self.assertEqual(coding.top_p, 0.95)
        self.assertEqual(coding.top_k, 20)
        self.assertEqual(coding.presence_penalty, 0.0)
        self.assertTrue(coding.enable_thinking)
        hindsight = default_sampling(repository, revision, "hindsight")
        self.assertEqual(hindsight["verification"].temperature, 0.7)
        self.assertEqual(hindsight["retain"].temperature, 0.7)
        self.assertFalse(hindsight["retain"].enable_thinking)
        self.assertEqual(hindsight["reflect"].temperature, 1.0)
        self.assertTrue(hindsight["reflect"].enable_thinking)
        self.assertEqual(hindsight["consolidation"].temperature, 0.7)
        self.assertEqual(default_sampling(repository, "0" * 40, "codex"), {})

    def test_local_status_neither_inspects_nor_activates_launchd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )

            production = compose_local(
                paths=paths, home=root, executable=Path("/usr/bin/python3")
            )
            result = production.application.dispatcher.execute(
                OperationRequest("status")
            )

            self.assertEqual(result.value["state"], "stopped")
            self.assertFalse(
                (root / "Library/LaunchAgents/io.nisavid.masticd.plist").exists()
            )

            inspected = production.application.dispatcher.execute(
                OperationRequest("gateway.inspect")
            ).value
            credential = inspected["credential"]
            self.assertEqual(credential["scheme"], "Bearer")
            self.assertEqual(credential["path"], str(paths.gateway_credential))
            self.assertEqual(set(credential), {"scheme", "path", "instructions"})

    def test_daemon_graph_composes_without_binding_or_starting_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )

            daemon = compose_daemon(paths=paths, home=root)

            self.assertIsInstance(daemon, DaemonService)
            self.assertFalse(paths.control_socket.exists())
            self.assertTrue(paths.gateway_credential.exists())
            self.assertEqual(
                stat.S_IMODE(paths.gateway_credential.stat().st_mode), 0o600
            )

    def test_daemon_graph_resolves_uv_only_when_runtime_work_begins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )
            with patch(
                "mastic.infrastructure.production.resolve_uv",
                side_effect=AssertionError("uv resolved during daemon composition"),
            ) as resolve:
                daemon = compose_daemon(paths=paths, home=root)

            self.assertIsInstance(daemon, DaemonService)
            resolve.assert_not_called()

    def test_production_graphs_reject_adoption_inside_owned_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )
            local = compose_local(
                paths=paths, home=root, executable=Path("/usr/bin/python3")
            )
            snapshot = paths.data_dir / "external-snapshot"
            snapshot.mkdir()
            (snapshot / "weights.bin").write_bytes(b"externally owned")
            parameters = {
                "repository": "owner/model",
                "revision": "a" * 40,
                "path": str(snapshot),
            }

            with self.assertRaisesRegex(Exception, "mastic-owned"):
                local.application.dispatcher.preview(
                    OperationRequest("model.adopt", parameters)
                )

            daemon = compose_daemon(paths=paths, home=root)
            router = daemon._router_factory(lambda: None)
            with self.assertRaisesRegex(ApplicationError, "mastic-owned"):
                router.execute("model.adopt", parameters)

    def test_launchd_definition_is_inactive_and_uses_private_module_target(
        self,
    ) -> None:
        adapter = make_launchd(
            executable=Path("/usr/bin/python3"), home=Path("/Users/example")
        )

        payload = plistlib.loads(adapter.preview())

        self.assertFalse(payload["KeepAlive"])
        self.assertFalse(payload["RunAtLoad"])
        self.assertEqual(
            payload["ProgramArguments"],
            ["/usr/bin/python3", "-m", "mastic.entrypoints", "daemon"],
        )
        self.assertEqual(
            payload["StandardOutPath"],
            "/Users/example/Library/Logs/mastic/supervisor.log",
        )
        self.assertEqual(payload["StandardErrorPath"], payload["StandardOutPath"])
        self.assertEqual(payload["Umask"], 0o077)

    def test_local_composition_preserves_tool_environment_interpreter_symlink(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_interpreter = root / "base-python"
            base_interpreter.touch()
            tool_interpreter = root / "tool-environment-python"
            tool_interpreter.symlink_to(base_interpreter)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )

            production = compose_local(
                paths=paths, home=root, executable=tool_interpreter
            )
            payload = plistlib.loads(production.launchd.preview())

        self.assertEqual(
            payload["ProgramArguments"][0], str(tool_interpreter.absolute())
        )
        self.assertNotEqual(payload["ProgramArguments"][0], str(base_interpreter))

    @patch("mastic.infrastructure.production_host.subprocess.run")
    def test_runtime_installer_uses_configured_absolute_uv(self, run) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "uv"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o700)
            with patch.dict(
                os.environ,
                {"MASTIC_UV_EXECUTABLE": str(executable)},
                clear=False,
            ):
                resolved = resolve_uv(Path(directory))
            AbsoluteUvRunner(resolved).run(("uv", "--version"))

        self.assertEqual(
            run.call_args.args[0], (str(executable.resolve()), "--version")
        )
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30 * 60)

    def test_invalid_explicit_uv_path_fails_without_falling_back_to_discovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fallback = Path(directory) / "uv"
            fallback.write_text("#!/bin/sh\n", encoding="utf-8")
            fallback.chmod(0o700)
            for configured in (str(Path(directory) / "missing"), ""):
                with (
                    self.subTest(configured=configured),
                    patch.dict(
                        os.environ,
                        {"MASTIC_UV_EXECUTABLE": configured},
                        clear=False,
                    ),
                    patch(
                        "mastic.infrastructure.production_host.shutil.which",
                        return_value=str(fallback),
                    ),
                    self.assertRaisesRegex(FileNotFoundError, "MASTIC_UV_EXECUTABLE"),
                ):
                    resolve_uv(Path(directory))

    def test_router_dispatches_all_physical_owner_families(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            runtime = _Port({"installation_id": "runtime"})
            model = _Port({"installation_id": "model"})
            supervisor = _Port({"state": "stopped"})
            stops = []
            router = DaemonOperationRouter(
                runtime=runtime,
                model=model,
                supervisor=supervisor,
                state=state,
                request_stop=lambda: stops.append(True),
            )

            router.execute("runtime.install", {"runtime": "optiq"})
            router.execute("model.install", {"repository": "owner/model"})
            router.execute("service.drain", {"resource": "coding"})
            router.execute("supervisor.stop", {})

            self.assertEqual(runtime.calls[0][0], "runtime.install")
            self.assertEqual(model.calls[0][0], "model.install")
            self.assertEqual(supervisor.calls[0][0], "service.drain")
            self.assertTrue(stops)
            self.assertEqual(
                state.operations()[0]["status"],
                "complete",
            )
            self.assertEqual(
                state.snapshot("supervisor", "supervisor")["state"], "stopped"
            )
            self.assertEqual(state.snapshot("gateway", "gateway")["port"], 8766)
            self.assertEqual(
                {metric["scope"] for metric in state.metrics()},
                {"gateway", "supervisor"},
            )

    def test_explicit_stop_and_service_finalization_stop_physical_state_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = _Port({"state": "stopped"})
            router = DaemonOperationRouter(
                runtime=_Port(),
                model=_Port(),
                supervisor=supervisor,
                state=OperationalStateStore(Path(directory) / "state.sqlite3"),
            )

            router.execute("supervisor.stop", {}, operation_id="stop-1")
            finalized = router.stop()

            self.assertEqual(finalized["state"], "stopped")
            self.assertEqual(supervisor.calls, [("supervisor.stop", {})])

    def test_completed_stop_replays_exactly_and_rejects_conflicting_id_reuse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = _Port({"state": "stopped", "generation": 7})
            router = DaemonOperationRouter(
                runtime=_Port(),
                model=_Port(),
                supervisor=supervisor,
                state=OperationalStateStore(Path(directory) / "state.sqlite3"),
            )

            first = router.execute("supervisor.stop", {}, operation_id="stop-exact")
            replay = router.execute("supervisor.stop", {}, operation_id="stop-exact")

            self.assertEqual(replay, first)
            with self.assertRaises(ApplicationError) as raised:
                router.execute(
                    "supervisor.stop",
                    {"force": True},
                    operation_id="stop-exact",
                )
            self.assertEqual(raised.exception.code, "operation_id_conflict")
            self.assertEqual(supervisor.calls, [("supervisor.stop", {})])

    def test_completed_mutation_is_not_reexecuted_when_final_event_write_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            runtime = _Port({"state": "complete"})
            router = DaemonOperationRouter(
                runtime=runtime,
                model=_Port(),
                supervisor=_Port(),
                state=state,
            )
            original_append_once = state.append_event_once
            attempts = 0

            def append_event_once(event):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("journal unavailable")
                return original_append_once(event)

            with patch.object(
                state, "append_event_once", side_effect=append_event_once
            ):
                first = router.execute(
                    "runtime.install",
                    {"runtime": "optiq"},
                    operation_id="runtime-1",
                )
                second = router.execute(
                    "runtime.install",
                    {"runtime": "optiq"},
                    operation_id="runtime-1",
                )
                third = router.execute(
                    "runtime.install",
                    {"runtime": "optiq"},
                    operation_id="runtime-1",
                )

            self.assertTrue(first["journal_reconciliation_required"])
            self.assertEqual(second["state"], "complete")
            self.assertEqual(third, second)
            self.assertEqual(runtime.calls, [("runtime.install", {"runtime": "optiq"})])
            self.assertEqual(
                [event["kind"] for event in state.events("runtime-1")],
                ["started", "complete"],
            )
            self.assertFalse(state.operation("runtime-1")["terminal_event_pending"])

    def test_startup_reconciles_a_pending_terminal_event_without_reexecution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = OperationalStateStore(Path(directory) / "state.sqlite3")
            state.put_operation(
                {
                    "id": "runtime-before-restart",
                    "kind": "runtime.install",
                    "resource": "optiq",
                    "status": "complete",
                    "request_fingerprint": "sha256:" + "a" * 64,
                    "terminal_event_pending": True,
                    "result": {"state": "complete"},
                }
            )
            state.append_event(
                {
                    "kind": "started",
                    "operation_id": "runtime-before-restart",
                    "resource": "optiq",
                }
            )
            runtime = _Port({"state": "complete"})
            router = DaemonOperationRouter(
                runtime=runtime,
                model=_Port(),
                supervisor=_Port({"state": "running"}),
                state=state,
            )

            router.start()

            self.assertEqual(runtime.calls, [])
            self.assertEqual(
                [event["kind"] for event in state.events("runtime-before-restart")],
                ["started", "complete"],
            )
            self.assertFalse(
                state.operation("runtime-before-restart")["terminal_event_pending"]
            )

    def test_operation_id_is_bound_to_the_exact_operation_and_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _Port({"state": "complete"})
            model = _Port({"state": "complete"})
            router = DaemonOperationRouter(
                runtime=runtime,
                model=model,
                supervisor=_Port(),
                state=OperationalStateStore(Path(directory) / "state.sqlite3"),
            )
            router.execute(
                "runtime.install",
                {"runtime": "optiq", "options": {"channel": "tested"}},
                operation_id="physical-1",
            )

            conflicting_requests = (
                (
                    "runtime.install",
                    {"runtime": "optiq", "options": {"channel": "custom"}},
                ),
                ("model.install", {"repository": "owner/model"}),
            )
            for operation, parameters in conflicting_requests:
                with self.subTest(operation=operation, parameters=parameters):
                    with self.assertRaises(ApplicationError) as raised:
                        router.execute(
                            operation,
                            parameters,
                            operation_id="physical-1",
                        )
                    self.assertEqual(raised.exception.code, "operation_id_conflict")

            self.assertEqual(len(runtime.calls), 1)
            self.assertEqual(model.calls, [])

    def test_concurrent_matching_operation_ids_share_one_physical_result(self) -> None:
        class BlockingPort(_Port):
            def __init__(self) -> None:
                super().__init__({"state": "complete"})
                self.entered = threading.Event()
                self.duplicate_entered = threading.Event()
                self.release = threading.Event()
                self.entries = 0

            def execute(self, operation, parameters):
                self.entries += 1
                if self.entries > 1:
                    self.duplicate_entered.set()
                self.entered.set()
                self.release.wait(1)
                return super().execute(operation, parameters)

        with tempfile.TemporaryDirectory() as directory:
            runtime = BlockingPort()
            router = DaemonOperationRouter(
                runtime=runtime,
                model=_Port(),
                supervisor=_Port(),
                state=OperationalStateStore(Path(directory) / "state.sqlite3"),
            )
            results = []

            def execute() -> None:
                results.append(
                    router.execute(
                        "runtime.install",
                        {"runtime": "optiq"},
                        operation_id="runtime-concurrent",
                    )
                )

            first = threading.Thread(target=execute, daemon=True)
            second = threading.Thread(target=execute, daemon=True)
            first.start()
            self.assertTrue(runtime.entered.wait(1))
            second.start()

            self.assertFalse(runtime.duplicate_entered.wait(0.05))
            runtime.release.set()
            first.join(1)
            second.join(1)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(len(runtime.calls), 1)
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])

    def test_router_rejects_unowned_resume_instead_of_faking_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = DaemonOperationRouter(
                runtime=_Port(),
                model=_Port(),
                supervisor=_Port(),
                state=OperationalStateStore(Path(directory) / "state.sqlite3"),
            )

            with self.assertRaisesRegex(ApplicationError, "not owned"):
                router.execute("operation.resume", {"resource": "unknown"})

    def test_supervisor_stop_drains_physical_work_and_rejects_new_work(self) -> None:
        class BlockingPort(_Port):
            def __init__(self) -> None:
                super().__init__({"state": "complete"})
                self.entered = threading.Event()
                self.release = threading.Event()

            def execute(self, operation, parameters):
                self.entered.set()
                self.release.wait(1)
                return super().execute(operation, parameters)

        with tempfile.TemporaryDirectory() as directory:
            runtime = BlockingPort()
            supervisor = _Port({"state": "stopped"})
            router = DaemonOperationRouter(
                runtime=runtime,
                model=_Port(),
                supervisor=supervisor,
                state=OperationalStateStore(Path(directory) / "state.sqlite3"),
                physical_drain_timeout=1,
            )
            physical = threading.Thread(
                target=lambda: router.execute("runtime.install", {"runtime": "optiq"}),
                daemon=True,
            )
            physical.start()
            self.assertTrue(runtime.entered.wait(1))
            stopped = []
            stopping = threading.Thread(
                target=lambda: stopped.append(router.execute("supervisor.stop", {})),
                daemon=True,
            )
            stopping.start()
            with router._condition:  # noqa: SLF001 - lifecycle synchronization proof.
                self.assertTrue(
                    router._condition.wait_for(  # noqa: SLF001
                        lambda: router._stopping,
                        timeout=1,  # noqa: SLF001
                    )
                )

            with self.assertRaises(ApplicationError) as raised:
                router.execute("model.install", {"repository": "owner/model"})
            self.assertEqual(raised.exception.code, "supervisor_stopping")
            self.assertEqual(supervisor.calls, [])

            runtime.release.set()
            physical.join(1)
            stopping.join(1)
            self.assertFalse(physical.is_alive())
            self.assertFalse(stopping.is_alive())
            self.assertEqual(stopped[0]["state"], "stopped")
            self.assertEqual(supervisor.calls, [("supervisor.stop", {})])

    def test_waiting_stop_reclaims_the_drain_gate_after_the_first_stop_fails(
        self,
    ) -> None:
        class StopPort(_Port):
            def __init__(self) -> None:
                super().__init__({"state": "stopped"})
                self.first_entered = threading.Event()
                self.first_release = threading.Event()
                self.second_entered = threading.Event()
                self.second_release = threading.Event()
                self.stop_calls = 0

            def execute(self, operation, parameters):
                if operation == "supervisor.stop":
                    self.stop_calls += 1
                    if self.stop_calls == 1:
                        self.first_entered.set()
                        self.first_release.wait(1)
                        raise RuntimeError("first stop failed")
                    self.second_entered.set()
                    self.second_release.wait(1)
                return super().execute(operation, parameters)

        with tempfile.TemporaryDirectory() as directory:
            supervisor = StopPort()
            model = _Port({"state": "complete"})
            router = DaemonOperationRouter(
                runtime=_Port(),
                model=model,
                supervisor=supervisor,
                state=OperationalStateStore(Path(directory) / "state.sqlite3"),
            )
            failures = []
            stopped = []

            def fail_first() -> None:
                try:
                    router.execute("supervisor.stop", {})
                except RuntimeError as error:
                    failures.append(str(error))

            first = threading.Thread(target=fail_first, daemon=True)
            second = threading.Thread(
                target=lambda: stopped.append(router.execute("supervisor.stop", {})),
                daemon=True,
            )
            first.start()
            self.assertTrue(supervisor.first_entered.wait(1))
            second.start()
            supervisor.first_release.set()
            self.assertTrue(supervisor.second_entered.wait(1))

            with self.assertRaises(ApplicationError) as raised:
                router.execute("model.install", {"repository": "owner/model"})
            self.assertEqual(raised.exception.code, "supervisor_stopping")
            self.assertEqual(model.calls, [])

            supervisor.second_release.set()
            first.join(1)
            second.join(1)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(failures, ["first stop failed"])
            self.assertEqual(stopped[0]["state"], "stopped")

    def test_stop_serializes_maintenance_and_rejects_lifecycle_start(self) -> None:
        class MaintenancePort(_Port):
            def __init__(self) -> None:
                super().__init__({"state": "running"})
                self.maintenance_entered = threading.Event()
                self.maintenance_release = threading.Event()
                self.stop_entered = threading.Event()
                self.stop_release = threading.Event()

            def execute(self, operation, parameters):
                if operation == "supervisor.maintain":
                    self.maintenance_entered.set()
                    self.maintenance_release.wait(1)
                elif operation == "supervisor.stop":
                    self.stop_entered.set()
                    self.stop_release.wait(1)
                    return {"state": "stopped"}
                return super().execute(operation, parameters)

        with tempfile.TemporaryDirectory() as directory:
            supervisor = MaintenancePort()
            router = DaemonOperationRouter(
                runtime=_Port(),
                model=_Port(),
                supervisor=supervisor,
                state=OperationalStateStore(Path(directory) / "state.sqlite3"),
            )
            maintenance = threading.Thread(target=router.maintain, daemon=True)
            stopping = threading.Thread(target=router.stop, daemon=True)
            maintenance.start()
            self.assertTrue(supervisor.maintenance_entered.wait(1))
            stopping.start()

            self.assertFalse(supervisor.stop_entered.wait(0.05))
            with self.assertRaises(ApplicationError) as raised:
                router.start()
            self.assertEqual(raised.exception.code, "supervisor_stopping")

            supervisor.maintenance_release.set()
            self.assertTrue(supervisor.stop_entered.wait(1))
            supervisor.stop_release.set()
            maintenance.join(1)
            stopping.join(1)
            self.assertFalse(maintenance.is_alive())
            self.assertFalse(stopping.is_alive())

    def test_failed_daemon_cleanup_stop_reopens_the_router_in_finally(self) -> None:
        class FailingStop(_Port):
            def execute(self, operation, parameters):
                if operation == "supervisor.stop":
                    raise RuntimeError("stop failed")
                return super().execute(operation, parameters)

        with tempfile.TemporaryDirectory() as directory:
            model = _Port({"state": "complete"})
            router = DaemonOperationRouter(
                runtime=_Port(),
                model=model,
                supervisor=FailingStop(),
                state=OperationalStateStore(Path(directory) / "state.sqlite3"),
            )

            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                router.stop()
            result = router.execute("model.install", {"repository": "owner/model"})

        self.assertEqual(result["state"], "complete")

    def test_state_removal_rejects_symlink_even_when_it_resolves_to_owned_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owned = root / "owned"
            owned.mkdir()
            link = root / "link"
            link.symlink_to(owned, target_is_directory=True)
            remover = OwnedStateRemover((owned,))

            with self.assertRaisesRegex(ApplicationError, "outside mastic ownership"):
                remover.execute(
                    "state.remove", {"paths": [str(link)], "confirmed": True}
                )
            self.assertTrue(owned.exists())

    def test_recommended_setup_blocks_undersized_mac(self) -> None:
        with self.assertRaisesRegex(ValueError, "No validated setup profile fits"):
            _setup_resolver().resolve(
                SetupPreflight(
                    "darwin",
                    "arm64",
                    memory_bytes=16 * 1024**3,
                    disk_free_bytes=100 * 1024**3,
                    online=True,
                )
            )

    def test_recommended_setup_defaults_to_balanced_capacity_and_application_targets(
        self,
    ) -> None:
        preview = _setup_resolver().preview(
            _setup_resolver().resolve(
                SetupPreflight(
                    "darwin",
                    "arm64",
                    memory_bytes=48 * 1024**3,
                    disk_free_bytes=100 * 1024**3,
                    online=True,
                )
            )
        )

        self.assertEqual(preview.capacity_profile, "balanced")
        self.assertEqual(preview.context_window, 131_072)
        self.assertEqual(preview.service_options["max_context"], 131_072)

        self.assertEqual(preview.service_options["max_concurrent"], 6)
        self.assertEqual(preview.service_options["prompt_cache_bytes"], 2 * 1024**3)
        self.assertNotIn("temperature", preview.service_options)
        self.assertEqual(preview.projected_kv_bytes, 5_737_807_872)
        self.assertEqual(preview.application_targets, ("codex", "hindsight"))
        self.assertEqual(
            preview.application_target_options["codex"]["sampling_profiles"]["coding"][
                "temperature"
            ],
            0.6,
        )
        self.assertEqual(
            preview.application_target_options["codex"]["sampling_profiles"]["coding"][
                "top_k"
            ],
            20,
        )
        self.assertTrue(
            preview.application_target_options["codex"]["sampling_profiles"]["coding"][
                "enable_thinking"
            ]
        )
        self.assertEqual(
            preview.application_target_options["codex"]["sampling_profiles"]["coding"][
                "upstream_profile"
            ],
            "precise-coding-thinking",
        )
        self.assertEqual(
            preview.application_target_options["codex"]["sampling_profiles"]["coding"][
                "source_revision"
            ],
            "995ad96eacd98c81ed38be0c5b274b04031597b0",
        )
        self.assertEqual(
            preview.application_target_options["hindsight"]["sampling_profiles"][
                "retain"
            ]["temperature"],
            0.7,
        )
        self.assertFalse(
            preview.application_target_options["hindsight"]["sampling_profiles"][
                "retain"
            ]["enable_thinking"]
        )
        self.assertEqual(
            preview.application_target_options["hindsight"]["sampling_profiles"][
                "reflect"
            ]["temperature"],
            1.0,
        )
        self.assertEqual(
            preview.application_target_options["hindsight"]["max_concurrent"], 1
        )

    def test_phase_one_intents_select_the_provisional_validated_capacities(
        self,
    ) -> None:
        facts = SetupPreflight(
            "darwin",
            "arm64",
            memory_bytes=96 * 1024**3,
            disk_free_bytes=300 * 1024**3,
            online=True,
        )
        resolver = _setup_resolver()

        selected = {
            intent: resolver.resolve(
                facts, SetupRequest(intent=intent)
            ).capacity_profile.name
            for intent in SetupIntent
        }

        self.assertEqual(
            selected,
            {
                SetupIntent.BALANCED: "balanced",
                SetupIntent.DEEP: "long-context",
                SetupIntent.RESPONSIVE: "balanced",
            },
        )

    @patch("mastic.infrastructure.production_host.httpx.post")
    def test_gateway_verification_uses_the_public_chat_completions_contract(
        self, post
    ) -> None:
        post.return_value.is_success = True
        post.return_value.json.return_value = {
            "choices": [{"message": {"content": "mastic ready"}}]
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credential = GatewayCredential(root / "gateway.token")
            token = credential.load_or_create()
            result = GatewayVerificationPort(credential).execute(
                "verify.request",
                {
                    "endpoint": "http://127.0.0.1:8766/v1",
                    "model": "coding",
                    "request": "Respond with exactly: mastic ready",
                },
            )

        self.assertEqual(result["text"], "mastic ready")
        self.assertEqual(
            post.call_args.args[0], "http://127.0.0.1:8766/v1/chat/completions"
        )
        self.assertEqual(
            post.call_args.kwargs["headers"]["authorization"], f"Bearer {token}"
        )

    def test_launch_supply_keeps_config_key_but_uses_exact_revision_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            revision = "a" * 40
            snapshot = Path(directory) / "snapshot"
            snapshot.mkdir()
            config = validate_config(
                {
                    "schema_version": 1,
                    "models": {
                        "friendly-installation": {
                            "repository": "owner/model",
                            "revision": revision,
                        }
                    },
                }
            )
            inventory = CacheInventory(
                (
                    CachedRevision(
                        f"owner/model@{revision}",
                        "owner/model",
                        revision,
                        snapshot,
                        0,
                        "local-observed",
                        True,
                    ),
                ),
                "local-observed",
                (),
            )

            installations = configured_model_installations(config, inventory)

            self.assertEqual(set(installations), {"friendly-installation"})
            self.assertEqual(
                installations["friendly-installation"].installation_id,
                f"owner/model@{revision}",
            )

    def test_launch_supply_uses_adopted_external_snapshot_without_cache_entry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            revision = "a" * 40
            snapshot = Path(directory) / "external"
            snapshot.mkdir()
            config = validate_config(
                {
                    "schema_version": 1,
                    "models": {
                        "adopted": {
                            "repository": "owner/model",
                            "revision": revision,
                            "provenance": "adopted",
                            "path": str(snapshot),
                        }
                    },
                }
            )
            inventory = CacheInventory((), "local-observed", ())

            installations = configured_model_installations(config, inventory)

            self.assertEqual(installations["adopted"].snapshot_path, snapshot)
            self.assertEqual(
                installations["adopted"].provenance.source, "external-adopted"
            )
            self.assertEqual(
                installations["adopted"].installation_id,
                f"owner/model@{revision}",
            )

    def test_removal_inventory_includes_manifest_backed_orphan_targets(self) -> None:
        class Supply:
            def inventory(self):
                return CacheInventory((), "local-observed", ())

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = MasticPaths(
                home / "config", home / "state", home / "data", home / "logs"
            )
            paths.prepare()
            ownership = paths.state_dir / "application-targets"
            ownership.mkdir(mode=0o700)
            (ownership / "codex.ownership.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "integration": "codex",
                        "config_path": str(home / ".codex/config.toml"),
                        "backup_path": str(ownership / "codex.config.backup"),
                        "fields": [],
                    }
                ),
                encoding="utf-8",
            )
            profile = "agent-memory"
            (ownership / f"hindsight-{profile}.ownership.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "integration": "hindsight",
                        "config_path": str(home / f".hindsight/profiles/{profile}.env"),
                        "backup_path": str(
                            ownership / f"hindsight-{profile}.config.backup"
                        ),
                        "fields": [],
                    }
                ),
                encoding="utf-8",
            )

            inventory = removal_inventory(
                paths,
                _Launchd(False),
                ConfigStore(paths.config_file, validate_config),
                Supply(),
                home,
            )

            self.assertEqual(
                inventory.application_target_integrations, ("codex", "hindsight")
            )
            self.assertIn(str(paths.state_dir), inventory.product_owned_paths)

    def test_removal_inventory_fails_closed_on_invalid_recovery_manifest(self) -> None:
        class Supply:
            def inventory(self):
                return CacheInventory((), "local-observed", ())

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = MasticPaths(
                home / "config", home / "state", home / "data", home / "logs"
            )
            paths.prepare()
            ownership = paths.state_dir / "application-targets"
            ownership.mkdir(mode=0o700)
            (ownership / "codex.ownership.json").write_text(
                "not-json", encoding="utf-8"
            )

            with self.assertRaises(ApplicationError) as raised:
                removal_inventory(
                    paths,
                    _Launchd(False),
                    ConfigStore(paths.config_file, validate_config),
                    Supply(),
                    home,
                )

            self.assertEqual(
                raised.exception.code, "application_target_recovery_required"
            )

    def test_removal_inventory_fails_closed_on_ambiguous_hindsight_recovery(
        self,
    ) -> None:
        class Supply:
            def inventory(self):
                return CacheInventory((), "local-observed", ())

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = MasticPaths(
                home / "config", home / "state", home / "data", home / "logs"
            )
            paths.prepare()
            ownership = paths.state_dir / "application-targets"
            ownership.mkdir(mode=0o700)
            for profile in ("one", "two"):
                (ownership / f"hindsight-{profile}.ownership.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "integration": "hindsight",
                            "config_path": str(
                                home / f".hindsight/profiles/{profile}.env"
                            ),
                            "backup_path": str(
                                ownership / f"hindsight-{profile}.config.backup"
                            ),
                            "fields": [],
                        }
                    ),
                    encoding="utf-8",
                )

            with self.assertRaises(ApplicationError) as raised:
                removal_inventory(
                    paths,
                    _Launchd(False),
                    ConfigStore(paths.config_file, validate_config),
                    Supply(),
                    home,
                )

            self.assertEqual(
                raised.exception.code, "application_target_recovery_required"
            )

    def test_removal_inventory_fails_closed_on_desired_owned_profile_mismatch(
        self,
    ) -> None:
        class Supply:
            def inventory(self):
                return CacheInventory((), "local-observed", ())

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = MasticPaths(
                home / "config", home / "state", home / "data", home / "logs"
            )
            paths.prepare()
            ownership = paths.state_dir / "application-targets"
            ownership.mkdir(mode=0o700)
            owned_profile = "owned-memory"
            (ownership / f"hindsight-{owned_profile}.ownership.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "integration": "hindsight",
                        "config_path": str(
                            home / f".hindsight/profiles/{owned_profile}.env"
                        ),
                        "backup_path": str(
                            ownership / f"hindsight-{owned_profile}.config.backup"
                        ),
                        "fields": [],
                    }
                ),
                encoding="utf-8",
            )
            desired = SimpleNamespace(profile="desired-memory")
            store = SimpleNamespace(
                exists=True,
                load=lambda: SimpleNamespace(
                    value=SimpleNamespace(application_targets={"hindsight": desired})
                ),
            )

            with self.assertRaises(ApplicationError) as raised:
                removal_inventory(paths, _Launchd(False), store, Supply(), home)

            self.assertEqual(
                raised.exception.code, "application_target_recovery_required"
            )


class _FakeRouter(_Port):
    def __init__(self, request_stop) -> None:
        super().__init__({"state": "stopped"})
        self.request_stop = request_stop
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1
        return {"state": "running"}

    def stop(self):
        self.stop_calls += 1
        return {"state": "stopped"}

    def cancel(self, operation_id):
        return False

    def maintain(self):
        self.calls.append(("maintain", {}))
        return {"state": "running"}

    def record_maintenance_failure(self, error):
        self.calls.append(("maintenance_failure", type(error).__name__))

    def execute(self, operation, parameters, *, operation_id=None):
        value = super().execute(operation, parameters)
        if operation == "supervisor.stop":
            self.request_stop()
        return value


class _FakeServer:
    def __init__(self, _path, handler, *, cancel_handler) -> None:
        self.handler = handler
        self.cancel_handler = cancel_handler
        self.progress = []
        self.closed = False
        self.request_task = None

    async def start(self):
        from mastic.infrastructure.control_protocol import ControlRequest

        async def emit(value):
            self.progress.append(dict(value))

        self.request_task = asyncio.create_task(
            self.handler(
                ControlRequest("request", "operation", "supervisor.stop", {}), emit
            )
        )

    async def close(self):
        if self.request_task is not None:
            await self.request_task
        self.closed = True


class DaemonServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_supervisor_stop_closes_control_service(self) -> None:
        routers = []
        servers = []

        def router_factory(request_stop):
            router = _FakeRouter(request_stop)
            routers.append(router)
            return router

        def server_factory(*args, **kwargs):
            server = _FakeServer(*args, **kwargs)
            servers.append(server)
            return server

        service = DaemonService(
            Path("/tmp/masticd-test.sock"),
            router_factory,
            server_factory=server_factory,
        )

        await asyncio.wait_for(service.serve(), timeout=1)

        self.assertEqual(routers[0].start_calls, 1)
        self.assertEqual(routers[0].stop_calls, 1)
        self.assertEqual(
            [item["phase"] for item in servers[0].progress], ["started", "complete"]
        )
        self.assertTrue(servers[0].closed)

    async def test_daemon_runs_periodic_maintenance_without_cli_requests(self) -> None:
        routers = []

        class IdleServer:
            async def start(self):
                return None

            async def close(self):
                return None

        def router_factory(request_stop):
            router = _FakeRouter(request_stop)
            routers.append(router)
            return router

        service = DaemonService(
            Path("/tmp/masticd-maintenance-test.sock"),
            router_factory,
            server_factory=lambda *args, **kwargs: IdleServer(),
            maintenance_interval=0.01,
        )
        task = asyncio.create_task(service.serve())
        await asyncio.sleep(0.04)
        routers[0].request_stop()
        await asyncio.wait_for(task, timeout=1)

        self.assertGreaterEqual(
            sum(call[0] == "maintain" for call in routers[0].calls), 2
        )


if __name__ == "__main__":
    unittest.main()
