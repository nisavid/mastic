from __future__ import annotations

import asyncio
import json
import unittest

import httpx
from starlette.testclient import TestClient

from mlxctl.infrastructure.gateway import (
    GatewayRoute,
    create_gateway,
    validate_loopback_bind,
)


class FakeResolver:
    def __init__(self, routes: list[GatewayRoute]) -> None:
        self.routes = {route.service: route for route in routes}

    async def list_routes(self) -> list[GatewayRoute]:
        return list(self.routes.values())

    async def resolve(self, service: str) -> GatewayRoute | None:
        return self.routes.get(service)


class FakeActivity:
    def __init__(self) -> None:
        self.active: dict[str, int] = {}
        self.events: list[tuple[str, str]] = []

    def begin(self, service: str) -> bool:
        self.active[service] = self.active.get(service, 0) + 1
        self.events.append(("begin", service))
        return True

    def end(self, service: str) -> None:
        self.active[service] -= 1
        self.events.append(("end", service))


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False
        self.pulled = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class FakeUpstreamClient:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []
        self.entered = False
        self.closed = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.closed = True

    def build_request(self, method: str, url: str, **kwargs) -> httpx.Request:
        return httpx.Request(method, url, **kwargs)

    async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("No fake upstream response was queued")
        response = self.responses.pop(0)
        response.request = request
        return response


