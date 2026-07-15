from __future__ import annotations

import asyncio
import os
import plistlib
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mlxctl.application.config_schema import validate_config
from mlxctl.application.dispatch import ApplicationError, OperationRequest
from mlxctl.application.setup import SetupPreflight
from mlxctl.infrastructure.config_store import ConfigStore
from mlxctl.infrastructure.model_supply import CacheInventory, CachedRevision
from mlxctl.infrastructure.gateway_credential import GatewayCredential
from mlxctl.infrastructure.paths_v1 import MlxctlPaths
from mlxctl.infrastructure.daemon_service import DaemonOperationRouter, DaemonService
from mlxctl.infrastructure.production import (
    _ActivatingOperationOwner,
    _GatewayMutationGuard,
    _LocalSupervisorOwner,
    _LocalModelSupply,
    _SetupSupervisorOwner,
    _setup_planner,
    compose_daemon,
    compose_local,
    make_launchd,
)
from mlxctl.infrastructure.production_host import (
    AbsoluteUvRunner,
    GatewayVerificationPort,
    OwnedStateRemover,
    client_request,
    configured_model_installations,
    default_sampling,
    resolve_uv,
)
from mlxctl.infrastructure.state_store import OperationalStateStore


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
    def __init__(self, running: bool) -> None:
        self.running = running


class _Launchd:
    def __init__(self, running: bool) -> None:
        self.running = running

    def status(self):
        return _LaunchdStatus(self.running)


