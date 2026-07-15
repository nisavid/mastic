from __future__ import annotations

import asyncio
import os
import plistlib
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mlxctl.application.config_schema import validate_config
from mlxctl.application.dispatch import ApplicationError, OperationRequest
from mlxctl.application.setup import SetupPreflight
from mlxctl.infrastructure.model_supply import CacheInventory, CachedRevision
from mlxctl.infrastructure.paths_v1 import MlxctlPaths
from mlxctl.infrastructure.daemon_service import DaemonOperationRouter, DaemonService
from mlxctl.infrastructure.production import (
    _ActivatingOperationOwner,
    _GatewayMutationGuard,
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

    def test_daemon_graph_composes_without_binding_or_starting_services(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MlxctlPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )

            daemon = compose_daemon(paths=paths, home=root)

            self.assertIsInstance(daemon, DaemonService)
            self.assertFalse(paths.control_socket.exists())

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

        result = GatewayVerificationPort().execute(
            "verify.request",
            {
                "endpoint": "http://127.0.0.1:8766/v1",
                "model": "coding",
                "request": "Respond with exactly: mlxctl ready",
            },
        )
        client_request("http://127.0.0.1:8766/v1", "coding", {"messages": []})

        self.assertEqual(result["text"], "mlxctl ready")
        self.assertEqual(
            [call.args[0] for call in post.call_args_list],
            [
                "http://127.0.0.1:8766/v1/chat/completions",
                "http://127.0.0.1:8766/v1/chat/completions",
            ],
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


if __name__ == "__main__":
    unittest.main()
