import threading
import time
import unittest
from unittest.mock import patch

from mastic.infrastructure.gateway import GatewayRoute
from mastic.infrastructure.gateway_runtime import GatewayAdmission, GatewayRuntime


class FakeServer:
    def __init__(self) -> None:
        self.started = False
        self.should_exit = False

    def run(self) -> None:
        self.started = True
        while not self.should_exit:
            time.sleep(0.001)


class GatewayRuntimeTests(unittest.TestCase):
    metrics: list[dict[str, object]]

    def setUp(self) -> None:
        self.server = FakeServer()
        self.now = 100
        self.metrics = []
        self.gateway = GatewayRuntime(
            host="127.0.0.1",
            port=8766,
            server_factory=lambda app, host, port: self.server,
            clock_ns=self._clock,
            max_in_flight_per_service=1,
            metric_sink=self.metrics.append,
        )

    def _clock(self) -> int:
        self.now += 1
        return self.now

    def test_start_stop_and_routes_are_explicit(self) -> None:
        self.gateway.describe_route(
            GatewayRoute("coding", "stopped", model="qwen", runtime="optiq")
        )
        self.gateway.start()
        self.gateway.start()
        self.gateway.set_route("coding", "ready", "http://127.0.0.1:49152")

        route = self.gateway.resolve("coding")
        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.model, "qwen")
        self.assertEqual(route.runtime, "optiq")
        self.assertEqual(route.endpoint, "http://127.0.0.1:49152")

        self.gateway.stop(1)
        self.assertFalse(self.server.started and not self.server.should_exit)

    def test_old_stopper_does_not_clear_a_new_server_generation(self) -> None:
        servers: list[FakeServer] = []
        restart: list[object] = []

        class ControlledThread:
            created = 0

            def __init__(self, *, target, name, daemon) -> None:
                del name, daemon
                type(self).created += 1
                self.generation = type(self).created
                self.target = target
                self.alive = False

            def start(self) -> None:
                self.target.__self__.started = True
                self.alive = self.generation > 1

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout: float) -> None:
                del timeout
                if self.generation == 1:
                    restart_callback = restart.pop()
                    assert callable(restart_callback)
                    restart_callback()

        def make_server(app, host, port) -> FakeServer:
            del app, host, port
            server = FakeServer()
            servers.append(server)
            return server

        gateway = GatewayRuntime(
            host="127.0.0.1",
            port=8766,
            server_factory=make_server,
        )
        restart.append(gateway.start)

        with patch(
            "mastic.infrastructure.gateway_runtime.threading.Thread",
            ControlledThread,
        ):
            gateway.start()
            gateway.stop(1)
            gateway.start()

        self.assertEqual(len(servers), 2)

    def test_start_waits_until_the_stopping_generation_has_fully_exited(self) -> None:
        allow_exit = threading.Event()
        servers: list[FakeServer] = []

        class SlowExitServer(FakeServer):
            def run(self) -> None:
                self.started = True
                while not self.should_exit:
                    time.sleep(0.001)
                allow_exit.wait(1)

        def make_server(app, host, port):
            del app, host, port
            server = SlowExitServer()
            servers.append(server)
            return server

        gateway = GatewayRuntime(
            host="127.0.0.1",
            port=8766,
            server_factory=make_server,
        )
        gateway.start()
        stopping = threading.Thread(target=gateway.stop, args=(1,), daemon=True)
        stopping.start()
        while not servers[0].should_exit:
            time.sleep(0.001)
        started = threading.Event()
        restarting = threading.Thread(
            target=lambda: (gateway.start(), started.set()), daemon=True
        )
        restarting.start()

        self.assertFalse(started.wait(0.05))
        allow_exit.set()
        stopping.join(1)
        restarting.join(1)

        self.assertFalse(stopping.is_alive())
        self.assertFalse(restarting.is_alive())
        self.assertTrue(started.is_set())
        self.assertEqual(len(servers), 2)
        gateway.stop(1)

    def test_activity_prevents_busy_eviction_and_drain_waits(self) -> None:
        self.assertTrue(self.gateway.begin("coding"))
        self.assertFalse(self.gateway.begin("coding"))
        self.assertTrue(self.gateway.is_busy("coding"))
        done = threading.Event()
        entered = threading.Event()

        def drain() -> None:
            entered.set()
            self.gateway.drain(1)
            done.set()

        thread = threading.Thread(target=drain)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(done.wait(0.05))
        self.gateway.end("coding")
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertTrue(done.is_set())
        self.assertFalse(self.gateway.is_busy("coding"))
        self.assertGreater(self.gateway.last_used_ns("coding"), 0)
        self.assertEqual(
            [
                metric["event"]
                for metric in self.metrics
                if metric["scope"] == "gateway"
            ],
            ["accepted", "rejected", "complete"],
        )
        self.assertEqual(
            {metric["scope"] for metric in self.metrics}, {"gateway", "service"}
        )

    def test_shedding_rejects_new_routes_without_forgetting_identity(self) -> None:
        self.gateway.describe_route(
            GatewayRoute(
                "coding",
                "ready",
                "http://127.0.0.1:49152",
                "qwen",
                "optiq",
            )
        )
        self.gateway.shed_new_work(True)

        route = self.gateway.resolve("coding")

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.state, "unavailable")
        self.assertIsNone(route.endpoint)
        self.assertEqual(route.model, "qwen")

    def test_metric_failures_never_leak_or_block_request_capacity(self) -> None:
        gateway = GatewayRuntime(
            host="127.0.0.1",
            port=8766,
            server_factory=lambda app, host, port: self.server,
            max_in_flight_per_service=1,
            metric_sink=lambda _metric: (_ for _ in ()).throw(OSError("disk full")),
        )
        route = GatewayRoute(
            "coding", "ready", "http://127.0.0.1:49152", "qwen", "optiq"
        )
        gateway.describe_route(route)

        self.assertEqual(gateway.admit(route), GatewayAdmission.ACCEPTED)
        self.assertEqual(gateway.admit(route), GatewayAdmission.BUSY)
        gateway.end("coding")
        self.assertEqual(gateway.admit(route), GatewayAdmission.ACCEPTED)

    def test_admission_revalidates_the_exact_route_atomically(self) -> None:
        route = GatewayRoute(
            "coding", "ready", "http://127.0.0.1:49152", "qwen", "optiq"
        )
        self.gateway.describe_route(route)
        self.gateway.set_route("coding", "stopped", None)

        self.assertEqual(self.gateway.admit(route), GatewayAdmission.UNAVAILABLE)
        self.assertFalse(self.gateway.is_busy("coding"))


if __name__ == "__main__":
    unittest.main()
