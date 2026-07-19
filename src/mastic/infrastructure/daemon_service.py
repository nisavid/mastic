"""Foreground masticd control service and daemon-owned operation routing."""

from __future__ import annotations

import asyncio
import signal
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from mastic.application.dispatch import ApplicationError
from mastic.infrastructure.control_protocol import (
    ControlProtocolError,
    ControlRequest,
    UnixControlServer,
)
from mastic.infrastructure.state_store import OperationalStateStore
from mastic.infrastructure.system_adapters import SystemClock


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
        physical_drain_timeout: float = 30.0,
    ) -> None:
        if physical_drain_timeout <= 0:
            raise ValueError("physical operation drain timeout must be positive")
        self._runtime = runtime
        self._model = model
        self._supervisor = supervisor
        self._state = state
        self._request_stop = request_stop or (lambda: None)
        self._gateway_host = gateway_host
        self._gateway_port = gateway_port
        self._pressure = pressure or (lambda: "unknown")
        self._physical_drain_timeout = physical_drain_timeout
        self._condition = threading.Condition(threading.RLock())
        self._active_physical = 0
        self._stopping = False
        self._last_maintenance: tuple[object, ...] | None = None

    def execute(
        self,
        operation: str,
        parameters: Mapping[str, object],
        *,
        operation_id: str | None = None,
    ) -> Mapping[str, object]:
        if operation.startswith("runtime."):
            return self._guarded_physical(
                self._runtime, operation, parameters, operation_id
            )
        if operation.startswith("model."):
            return self._guarded_physical(
                self._model, operation, parameters, operation_id
            )
        if operation.startswith(("supervisor.", "gateway.", "service.")):
            if operation == "supervisor.stop":
                self._prepare_stop()
            else:
                self._assert_accepting()
            try:
                value = self._supervisor.execute(operation, parameters)
            except BaseException:
                if operation == "supervisor.stop":
                    with self._condition:
                        self._stopping = False
                        self._condition.notify_all()
                raise
            self._record_lifecycle(value)
            if operation == "supervisor.stop":
                self._request_stop()
            return value
        raise ApplicationError(
            "operation_unavailable", f"{operation} is not owned by masticd"
        )

    def _guarded_physical(
        self,
        owner: OperationOwner,
        operation: str,
        parameters: Mapping[str, object],
        operation_id: str | None,
    ) -> Mapping[str, object]:
        with self._condition:
            self._assert_accepting_locked()
            self._active_physical += 1
        try:
            return self._execute_physical(owner, operation, parameters, operation_id)
        finally:
            with self._condition:
                self._active_physical -= 1
                self._condition.notify_all()

    def _prepare_stop(self) -> None:
        deadline = time.monotonic() + self._physical_drain_timeout
        with self._condition:
            self._stopping = True
            while self._active_physical:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stopping = False
                    self._condition.notify_all()
                    raise ApplicationError(
                        "physical_operations_busy",
                        "The Supervisor still owns an active Runtime or Model operation.",
                        next_actions=(
                            "wait for the active operation to finish",
                            "retry mastic supervisor stop",
                        ),
                    )
                self._condition.wait(remaining)

    def _assert_accepting(self) -> None:
        with self._condition:
            self._assert_accepting_locked()

    def _assert_accepting_locked(self) -> None:
        if self._stopping:
            raise ApplicationError(
                "supervisor_stopping",
                "The Supervisor is draining and no longer accepts new operations.",
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
        self._prepare_stop()
        try:
            value = self._supervisor.execute("supervisor.stop", {})
            self._record_lifecycle(value)
            return value
        except BaseException:
            with self._condition:
                self._stopping = False
                self._condition.notify_all()
            raise

    def maintain(self) -> Mapping[str, object]:
        with self._condition:
            if self._stopping:
                return {"state": "stopping"}
        value = self._supervisor.execute("supervisor.maintain", {})
        signature = (
            value.get("state"),
            value.get("pressure"),
            value.get("shedding_new_work"),
            tuple(value.get("restarted_services", ())),
            tuple(value.get("stopped_services", ())),
        )
        if signature != self._last_maintenance:
            self._record_lifecycle(value)
            self._last_maintenance = signature
        return value

    def record_maintenance_failure(self, error: Exception) -> None:
        signature = ("maintenance_failed", type(error).__name__, str(error))
        if signature == self._last_maintenance:
            return
        self._state.record_metric(
            {
                "kind": "maintenance_failure",
                "scope": "supervisor",
                "resource": "supervisor",
                "error_type": type(error).__name__,
                "error": str(error),
                "observed_at_ns": SystemClock().time_ns(),
            }
        )
        self._last_maintenance = signature

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
        self._state.record_metric(
            {
                "kind": "lifecycle_state",
                "scope": "supervisor",
                "resource": "supervisor",
                "state": state,
                "pressure": pressure,
                "observed_at_ns": SystemClock().time_ns(),
            }
        )
        self._state.record_metric(
            {
                "kind": "gateway_state",
                "scope": "gateway",
                "resource": "gateway",
                "state": "stopped" if state == "stopped" else "running",
                "observed_at_ns": SystemClock().time_ns(),
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
        maintenance_interval: float = 1.0,
    ) -> None:
        if maintenance_interval <= 0:
            raise ValueError("maintenance interval must be positive")
        self._socket_path = socket_path
        self._router_factory = router_factory
        self._server_factory = server_factory
        self._maintenance_interval = maintenance_interval

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
            if stopped.is_set():
                raise ControlProtocolError(
                    "supervisor_stopping",
                    "The Supervisor is draining and no longer accepts new operations.",
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
        maintenance_task: asyncio.Task[None] | None = None

        async def maintain() -> None:
            while not stopped.is_set():
                try:
                    await asyncio.wait_for(
                        stopped.wait(), timeout=self._maintenance_interval
                    )
                except TimeoutError:
                    try:
                        await asyncio.to_thread(router.maintain)
                    except Exception as error:
                        # A later pass may recover after a transient process/probe error.
                        await asyncio.to_thread(
                            router.record_maintenance_failure, error
                        )
                        continue

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
            maintenance_task = asyncio.create_task(maintain())
            await stopped.wait()
        finally:
            initialized.set()
            stopped.set()
            if maintenance_task is not None:
                await maintenance_task
            try:
                if start_attempted:
                    await asyncio.to_thread(router.stop)
            finally:
                await server.close()
                for signum in installed_signals:
                    loop.remove_signal_handler(signum)