class GatewayTests(unittest.TestCase):
    def test_bind_validation_accepts_only_literal_loopback_addresses(self) -> None:
        self.assertEqual(validate_loopback_bind("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_loopback_bind("::1"), "::1")
        for unsafe in ("0.0.0.0", "::", "192.168.1.4", "localhost", "example.com"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                validate_loopback_bind(unsafe)

    def test_models_lists_service_routes_and_readiness_without_upstream_addresses(
        self,
    ) -> None:
        resolver = FakeResolver(
            [
                GatewayRoute(
                    service="coding",
                    state="ready",
                    endpoint="http://127.0.0.1:49152",
                    model="qwen-coder",
                    runtime="optiq@0.2.18",
                ),
                GatewayRoute(
                    service="vision",
                    state="stopped",
                    model="qwen-vl",
                    runtime="mlx_vlm@0.3.3",
                ),
            ]
        )
        upstream = FakeUpstreamClient()
        app = create_gateway(resolver, client_factory=lambda: upstream)

        with TestClient(app) as client:
            response = client.get("/v1/models")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "object": "list",
                "data": [
                    {
                        "id": "coding",
                        "object": "model",
                        "created": 0,
                        "owned_by": "mlxctl",
                        "status": "ready",
                        "model": "qwen-coder",
                        "runtime": "optiq@0.2.18",
                    },
                    {
                        "id": "vision",
                        "object": "model",
                        "created": 0,
                        "owned_by": "mlxctl",
                        "status": "stopped",
                        "model": "qwen-vl",
                        "runtime": "mlx_vlm@0.3.3",
                    },
                ],
            },
        )
        self.assertNotIn("49152", response.text)
        self.assertTrue(upstream.entered)
        self.assertTrue(upstream.closed)

    def test_chat_and_responses_route_model_field_by_service_name(self) -> None:
        resolver = FakeResolver(
            [
                GatewayRoute(
                    service="coding", state="ready", endpoint="http://127.0.0.1:49152"
                )
            ]
        )
        upstream = FakeUpstreamClient()
        upstream.responses.extend(
            [
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":"chat-1","object":"chat.completion"}']),
                ),
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":"resp-1","object":"response"}']),
                ),
            ]
        )
        app = create_gateway(resolver, client_factory=lambda: upstream)

        with TestClient(app) as client:
            chat = client.post(
                "/v1/chat/completions",
                headers={"authorization": "Bearer local"},
                json={
                    "model": "coding",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            response = client.post(
                "/v1/responses",
                json={"model": "coding", "input": "hello"},
            )

        self.assertEqual(chat.status_code, 200)
        self.assertEqual(chat.json()["id"], "chat-1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "resp-1")
        self.assertEqual(
            [request.url for request in upstream.requests],
            [
                httpx.URL("http://127.0.0.1:49152/v1/chat/completions"),
                httpx.URL("http://127.0.0.1:49152/v1/responses"),
            ],
        )
        self.assertNotIn("authorization", upstream.requests[0].headers)
        self.assertEqual(json.loads(upstream.requests[0].content)["model"], "coding")

    def test_stopped_missing_and_unavailable_services_return_actionable_errors(
        self,
    ) -> None:
        resolver = FakeResolver(
            [
                GatewayRoute(service="stopped", state="stopped"),
                GatewayRoute(
                    service="broken",
                    state="unavailable",
                    endpoint="http://127.0.0.1:49153",
                ),
            ]
        )
        upstream = FakeUpstreamClient()
        app = create_gateway(resolver, client_factory=lambda: upstream)

        with TestClient(app) as client:
            stopped = client.post(
                "/v1/responses", json={"model": "stopped", "input": "x"}
            )
            missing = client.post(
                "/v1/responses", json={"model": "missing", "input": "x"}
            )
            broken = client.post(
                "/v1/responses", json={"model": "broken", "input": "x"}
            )

        self.assertEqual(
            (stopped.status_code, stopped.json()["error"]["code"]),
            (409, "service_stopped"),
        )
        self.assertIn("mlxctl service start stopped", stopped.json()["error"]["action"])
        self.assertEqual(
            (missing.status_code, missing.json()["error"]["code"]),
            (404, "service_not_found"),
        )
        self.assertIn("mlxctl service list", missing.json()["error"]["action"])
        self.assertEqual(
            (broken.status_code, broken.json()["error"]["code"]),
            (503, "service_unavailable"),
        )
        self.assertIn("mlxctl service inspect broken", broken.json()["error"]["action"])
        self.assertEqual(upstream.requests, [])

    def test_resolver_cannot_route_to_an_arbitrary_destination(self) -> None:
        resolver = FakeResolver(
            [
                GatewayRoute(
                    service="unsafe", state="ready", endpoint="https://example.com:443"
                )
            ]
        )
        upstream = FakeUpstreamClient()
        app = create_gateway(resolver, client_factory=lambda: upstream)

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses", json={"model": "unsafe", "input": "x"}
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "invalid_upstream_endpoint")
        self.assertEqual(upstream.requests, [])

    def test_streaming_response_is_forwarded_and_upstream_is_closed(self) -> None:
        resolver = FakeResolver(
            [
                GatewayRoute(
                    service="coding", state="ready", endpoint="http://[::1]:49152"
                )
            ]
        )
        stream = ChunkStream([b"data: one\n\n", b"data: two\n\n", b"data: [DONE]\n\n"])
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "x-request-id": "upstream-1",
                },
                stream=stream,
            )
        )
        activity = FakeActivity()
        app = create_gateway(
            resolver, client_factory=lambda: upstream, activity=activity
        )

        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={"model": "coding", "stream": True, "messages": []},
            ) as response:
                body = b"".join(response.iter_bytes())

        self.assertEqual(body, b"data: one\n\ndata: two\n\ndata: [DONE]\n\n")
        self.assertEqual(response.headers["content-type"], "text/event-stream")
        self.assertEqual(response.headers["x-request-id"], "upstream-1")
        self.assertEqual(stream.pulled, 3)
        self.assertTrue(stream.closed)
        self.assertEqual(activity.events, [("begin", "coding"), ("end", "coding")])
        self.assertEqual(activity.active["coding"], 0)

    def test_invalid_json_or_model_is_rejected_without_contacting_upstream(
        self,
    ) -> None:
        resolver = FakeResolver([])
        upstream = FakeUpstreamClient()
        app = create_gateway(resolver, client_factory=lambda: upstream)

        with TestClient(app) as client:
            invalid_json = client.post(
                "/v1/responses",
                content=b"{",
                headers={"content-type": "application/json"},
            )
            missing_model = client.post("/v1/responses", json={"input": "x"})

        self.assertEqual(invalid_json.status_code, 400)
        self.assertEqual(invalid_json.json()["error"]["code"], "invalid_json")
        self.assertEqual(missing_model.status_code, 400)
        self.assertEqual(missing_model.json()["error"]["code"], "model_required")
        self.assertEqual(upstream.requests, [])

    def test_oversized_request_is_rejected_before_buffering_or_routing(self) -> None:
        resolver = FakeResolver(
            [
                GatewayRoute(
                    service="coding",
                    state="ready",
                    endpoint="http://127.0.0.1:49152",
                )
            ]
        )
        upstream = FakeUpstreamClient()
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            max_request_bytes=64,
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={"model": "coding", "input": "x" * 128},
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "request_too_large")
        self.assertEqual(upstream.requests, [])

    def test_proxy_requires_json_and_rejects_non_loopback_browser_origins(self) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        upstream = FakeUpstreamClient()
        app = create_gateway(resolver, client_factory=lambda: upstream)

        with TestClient(app) as client:
            wrong_type = client.post(
                "/v1/responses",
                content='{"model":"coding"}',
                headers={"content-type": "text/plain"},
            )
            hostile_origin = client.post(
                "/v1/responses",
                json={"model": "coding", "input": "x"},
                headers={"origin": "https://attacker.example"},
            )

        self.assertEqual(wrong_type.status_code, 415)
        self.assertEqual(wrong_type.json()["error"]["code"], "unsupported_media_type")
        self.assertEqual(hostile_origin.status_code, 403)
        self.assertEqual(hostile_origin.json()["error"]["code"], "origin_not_allowed")
        self.assertEqual(upstream.requests, [])

    def test_bounded_admission_rejects_excess_work_before_upstream(self) -> None:
        class RejectingActivity(FakeActivity):
            def begin(self, service: str) -> bool:
                self.events.append(("rejected", service))
                return False

        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        upstream = FakeUpstreamClient()
        activity = RejectingActivity()
        app = create_gateway(
            resolver, client_factory=lambda: upstream, activity=activity
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses", json={"model": "coding", "input": "x"}
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "service_busy")
        self.assertEqual(upstream.requests, [])


class GatewayStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_downstream_backpressure_and_disconnect_close_upstream(self) -> None:
        resolver = FakeResolver(
            [
                GatewayRoute(
                    service="coding", state="ready", endpoint="http://127.0.0.1:49152"
                )
            ]
        )
        stream = ChunkStream([b"data: one\n\n", b"data: two\n\n", b"data: three\n\n"])
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        )
        app = create_gateway(resolver, client_factory=lambda: upstream)
        first_chunk_waiting = asyncio.Event()
        release_first_chunk = asyncio.Event()
        body_messages = 0

        request_body = json.dumps(
            {"model": "coding", "stream": True, "messages": []}
        ).encode()
        received_request = False

        async def receive():
            nonlocal received_request
            if not received_request:
                received_request = True
                return {
                    "type": "http.request",
                    "body": request_body,
                    "more_body": False,
                }
            await asyncio.Future()

        async def send(message):
            nonlocal body_messages
            if message["type"] != "http.response.body" or not message.get("body"):
                return
            body_messages += 1
            if body_messages == 1:
                first_chunk_waiting.set()
                await release_first_chunk.wait()
                return
            raise OSError("client disconnected")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 60000),
            "server": ("127.0.0.1", 8766),
            "root_path": "",
            "state": {"http_client": upstream},
        }

        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(first_chunk_waiting.wait(), timeout=1)
        await asyncio.sleep(0)
        self.assertEqual(stream.pulled, 1, "upstream must not outrun downstream send")

        release_first_chunk.set()
        outcome = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(outcome[0], Exception)
        self.assertEqual(stream.pulled, 2)
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
