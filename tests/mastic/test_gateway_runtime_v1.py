import threading
import time
import unittest

from mastic.infrastructure.gateway import GatewayRoute
from mastic.infrastructure.gateway_runtime import GatewayRuntime


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

    def test_activity_prevents_busy_eviction_and_drain_waits(self) -> None:
        self.assertTrue(self.gateway.begin("coding"))
        self.assertFalse(self.gateway.begin("coding"))
        self.assertTrue(self.gateway.is_busy("coding"))
        done = threading.Event()

        def drain() -> None:
            self.gateway.drain(1)
            done.set()

        thread = threading.Thread(target=drain)
        thread.start()
        time.sleep(0.01)
        self.assertFalse(done.is_set())
        self.gateway.end("coding")
        thread.join(1)

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


if __name__ == "__main__":
    unittest.main()