class ProductionCompositionTests(unittest.TestCase):
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
            paths = MlxctlPaths(
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

        owner = _SetupSupervisorOwner(remote, _Launchd(False), activator)
        result = owner.execute("supervisor.start", {})

        self.assertEqual(activator.calls, 1)
        self.assertEqual(remote.calls, [("supervisor.start", {})])
        self.assertEqual(result["state"], "running")

    def test_local_supervisor_stop_is_idempotent_without_remote_activation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _Port({"state": "stopping"})
            owner = _LocalSupervisorOwner(
                remote,
                _Launchd(False),
                root / "mlxd.sock",
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
                _Launchd(True),
                root / "mlxd.sock",
                OperationalStateStore(root / "state.db"),
                ConfigStore(root / "config.toml", validate_config),
            )

            result = owner.execute("supervisor.stop", {})

        self.assertEqual(result, {"state": "stopping"})
        self.assertEqual(remote.calls, [("supervisor.stop", {})])

    def test_local_supervisor_stop_forwards_to_foreground_socket_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "mlxd.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(str(path))
            remote = _Port({"state": "stopping"})
            owner = _LocalSupervisorOwner(
                remote,
                _Launchd(False),
                path,
                OperationalStateStore(root / "state.db"),
                ConfigStore(root / "config.toml", validate_config),
            )

            result = owner.execute("supervisor.stop", {})

        self.assertEqual(result, {"state": "stopping"})
        self.assertEqual(remote.calls, [("supervisor.stop", {})])

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
            versions = iter(range(10, 20))
            owner = _LocalSupervisorOwner(
                _Port(),
                _Launchd(False),
                root / "mlxd.sock",
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

    def test_running_gateway_endpoint_edit_fails_before_preview_or_execution(
        self,
    ) -> None:
        dispatcher = _Port()
        guard = _GatewayMutationGuard(dispatcher, _Launchd(True))

        with self.assertRaisesRegex(ApplicationError, "Stop the Supervisor"):
            guard.preview(OperationRequest("gateway.configure", {"port": 9000}))

        self.assertEqual(dispatcher.calls, [])

    def test_live_control_socket_blocks_gateway_endpoint_edit(self) -> None:
        dispatcher = _Port()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mlxd.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(str(path))
            guard = _GatewayMutationGuard(dispatcher, _Launchd(False), path)

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
        guard = _GatewayMutationGuard(dispatcher, _Launchd(True))
        request = OperationRequest("service.edit", {"resource": "coding"})

        self.assertEqual(guard.execute(request), {"edited": True})
        self.assertEqual(dispatcher.calls, [request])

    def test_client_sampling_defaults_cover_coding_and_memory_operations(self) -> None:
        self.assertEqual(default_sampling("codex")["coding"].temperature, 0.0)
        hindsight = default_sampling("hindsight")
        self.assertEqual(hindsight["verification"].temperature, 0.0)
        self.assertEqual(hindsight["retain"].temperature, 0.1)
        self.assertEqual(hindsight["reflect"].temperature, 0.9)
        self.assertEqual(hindsight["consolidation"].temperature, 0.0)

    def test_local_status_neither_inspects_nor_activates_launchd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MlxctlPaths(
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
                (root / "Library/LaunchAgents/io.nisavid.mlxd.plist").exists()
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
            paths = MlxctlPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )

            daemon = compose_daemon(paths=paths, home=root)

            self.assertIsInstance(daemon, DaemonService)
            self.assertFalse(paths.control_socket.exists())
            self.assertTrue(paths.gateway_credential.exists())
            self.assertEqual(
                stat.S_IMODE(paths.gateway_credential.stat().st_mode), 0o600
            )

    def test_production_graphs_reject_adoption_inside_owned_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MlxctlPaths(
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

            with self.assertRaisesRegex(Exception, "mlxctl-owned"):
                local.application.dispatcher.preview(
                    OperationRequest("model.adopt", parameters)
                )

            daemon = compose_daemon(paths=paths, home=root)
            router = daemon._router_factory(lambda: None)
            with self.assertRaisesRegex(ApplicationError, "mlxctl-owned"):
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
            ["/usr/bin/python3", "-m", "mlxctl.entrypoints", "daemon"],
        )
        self.assertEqual(
            payload["StandardOutPath"],
            "/Users/example/Library/Logs/mlxctl/supervisor.log",
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
            paths = MlxctlPaths(
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

    @patch("mlxctl.infrastructure.production_host.subprocess.run")
    def test_runtime_installer_uses_configured_absolute_uv(self, run) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "uv"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o700)
            with patch.dict(
                os.environ,
                {"MLXCTL_UV_EXECUTABLE": str(executable)},
                clear=False,
            ):
                resolved = resolve_uv(Path(directory))
            AbsoluteUvRunner(resolved).run(("uv", "--version"))

        self.assertEqual(
            run.call_args.args[0], (str(executable.resolve()), "--version")
        )
        self.assertFalse(run.call_args.kwargs["shell"])

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
                target=lambda: router.execute("runtime.install", {"runtime": "optiq"})
            )
            physical.start()
            self.assertTrue(runtime.entered.wait(1))
            stopped = []
            stopping = threading.Thread(
                target=lambda: stopped.append(router.execute("supervisor.stop", {}))
            )
            stopping.start()
            time.sleep(0.02)

            with self.assertRaises(ApplicationError) as raised:
                router.execute("model.install", {"repository": "owner/model"})
            self.assertEqual(raised.exception.code, "supervisor_stopping")
            self.assertEqual(supervisor.calls, [])

            runtime.release.set()
            physical.join(1)
            stopping.join(1)
            self.assertEqual(stopped[0]["state"], "stopped")
            self.assertEqual(supervisor.calls, [("supervisor.stop", {})])

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

            with self.assertRaisesRegex(ApplicationError, "outside mlxctl ownership"):
                remover.execute(
                    "state.remove", {"paths": [str(link)], "confirmed": True}
                )
            self.assertTrue(owned.exists())

    def test_recommended_setup_blocks_undersized_mac(self) -> None:
        with self.assertRaisesRegex(ValueError, "no recommended setup profile fits"):
            _setup_planner().plan(
                SetupPreflight(
                    "darwin",
                    "arm64",
                    memory_bytes=16 * 1024**3,
                    disk_free_bytes=100 * 1024**3,
                    online=True,
                )
            )

    @patch("mlxctl.infrastructure.production_host.httpx.post")
    def test_gateway_requests_append_to_openai_v1_base_once(self, post) -> None:
        post.return_value.is_success = True
        post.return_value.json.return_value = {
            "choices": [{"message": {"content": "mlxctl ready"}}]
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
                    "request": "Respond with exactly: mlxctl ready",
                },
            )
            client_request(
                "http://127.0.0.1:8766/v1",
                "coding",
                {"messages": []},
                credential=credential,
            )

        self.assertEqual(result["text"], "mlxctl ready")
        self.assertEqual(
            [call.args[0] for call in post.call_args_list],
            [
                "http://127.0.0.1:8766/v1/chat/completions",
                "http://127.0.0.1:8766/v1/chat/completions",
            ],
        )
        self.assertEqual(
            [call.kwargs["headers"]["authorization"] for call in post.call_args_list],
            [f"Bearer {token}", f"Bearer {token}"],
        )
        self.assertEqual(
            post.call_args_list[1].kwargs["json"]["messages"][0]["role"], "user"
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
        from mlxctl.infrastructure.control_protocol import ControlRequest

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
            Path("/tmp/mlxd-test.sock"),
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
            Path("/tmp/mlxd-maintenance-test.sock"),
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
