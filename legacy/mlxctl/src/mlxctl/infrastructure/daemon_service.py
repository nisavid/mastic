"""Foreground mlxd control service and daemon-owned operation routing."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from mlxctl.application.dispatch import ApplicationError
from mlxctl.infrastructure.control_protocol import (
    ControlProtocolError,
    ControlRequest,
    UnixControlServer,
)
from mlxctl.infrastructure.state_store import OperationalStateStore
from mlxctl.infrastructure.system_adapters import SystemClock


class OperationOwner(Protocol):
    def execute(
        self, operation: str, parameters: Mapping[str, object]
    ) -> Mapping[str, object]: ...


class DaemonOperationRouter:
    """Route only daemon-owned mutations and persist live lifecycle observations."""

    def __init__(
        self,
        *,
        runtime: OperationOwner,
        model: OperationOwner,
        supervisor: OperationOwner,
        state: OperationalStateStore,
        request_stop: Callable[[], None] | None = None,
        gateway_host: str = "127.0.0.1",
        gateway_port: int = 8766,
        pressure: Callable[[], object] | None = None,
    ) -> None:
        self._runtime = runtime
        self._model = model
        self._supervisor = supervisor
        self._state = state
        self._request_stop = request_stop or (lambda: None)
        self._gateway_host = gateway_host
        self._gateway_port = gateway_port
        self._pressure = pressure or (lambda: "unknown")

    def execute(
        self,
        operation: str,
        parameters: Mapping[str, object],
        *,
        operation_id: str | None = None,
    ) -> Mapping[str, object]:
        if operation.startswith("runtime."):
            return self._execute_physical(
                self._runtime, operation, parameters, operation_id
            )
        if operation.startswith("model."):
            return self._execute_physical(
                self._model, operation, parameters, operation_id
            )
        if operation.startswith(("supervisor.", "gateway.", "service.")):
            value = self._supervisor.execute(operation, parameters)
            self._record_lifecycle(value)
            if operation == "supervisor.stop":
                self._request_stop()
            return value
        raise ApplicationError(
            "operation_unavailable", f"{operation} is not owned by mlxd"
        )

    def _execute_physical(
        self,
        owner: OperationOwner,
        operation: str,
        parameters: Mapping[str, object],
        operation_id: str | None,
    ) -> Mapping[str, object]:
        identity = operation_id or str(uuid4())
        resource = str(
            parameters.get(
                "resource",
                parameters.get("runtime", parameters.get("repository", operation)),
            )
        )
        self._state.put_operation(
            {
                "id": identity,
                "kind": operation,
                "resource": resource,
                "status": "running",
            }
        )
        self._state.append_event(
            {"kind": "started", "operation_id": identity, "resource": resource}
        )
        try:
            result = owner.execute(operation, parameters)
        except Exception as error:
            self._state.put_operation(
                {
                    "id": identity,
                    "kind": operation,
                    "resource": resource,
                    "status": "failed",
                    "error": str(error),
                }
            )
            self._state.append_event(
                {
                    "kind": "failed",
                    "operation_id": identity,
                    "resource": resource,
                    "error": str(error),
                }
            )
            if isinstance(error, ApplicationError):
                raise
            raise ApplicationError("operation_failed", str(error)) from error
        self._state.put_operation(
            {
                "id": identity,
                "kind": operation,
                "resource": resource,
                "status": "complete",
            }
        )
        self._state.append_event(
            {"kind": "complete", "operation_id": identity, "resource": resource}
        )
        return {**result, "operation_id": identity}

    def cancel(self, operation_id: str) -> bool:
        """Report cancellation honestly until an owned task reaches a cancel point."""

        return False

    def start(self) -> Mapping[str, object]:
        value = self._supervisor.execute("supervisor.start", {})
        self._record_lifecycle(value)
        return value

    def stop(self) -> Mapping[str, object]:
        value = self._supervisor.execute("supervisor.stop", {})
        self._record_lifecycle(value)
        return value

    def _record_lifecycle(self, value: Mapping[str, object]) -> None:
        state = str(value.get("state", "running"))
        observed_pressure = self._pressure()
        pressure = str(getattr(observed_pressure, "value", observed_pressure))
        self._state.put_snapshot(
            {
                "kind": "supervisor",
                "id": "supervisor",
                "version": SystemClock().time_ns(),
                "state": state,
                "pressure": pressure,
            }
        )
        self._state.put_snapshot(
            {
                "kind": "gateway",
                "id": "gateway",
                "version": SystemClock().time_ns(),
                "state": "stopped" if state == "stopped" else "running",
                "host": self._gateway_host,
                "port": self._gateway_port,
            }
        )


class DaemonService:
    """Serve the private control socket until explicit stop or process signal."""

    def __init__(
        self,
        socket_path: Path,
        router_factory: Callable[[Callable[[], None]], DaemonOperationRouter],
        *,
        server_factory: Callable[..., UnixControlServer] = UnixControlServer,
    ) -> None:
        self._socket_path = socket_path
        self._router_factory = router_factory
        self._server_factory = server_factory

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        stopped = asyncio.Event()

        def request_stop() -> None:
            loop.call_soon_threadsafe(stopped.set)

        router = self._router_factory(request_stop)
        initialized = asyncio.Event()
        initialization_error: Exception | None = None

        async def handle(request: ControlRequest, emit) -> Mapping[str, object]:
            await initialized.wait()
            if initialization_error is not None:
                raise ControlProtocolError(
                    "supervisor_start_failed",
                    f"The Supervisor could not start: {initialization_error}",
                )
            await emit({"phase": "started", "operation": request.operation})
            try:
                result = await asyncio.to_thread(
                    router.execute,
                    request.operation,
                    request.parameters,
                    operation_id=request.operation_id,
                )
            except ApplicationError as error:
                raise ControlProtocolError(error.code, error.message) from error
            await emit({"phase": "complete", "operation": request.operation})
            return result

        server = self._server_factory(
            self._socket_path,
            handle,
            cancel_handler=router.cancel,
        )
        installed_signals = []
        start_attempted = False
        try:
            await server.start()
            start_attempted = True
            try:
                await asyncio.to_thread(router.start)
            except Exception as error:
                initialization_error = error
                raise
            finally:
                initialized.set()
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, request_stop)
                    installed_signals.append(signum)
                except (NotImplementedError, RuntimeError):
                    pass
            await stopped.wait()
            # Let the stop request's result frame leave the control connection.
            await asyncio.sleep(0.05)
        finally:
            initialized.set()
            try:
                if start_attempted:
                    await asyncio.to_thread(router.stop)
            finally:
                await server.close()
                for signum in installed_signals:
                    loop.remove_signal_handler(signum)
