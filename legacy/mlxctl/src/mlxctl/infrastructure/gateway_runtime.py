"""Threaded stable Gateway runtime with request activity accounting."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Protocol

from mlxctl.infrastructure.gateway import (
    GatewayRoute,
    create_gateway,
    validate_loopback_bind,
)


class GatewayServer(Protocol):
    started: bool
    should_exit: bool

    def run(self) -> None: ...


ServerFactory = Callable[[object, str, int], GatewayServer]


class GatewayRuntime:
    """Own one loopback Gateway and its current private route table."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        server_factory: ServerFactory | None = None,
        start_timeout: float = 10.0,
        poll_interval: float = 0.01,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        validate_loopback_bind(host)
        if not 1 <= port <= 65535:
            raise ValueError("Gateway port must be in 1..65535")
        if start_timeout <= 0 or poll_interval <= 0:
            raise ValueError("Gateway timeouts must be positive")
        self.host = host
        self.port = port
        self._server_factory = server_factory or _uvicorn_server
        self._start_timeout = start_timeout
        self._poll_interval = poll_interval
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._routes: dict[str, GatewayRoute] = {}
        self._active: dict[str, int] = {}
        self._last_used: dict[str, int] = {}
        self._shedding = False
        self._server: GatewayServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            app = create_gateway(self, bind_host=self.host, activity=self)
            server = self._server_factory(app, self.host, self.port)
            thread = threading.Thread(
                target=server.run,
                name="mlxctl-gateway",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if server.started:
                return
            if not thread.is_alive():
                break
            time.sleep(self._poll_interval)
        self.stop(self._start_timeout)
        raise RuntimeError("Gateway did not start on its configured loopback endpoint")

    def stop(self, timeout: float) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            if server is None or thread is None:
                return
            server.should_exit = True
        if thread is not threading.current_thread():
            thread.join(timeout)
        if thread.is_alive():
            raise RuntimeError("Gateway did not stop before the shutdown deadline")
        with self._lock:
            self._server = None
            self._thread = None

    def set_route(self, service: str, state: str, endpoint: str | None) -> None:
        if state not in {"ready", "stopped", "unavailable"}:
            raise ValueError(f"invalid Gateway route state: {state}")
        with self._lock:
            previous = self._routes.get(service)
            self._routes[service] = GatewayRoute(
                service=service,
                state=state,  # type: ignore[arg-type]
                endpoint=endpoint,
                model=previous.model if previous else None,
                runtime=previous.runtime if previous else None,
            )

    def describe_route(self, route: GatewayRoute) -> None:
        """Set desired model/runtime identity without changing lifecycle state."""

        with self._lock:
            current = self._routes.get(route.service)
            self._routes[route.service] = replace(
                route,
                state=current.state if current else route.state,
                endpoint=current.endpoint if current else route.endpoint,
            )

    def list_routes(self) -> Iterable[GatewayRoute]:
        with self._lock:
            return tuple(self._effective(route) for route in self._routes.values())

    def resolve(self, service: str) -> GatewayRoute | None:
        with self._lock:
            route = self._routes.get(service)
            return self._effective(route) if route is not None else None

    def shed_new_work(self, enabled: bool) -> None:
        with self._lock:
            self._shedding = enabled

    def begin(self, service: str) -> None:
        with self._condition:
            self._active[service] = self._active.get(service, 0) + 1
            self._last_used[service] = self._clock_ns()

    def end(self, service: str) -> None:
        with self._condition:
            current = self._active.get(service, 0)
            self._active[service] = max(0, current - 1)
            self._last_used[service] = self._clock_ns()
            self._condition.notify_all()

    def is_busy(self, service: str) -> bool:
        with self._lock:
            return self._active.get(service, 0) > 0

    def last_used_ns(self, service: str) -> int:
        with self._lock:
            return self._last_used.get(service, 0)

    def drain(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            self._shedding = True
            while any(self._active.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._condition.wait(remaining)

    def _effective(self, route: GatewayRoute) -> GatewayRoute:
        if self._shedding and route.state == "ready":
            return replace(route, state="unavailable", endpoint=None)
        return route


def _uvicorn_server(app: object, host: str, port: int) -> GatewayServer:
    import uvicorn

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    return uvicorn.Server(config)
