"""Threaded stable Gateway runtime with request activity accounting."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from typing import Protocol

from mastic.infrastructure.gateway import (
    GatewayAdmission,
    GatewayRequestProfile,
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
        max_in_flight_per_service: int = 4,
        metric_sink: Callable[[Mapping[str, object]], object] | None = None,
        authenticate: Callable[[str | None], bool] | None = None,
        profile_resolver: Callable[[str, str], GatewayRequestProfile | None]
        | None = None,
    ) -> None:
        validate_loopback_bind(host)
        if not 1 <= port <= 65535:
            raise ValueError("Gateway port must be in 1..65535")
        if start_timeout <= 0 or poll_interval <= 0 or max_in_flight_per_service <= 0:
            raise ValueError("Gateway timeouts must be positive")
        self.host = host
        self.port = port
        self._server_factory = server_factory or _uvicorn_server
        self._start_timeout = start_timeout
        self._poll_interval = poll_interval
        self._clock_ns = clock_ns
        self._max_in_flight_per_service = max_in_flight_per_service
        self._metric_sink = metric_sink or (lambda _metric: None)
        self._authenticate = authenticate
        self._profile_resolver = profile_resolver
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
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
        with self._lifecycle_lock:
            with self._lock:
                if self._thread is not None and self._thread.is_alive():
                    return
                app = create_gateway(
                    self,
                    bind_host=self.host,
                    activity=self,
                    authenticate=self._authenticate,
                    profile_resolver=self._profile_resolver,
                )
                server = self._server_factory(app, self.host, self.port)
                thread = threading.Thread(
                    target=server.run,
                    name="mastic-gateway",
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
            raise RuntimeError(
                "Gateway did not start on its configured loopback endpoint"
            )

    def stop(self, timeout: float) -> None:
        with self._lifecycle_lock:
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
                if self._server is server and self._thread is thread:
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

    def remove_route(self, service: str) -> None:
        """Remove one stopped desired route and its activity metadata."""

        with self._lock:
            if self._active.get(service, 0) > 0:
                raise RuntimeError(f"Gateway route {service!r} is still active")
            self._routes.pop(service, None)
            self._active.pop(service, None)
            self._last_used.pop(service, None)

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

    def begin(self, service: str) -> bool:
        """Reserve capacity without a route snapshot for internal lifecycle tests."""

        with self._condition:
            active = self._active.get(service, 0)
            if self._shedding or active >= self._max_in_flight_per_service:
                admitted = False
                recorded_active = active
            else:
                self._active[service] = active + 1
                recorded_active = self._active[service]
                admitted = True
                self._last_used[service] = self._clock_ns()
        self._record_activity(
            service, "accepted" if admitted else "rejected", recorded_active
        )
        return admitted

    def admit(self, route: GatewayRoute) -> GatewayAdmission:
        """Atomically revalidate one resolved route and reserve its capacity."""

        service = route.service
        with self._condition:
            current = self._routes.get(service)
            effective = self._effective(current) if current is not None else None
            if effective != route or route.state != "ready" or route.endpoint is None:
                admission = GatewayAdmission.UNAVAILABLE
                active = self._active.get(service, 0)
            else:
                active = self._active.get(service, 0)
                if active >= self._max_in_flight_per_service:
                    admission = GatewayAdmission.BUSY
                else:
                    active += 1
                    self._active[service] = active
                    self._last_used[service] = self._clock_ns()
                    admission = GatewayAdmission.ACCEPTED
        self._record_activity(
            service,
            "accepted" if admission is GatewayAdmission.ACCEPTED else "rejected",
            active,
        )
        return admission

    def end(self, service: str) -> None:
        with self._condition:
            current = self._active.get(service, 0)
            self._active[service] = max(0, current - 1)
            self._last_used[service] = self._clock_ns()
            active = self._active[service]
            self._condition.notify_all()
        self._record_activity(service, "complete", active)

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

    def _record_activity(self, service: str, event: str, active: int) -> None:
        observed_at_ns = self._clock_ns()
        common = {
            "service": service,
            "resource": service,
            "event": event,
            "active_requests": active,
            "observed_at_ns": observed_at_ns,
        }
        for metric in (
            {"kind": "gateway_activity", "scope": "gateway", **common},
            {"kind": "service_request", "scope": "service", **common},
        ):
            try:
                self._metric_sink(metric)
            except Exception:
                # Telemetry cannot change admission, capacity, or completion state.
                continue


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
