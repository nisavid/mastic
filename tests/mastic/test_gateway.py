from __future__ import annotations

import asyncio
import json
import unittest

import httpx
from starlette.testclient import TestClient

from mastic.infrastructure.gateway import (
    GatewayRequestProfile,
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


class PostTerminalStream(ChunkStream):
    async def __aiter__(self):
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk
        raise AssertionError("Gateway pulled the upstream after semantic completion")


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
    def test_gateway_requires_correct_bearer_for_models_and_inference(self) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=ChunkStream([b'{"id":"response-1"}']),
            )
        )
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            authenticate=lambda value: value == "Bearer private-token",
        )

        with TestClient(app) as client:
            missing = client.get("/v1/models")
            wrong = client.post(
                "/v1/responses",
                headers={"authorization": "Bearer wrong"},
                json={"model": "coding", "input": "hello"},
            )
            models = client.get(
                "/v1/models",
                headers={"authorization": "Bearer private-token"},
            )
            response = client.post(
                "/v1/responses",
                headers={"authorization": "Bearer private-token"},
                json={"model": "coding", "input": "hello"},
            )

        self.assertEqual((missing.status_code, wrong.status_code), (401, 401))
        self.assertEqual(missing.json()["error"]["code"], "authentication_required")
        self.assertEqual(wrong.json()["error"]["code"], "authentication_required")
        self.assertEqual(models.status_code, 200)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("authorization", upstream.requests[0].headers)

    def test_bind_validation_accepts_only_literal_loopback_addresses(self) -> None:
        self.assertEqual(validate_loopback_bind("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_loopback_bind("::1"), "::1")
        for unsafe in ("0.0.0.0", "::", "192.168.1.4", "localhost", "example.com"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                validate_loopback_bind(unsafe)

    def test_gateway_requires_positive_response_adaptation_limits(self) -> None:
        resolver = FakeResolver([])
        for option in (
            {"max_response_adaptation_bytes": 0},
            {"max_response_adapters": 0},
        ):
            with (
                self.subTest(option=option),
                self.assertRaisesRegex(
                    ValueError, "Gateway request limits must be positive"
                ),
            ):
                create_gateway(resolver, **option)

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
                        "owned_by": "mastic",
                        "status": "ready",
                        "model": "qwen-coder",
                        "runtime": "optiq@0.2.18",
                    },
                    {
                        "id": "vision",
                        "object": "model",
                        "created": 0,
                        "owned_by": "mastic",
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

    def test_responses_adapts_codex_namespaces_without_changing_chat(self) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        upstream = FakeUpstreamClient()
        upstream.responses.extend(
            [
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":"chat-1","name":"math__add"}']),
                ),
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream(
                        [
                            b'{"id":"resp-1","output":[{"type":"function_call",',
                            b'"name":"math__add","arguments":"{}"}]}',
                        ]
                    ),
                ),
            ]
        )
        app = create_gateway(resolver, client_factory=lambda: upstream)
        namespaced_tool = {
            "type": "namespace",
            "name": "math",
            "description": "Math tools",
            "tools": [
                {
                    "type": "function",
                    "name": "add",
                    "description": "Add two numbers",
                    "parameters": {},
                }
            ],
        }

        with TestClient(app) as client:
            chat = client.post(
                "/v1/chat/completions",
                json={"model": "coding", "messages": [], "tools": [namespaced_tool]},
            )
            response = client.post(
                "/v1/responses",
                json={"model": "coding", "input": [], "tools": [namespaced_tool]},
            )

        self.assertEqual(chat.json(), {"id": "chat-1", "name": "math__add"})
        self.assertEqual(
            json.loads(upstream.requests[0].content)["tools"], [namespaced_tool]
        )
        forwarded = json.loads(upstream.requests[1].content)
        self.assertEqual(forwarded["tools"][0]["name"], "math__add")
        self.assertEqual(
            forwarded["tools"][0]["description"],
            "[math] Math tools\n\nAdd two numbers",
        )
        self.assertEqual(
            response.json()["output"][0],
            {
                "type": "function_call",
                "namespace": "math",
                "name": "add",
                "arguments": "{}",
            },
        )

    def test_responses_rejects_non_string_namespace_descriptions_before_upstream(
        self,
    ) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        upstream = FakeUpstreamClient()
        upstream.responses.extend(
            [
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":"unexpected"}']),
                )
                for _ in range(2)
            ]
        )
        app = create_gateway(resolver, client_factory=lambda: upstream)

        with TestClient(app) as client:
            responses = [
                client.post(
                    "/v1/responses",
                    json={
                        "model": "coding",
                        "input": [],
                        "tools": [
                            {
                                "type": "namespace",
                                "name": "math",
                                "description": description,
                                "tools": [
                                    {
                                        "type": "function",
                                        "name": "add",
                                        "parameters": {},
                                    }
                                ],
                            }
                        ],
                    },
                )
                for description in (["not", "text"], {"not": "text"})
            ]

        self.assertEqual([response.status_code for response in responses], [400, 400])
        self.assertEqual(
            [response.json()["error"]["code"] for response in responses],
            ["invalid_namespace", "invalid_namespace"],
        )
        self.assertEqual(upstream.requests, [])

    def test_responses_continuation_reuses_the_exact_namespace_mapping(self) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        upstream = FakeUpstreamClient()
        upstream.responses.extend(
            [
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":"resp-1","output":[]}']),
                ),
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream(
                        [
                            b'{"id":"resp-2","output":[{"type":"function_call",',
                            b'"name":"math__add","arguments":"{}"}]}',
                        ]
                    ),
                ),
            ]
        )
        app = create_gateway(resolver, client_factory=lambda: upstream)

        with TestClient(app) as client:
            first = client.post(
                "/v1/responses",
                json={
                    "model": "coding",
                    "input": [],
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "math",
                            "tools": [
                                {"type": "function", "name": "add", "parameters": {}}
                            ],
                        }
                    ],
                },
            )
            continued = client.post(
                "/v1/responses",
                json={
                    "model": "coding",
                    "previous_response_id": "resp-1",
                    "input": [],
                },
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            continued.json()["output"][0],
            {
                "type": "function_call",
                "namespace": "math",
                "name": "add",
                "arguments": "{}",
            },
        )
        self.assertEqual(
            json.loads(upstream.requests[1].content)["previous_response_id"],
            "resp-1",
        )

    def test_responses_adapter_cache_is_service_isolated_and_bounded(self) -> None:
        resolver = FakeResolver(
            [
                GatewayRoute(service, "ready", "http://127.0.0.1:49152")
                for service in ("coding", "analysis", "vision")
            ]
        )
        upstream = FakeUpstreamClient()
        upstream.responses.extend(
            [
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":"shared","output":[]}']),
                ),
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":"shared","output":[]}']),
                ),
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream(
                        [
                            b'{"id":"coding-next","output":[{"type":"function_call",',
                            b'"name":"math__add","arguments":"{}"}]}',
                        ]
                    ),
                ),
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":"resp-vision","output":[]}']),
                ),
            ]
        )
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            max_response_adapters=2,
        )

        def namespaced_tool(namespace: str, name: str) -> dict[str, object]:
            return {
                "type": "namespace",
                "name": namespace,
                "tools": [{"type": "function", "name": name, "parameters": {}}],
            }

        with TestClient(app) as client:
            coding = client.post(
                "/v1/responses",
                json={
                    "model": "coding",
                    "input": [],
                    "tools": [namespaced_tool("math", "add")],
                },
            )
            analysis = client.post(
                "/v1/responses",
                json={
                    "model": "analysis",
                    "input": [],
                    "tools": [namespaced_tool("geo", "locate")],
                },
            )
            coding_continuation = client.post(
                "/v1/responses",
                json={
                    "model": "coding",
                    "previous_response_id": "shared",
                    "input": [],
                },
            )
            vision = client.post(
                "/v1/responses",
                json={"model": "vision", "input": []},
            )
            evicted_analysis = client.post(
                "/v1/responses",
                json={
                    "model": "analysis",
                    "previous_response_id": "shared",
                    "input": [],
                },
            )

        self.assertEqual((coding.status_code, analysis.status_code), (200, 200))
        self.assertEqual(coding_continuation.json()["output"][0]["namespace"], "math")
        self.assertEqual(vision.status_code, 200)
        self.assertEqual(evicted_analysis.status_code, 400)
        self.assertEqual(evicted_analysis.json()["error"]["code"], "invalid_namespace")
        self.assertEqual(len(upstream.requests), 4)

    def test_profiled_endpoints_enforce_supported_generation_parameters(self) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        upstream = FakeUpstreamClient()
        upstream.responses.extend(
            [
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":"chat-profile"}']),
                ),
                httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    stream=ChunkStream([b'{"id":"responses-profile"}']),
                ),
            ]
        )
        profile = GatewayRequestProfile(
            service="coding",
            parameters={
                "temperature": 0.7,
                "top_p": 0.8,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 1.5,
                "repetition_penalty": 1.0,
                "enable_thinking": False,
            },
        )
        coding = GatewayRequestProfile(
            service="coding",
            parameters={
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
                "repetition_penalty": 1.0,
                "enable_thinking": True,
                "preserve_thinking": True,
            },
        )
        profiles = {
            ("hindsight", "retain"): profile,
            ("codex", "coding"): coding,
        }
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            profile_resolver=lambda application_target, name: profiles.get(
                (application_target, name)
            ),
        )

        with TestClient(app) as client:
            chat = client.post(
                "/application-targets/hindsight/profiles/retain/v1/chat/completions",
                json={
                    "model": "coding",
                    "messages": [{"role": "user", "content": "remember this"}],
                    "temperature": 0.0,
                    "top_p": 1.0,
                },
            )
            responses = client.post(
                "/application-targets/codex/profiles/coding/v1/responses",
                json={"model": "coding", "input": "fix this", "temperature": 0.0},
            )
            missing = client.post(
                "/application-targets/codex/profiles/missing/v1/responses",
                json={"model": "coding", "input": "fix this"},
            )
            legacy = client.post(
                "/clients/codex/profiles/coding/v1/responses",
                json={"model": "coding", "input": "fix this"},
            )

        self.assertEqual((chat.status_code, responses.status_code), (200, 200))
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "profile_not_found")
        self.assertEqual(legacy.status_code, 404)
        chat_body = json.loads(upstream.requests[0].content)
        self.assertEqual(
            {
                key: chat_body[key]
                for key in profile.parameters
                if key not in {"enable_thinking", "preserve_thinking"}
            },
            {
                "temperature": 0.7,
                "top_p": 0.8,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 1.5,
                "repetition_penalty": 1.0,
            },
        )
        self.assertEqual(chat_body["chat_template_kwargs"], {"enable_thinking": False})
        responses_body = json.loads(upstream.requests[1].content)
        self.assertEqual(responses_body["temperature"], 0.6)
        self.assertEqual(responses_body["top_p"], 0.95)
        self.assertEqual(responses_body["top_k"], 20)
        self.assertEqual(
            responses_body["chat_template_kwargs"],
            {"enable_thinking": True, "preserve_thinking": True},
        )
        for unsupported in ("min_p", "presence_penalty", "repetition_penalty"):
            self.assertNotIn(unsupported, responses_body)
        self.assertEqual(
            [request.url for request in upstream.requests],
            [
                httpx.URL("http://127.0.0.1:49152/v1/chat/completions"),
                httpx.URL("http://127.0.0.1:49152/v1/responses"),
            ],
        )

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
        self.assertIn("mastic service start stopped", stopped.json()["error"]["action"])
        self.assertEqual(
            (missing.status_code, missing.json()["error"]["code"]),
            (404, "service_not_found"),
        )
        self.assertIn("mastic service list", missing.json()["error"]["action"])
        self.assertEqual(
            (broken.status_code, broken.json()["error"]["code"]),
            (503, "service_unavailable"),
        )
        self.assertIn("mastic service inspect broken", broken.json()["error"]["action"])
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

    def test_responses_stream_stops_and_releases_activity_at_terminal_event(self):
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        terminal = (
            b": keepalive\n\n"
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"resp-1"}}\n\n'
        )
        stream = PostTerminalStream([terminal[:37], terminal[37:]])
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
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
                "/v1/responses",
                json={"model": "coding", "stream": True, "input": "hello"},
            ) as response:
                body = b"".join(response.iter_bytes())

        self.assertEqual(body, terminal)
        self.assertTrue(stream.closed)
        self.assertEqual(activity.events, [("begin", "coding"), ("end", "coding")])
        self.assertEqual(activity.active["coding"], 0)

    def test_responses_stream_reconstructs_namespaces_and_preserves_other_frames(
        self,
    ) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        stream_bytes = (
            b"event: response.output_item.added\n"
            b'data: {"type":"response.output_item.added","item":{"type":"function_call","name":"math__add","arguments":"{}"}}\n\n'
            b"event: response.output_text.delta\n"
            b'data:  {"type":"response.output_text.delta","delta":"hello"}\n\n'
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"resp-1","output":[]}}\n\n'
        )
        stream = ChunkStream(
            [stream_bytes[:51], stream_bytes[51:173], stream_bytes[173:]]
        )
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        )
        app = create_gateway(resolver, client_factory=lambda: upstream)

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={
                    "model": "coding",
                    "stream": True,
                    "input": [],
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "math",
                            "tools": [{"type": "function", "name": "add"}],
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        frames = response.content.split(b"\n\n")
        added = json.loads(frames[0].split(b"data: ", 1)[1])
        self.assertEqual(
            added["item"],
            {
                "type": "function_call",
                "namespace": "math",
                "name": "add",
                "arguments": "{}",
            },
        )
        self.assertIn(
            b'data:  {"type":"response.output_text.delta","delta":"hello"}',
            response.content,
        )
        self.assertTrue(stream.closed)

    def test_responses_json_body_limit_closes_upstream_and_releases_activity(
        self,
    ) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        stream = ChunkStream([b'{"id":"resp-1",', b'"output":[]}'])
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=stream,
            )
        )
        activity = FakeActivity()
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            activity=activity,
            max_response_adaptation_bytes=16,
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={"model": "coding", "input": "hello"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["error"]["code"], "response_adaptation_too_large"
        )
        self.assertIn("16-byte limit", response.json()["error"]["message"])
        self.assertTrue(stream.closed)
        self.assertEqual(activity.events, [("begin", "coding"), ("end", "coding")])
        self.assertEqual(activity.active["coding"], 0)

    def test_responses_json_limit_rejects_namespace_reconstruction_expansion(
        self,
    ) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        body = (
            b'{"id":"resp-1","output":[{"type":"function_call",'
            b'"name":"math__add","arguments":"{}"}]}'
        )
        stream = ChunkStream([body])
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=stream,
            )
        )
        activity = FakeActivity()
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            activity=activity,
            max_response_adaptation_bytes=len(body),
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={
                    "model": "coding",
                    "input": [],
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "math",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "add",
                                    "parameters": {},
                                }
                            ],
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["error"]["code"], "response_adaptation_too_large"
        )
        self.assertIn(f"{len(body)}-byte limit", response.json()["error"]["message"])
        self.assertTrue(stream.closed)
        self.assertEqual(activity.events, [("begin", "coding"), ("end", "coding")])
        self.assertEqual(activity.active["coding"], 0)

    def test_responses_rejects_gzip_before_decoding_and_releases_activity(
        self,
    ) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        stream = ChunkStream([b"compressed body must not be consumed"])
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
                stream=stream,
            )
        )
        activity = FakeActivity()
        app = create_gateway(
            resolver, client_factory=lambda: upstream, activity=activity
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={"model": "coding", "input": "hello"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["error"]["code"],
            "unsupported_upstream_response_encoding",
        )
        self.assertIn("Content-Encoding: identity", response.json()["error"]["action"])
        self.assertEqual(stream.pulled, 0)
        self.assertTrue(stream.closed)
        self.assertEqual(activity.events, [("begin", "coding"), ("end", "coding")])
        self.assertEqual(activity.active["coding"], 0)

    def test_responses_sse_frame_limit_closes_upstream_and_releases_activity(
        self,
    ) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        stream = ChunkStream([b'data: {"delta":"', b'over-limit"}'])
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        )
        activity = FakeActivity()
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            activity=activity,
            max_response_adaptation_bytes=16,
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={"model": "coding", "stream": True, "input": "hello"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["error"]["code"], "response_adaptation_too_large"
        )
        self.assertTrue(stream.closed)
        self.assertEqual(activity.events, [("begin", "coding"), ("end", "coding")])
        self.assertEqual(activity.active["coding"], 0)

    def test_responses_sse_limit_rejects_namespace_reconstruction_expansion(
        self,
    ) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        frame = (
            b'data: {"type":"response.output_item.added","item":'
            b'{"type":"function_call","name":"math__add","arguments":"{}"}}\n\n'
        )
        stream = ChunkStream([frame])
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        )
        activity = FakeActivity()
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            activity=activity,
            max_response_adaptation_bytes=len(frame),
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={
                    "model": "coding",
                    "stream": True,
                    "input": [],
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "math",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "add",
                                    "parameters": {},
                                }
                            ],
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["error"]["code"], "response_adaptation_too_large"
        )
        self.assertTrue(stream.closed)
        self.assertEqual(activity.events, [("begin", "coding"), ("end", "coding")])
        self.assertEqual(activity.active["coding"], 0)

    def test_responses_sse_response_limit_allows_multiple_small_frames(
        self,
    ) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        frames = b"data: {}\n\ndata: {}\n\n"
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ChunkStream([frames]),
            )
        )
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            max_response_adaptation_bytes=len(b"data: {}\n\n"),
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={"model": "coding", "stream": True, "input": "hello"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, frames)

    def test_responses_sse_late_limit_emits_explicit_failed_terminal(self) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        created = (
            b"event: response.created\n"
            b'data: {"type":"response.created","response":{"id":"resp-active"},'
            b'"sequence_number":7}\n\n'
        )
        delta = (
            b"event: response.output_text.delta\n"
            b'data: {"type":"response.output_text.delta","delta":"ok",'
            b'"sequence_number":8}\n\n'
        )
        oversized = b'data: {"padding":"' + (b"x" * 600) + b'"}\n\n'
        stream = ChunkStream([created, delta, oversized])
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        )
        activity = FakeActivity()
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            activity=activity,
            max_response_adaptation_bytes=512,
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={"model": "coding", "stream": True, "input": "hello"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(created + delta))
        failed_frame = response.content[len(created + delta) :]
        event_line, data_line = failed_frame.strip().splitlines()
        event = json.loads(data_line.removeprefix(b"data: "))
        self.assertEqual(event_line, b"event: response.failed")
        self.assertEqual(event["type"], "response.failed")
        self.assertEqual(event["sequence_number"], 9)
        self.assertEqual(event["response"]["id"], "resp-active")
        self.assertEqual(event["response"]["status"], "failed")
        self.assertEqual(event["response"]["error"]["code"], "server_error")
        self.assertTrue(stream.closed)
        self.assertEqual(activity.events, [("begin", "coding"), ("end", "coding")])
        self.assertEqual(activity.active["coding"], 0)

    def test_responses_sse_single_chunk_never_buffers_an_oversized_tail(
        self,
    ) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        first = b"data: {}\n\n"
        stream = ChunkStream([first + b"data: " + (b"x" * 128)])
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        )
        activity = FakeActivity()
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            activity=activity,
            max_response_adaptation_bytes=16,
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={"model": "coding", "stream": True, "input": "hello"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(first))
        self.assertNotIn(b"x" * 17, response.content)
        error_frame = response.content[len(first) :]
        event_line, data_line = error_frame.strip().splitlines()
        event = json.loads(data_line.removeprefix(b"data: "))
        self.assertEqual(event_line, b"event: error")
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["code"], "response_adaptation_too_large")
        self.assertTrue(stream.closed)
        self.assertEqual(activity.events, [("begin", "coding"), ("end", "coding")])
        self.assertEqual(activity.active["coding"], 0)

    def test_responses_sse_complete_frame_limit_closes_upstream_and_releases_activity(
        self,
    ) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        stream = ChunkStream([b'data: {"delta":"over-limit"}\n\n'])
        upstream = FakeUpstreamClient()
        upstream.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        )
        activity = FakeActivity()
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            activity=activity,
            max_response_adaptation_bytes=16,
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                json={"model": "coding", "stream": True, "input": "hello"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["error"]["code"], "response_adaptation_too_large"
        )
        self.assertTrue(stream.closed)
        self.assertEqual(activity.events, [("begin", "coding"), ("end", "coding")])
        self.assertEqual(activity.active["coding"], 0)

    def test_stream_close_failure_still_releases_activity(self) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        stream = ChunkStream([b"data: [DONE]\n\n"])
        upstream = FakeUpstreamClient()
        upstream_response = httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

        async def fail_close() -> None:
            raise RuntimeError("upstream close failed")

        upstream_response.aclose = fail_close  # type: ignore[method-assign]
        upstream.responses.append(upstream_response)
        activity = FakeActivity()
        app = create_gateway(
            resolver, client_factory=lambda: upstream, activity=activity
        )

        with self.assertRaisesRegex(RuntimeError, "upstream close failed"):
            with TestClient(app) as client:
                client.post(
                    "/v1/chat/completions",
                    json={"model": "coding", "stream": True, "messages": []},
                )

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

    def test_responses_namespace_expansion_respects_request_limit(self) -> None:
        resolver = FakeResolver(
            [GatewayRoute("coding", "ready", "http://127.0.0.1:49152")]
        )
        upstream = FakeUpstreamClient()
        payload = {
            "model": "coding",
            "input": [],
            "tools": [
                {
                    "type": "namespace",
                    "name": "n",
                    "description": "d" * 100,
                    "tools": [
                        {"type": "function", "name": "a"},
                        {"type": "function", "name": "b"},
                        {"type": "function", "name": "c"},
                        {"type": "function", "name": "d"},
                    ],
                }
            ],
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.assertLessEqual(len(body), 384)
        app = create_gateway(
            resolver,
            client_factory=lambda: upstream,
            max_request_bytes=384,
        )

        with TestClient(app) as client:
            response = client.post(
                "/v1/responses",
                content=body,
                headers={"content-type": "application/json"},
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "request_too_large")
        self.assertEqual(
            response.json()["error"]["message"],
            "The request exceeds the 384-byte Gateway limit.",
        )
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
