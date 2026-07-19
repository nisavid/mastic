from __future__ import annotations

import asyncio
import threading
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path

from mastic.application.dispatch import ApplicationError
from mastic.infrastructure.control_client import AsyncUnixControlClient
from mastic.infrastructure.control_protocol import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    UnixControlServer,
    read_message,
    write_message,
)
from mastic.infrastructure.daemon_service import DaemonOperationRouter
from mastic.infrastructure.state_store import OperationalStateStore


class _PhysicalTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []
        self.runtime_entered = threading.Event()
        self.runtime_release = threading.Event()
        self.model_entered = threading.Event()
        self.model_release = threading.Event()
        self.stop_entered = threading.Event()

    def execute(self, operation: str) -> Mapping[str, object]:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(operation)
        try:
            if operation == "runtime.install":
                self.runtime_entered.set()
                self.runtime_release.wait(2)
            elif operation == "model.install":
                self.model_entered.set()
                self.model_release.wait(2)
            elif operation == "supervisor.stop":
                self.stop_entered.set()
            return {
                "state": "stopped" if operation == "supervisor.stop" else "complete"
            }
        finally:
            with self._lock:
                self.active -= 1


class _TrackedOwner:
    def __init__(self, tracker: _PhysicalTracker) -> None:
        self._tracker = tracker

    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        del parameters
        return self._tracker.execute(operation)


class DaemonPhysicalSerializationTests(unittest.TestCase):
    def test_stop_drains_admitted_cross_family_mutations_without_overlap(self) -> None:
        tracker = _PhysicalTracker()
        with tempfile.TemporaryDirectory() as directory:
            owner = _TrackedOwner(tracker)
            router = DaemonOperationRouter(
                runtime=owner,
                model=owner,
                supervisor=owner,
                state=OperationalStateStore(Path(directory) / "state.sqlite3"),
                physical_drain_timeout=2,
            )
            failures: list[BaseException] = []

            def execute(
                operation: str,
                parameters: Mapping[str, object],
                operation_id: str,
            ) -> None:
                try:
                    router.execute(operation, parameters, operation_id=operation_id)
                except BaseException as error:
                    failures.append(error)

            runtime = threading.Thread(
                target=execute,
                args=("runtime.install", {"runtime": "optiq"}, "runtime-owner"),
                daemon=True,
            )
            model = threading.Thread(
                target=execute,
                args=(
                    "model.install",
                    {"repository": "owner/model"},
                    "model-retry",
                ),
                daemon=True,
            )
            stopping = threading.Thread(
                target=execute,
                args=("supervisor.stop", {}, "stop-after-admission"),
                daemon=True,
            )

            runtime.start()
            self.assertTrue(tracker.runtime_entered.wait(1))
            model.start()
            with router._condition:  # noqa: SLF001 - synchronize admitted work.
                self.assertTrue(
                    router._condition.wait_for(  # noqa: SLF001
                        lambda: router._active_physical == 2,  # noqa: SLF001
                        timeout=1,
                    )
                )
            self.assertFalse(tracker.model_entered.wait(0.05))

            stopping.start()
            with router._condition:  # noqa: SLF001 - synchronize stop admission.
                self.assertTrue(
                    router._condition.wait_for(  # noqa: SLF001
                        lambda: router._stopping,  # noqa: SLF001
                        timeout=1,
                    )
                )
            with self.assertRaises(ApplicationError) as raised:
                router.execute("gateway.restart", {}, operation_id="late-gateway-retry")
            self.assertEqual(raised.exception.code, "supervisor_stopping")

            tracker.runtime_release.set()
            self.assertTrue(tracker.model_entered.wait(1))
            self.assertFalse(tracker.stop_entered.is_set())
            tracker.model_release.set()
            self.assertTrue(tracker.stop_entered.wait(1))

            runtime.join(1)
            model.join(1)
            stopping.join(1)
            self.assertFalse(runtime.is_alive())
            self.assertFalse(model.is_alive())
            self.assertFalse(stopping.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(
                tracker.calls,
                ["runtime.install", "model.install", "supervisor.stop"],
            )
            self.assertEqual(tracker.max_active, 1)


class DaemonPhysicalSerializationProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_owner_keeps_gate_until_physical_completion(
        self,
    ) -> None:
        tracker = _PhysicalTracker()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner = _TrackedOwner(tracker)
            router = DaemonOperationRouter(
                runtime=owner,
                model=owner,
                supervisor=owner,
                state=OperationalStateStore(root / "state.sqlite3"),
            )

            async def handle(request, emit):
                del emit
                return await asyncio.to_thread(
                    router.execute,
                    request.operation,
                    request.parameters,
                    operation_id=request.operation_id,
                )

            server = UnixControlServer(root / "masticd.sock", handle)
            await server.start()
            try:
                reader, writer = await asyncio.open_unix_connection(server.socket_path)
                await write_message(
                    writer,
                    {
                        "type": "negotiate",
                        "protocol": PROTOCOL_NAME,
                        "supported_versions": [PROTOCOL_VERSION],
                        "request_id": "negotiate-disconnected-owner",
                    },
                )
                negotiated = await read_message(reader)
                self.assertEqual(negotiated["type"], "negotiated")
                await write_message(
                    writer,
                    {
                        "type": "request",
                        "protocol": PROTOCOL_NAME,
                        "version": PROTOCOL_VERSION,
                        "request_id": "request-disconnected-owner",
                        "operation_id": "runtime-disconnected-owner",
                        "operation": "runtime.install",
                        "parameters": {"runtime": "optiq"},
                    },
                )
                self.assertTrue(
                    await asyncio.to_thread(tracker.runtime_entered.wait, 1)
                )
                writer.close()
                await writer.wait_closed()

                retry = asyncio.create_task(
                    AsyncUnixControlClient(server.socket_path).execute(
                        "model.install",
                        {"repository": "owner/model"},
                        request_id="request-new-client",
                        operation_id="model-new-operation-id",
                    )
                )
                await asyncio.sleep(0.05)
                self.assertFalse(tracker.model_entered.is_set())

                tracker.runtime_release.set()
                self.assertTrue(await asyncio.to_thread(tracker.model_entered.wait, 1))
                self.assertFalse(retry.done())
                tracker.model_release.set()
                response = await asyncio.wait_for(retry, timeout=1)

                self.assertEqual(response.result["state"], "complete")
                self.assertEqual(tracker.calls, ["runtime.install", "model.install"])
                self.assertEqual(tracker.max_active, 1)
            finally:
                tracker.runtime_release.set()
                tracker.model_release.set()
                await server.close()


if __name__ == "__main__":
    unittest.main()
