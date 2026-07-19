from __future__ import annotations

import json
import unittest

import httpx

from mastic.infrastructure.responses_transport import (
    RequestTransformationTooLarge,
    ResponsesTransport,
)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class ResponsesTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_namespace_expansion_is_bounded_before_the_request_leaves_transport(
        self,
    ) -> None:
        transport = ResponsesTransport(max_request_bytes=384, max_response_bytes=128)
        payload = {
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
            ]
        }
        self.assertLessEqual(
            len(json.dumps(payload, separators=(",", ":")).encode()), 384
        )

        with self.assertRaises(RequestTransformationTooLarge):
            transport.transform_request(payload)

    async def test_json_response_is_reconstructed_within_the_response_budget(
        self,
    ) -> None:
        transport = ResponsesTransport(max_request_bytes=1024, max_response_bytes=256)
        _request, reconstruction = transport.transform_request(
            {
                "tools": [
                    {
                        "type": "namespace",
                        "name": "math",
                        "tools": [{"type": "function", "name": "add"}],
                    }
                ]
            }
        )
        upstream = httpx.Response(
            200,
            stream=ChunkStream(
                [
                    b'{"id":"response-1","output":[',
                    b'{"type":"function_call","name":"math__add"}]}',
                ]
            ),
        )

        body = await transport.adapt_response_body(
            upstream,
            reconstruction,
            content_type="Application/JSON; Charset=UTF-8",
        )

        self.assertEqual(
            body,
            b'{"id":"response-1","output":[{"type":"function_call","name":"add","namespace":"math"}]}',
        )

    def test_request_budget_applies_to_the_exact_serialized_transformation(
        self,
    ) -> None:
        transport = ResponsesTransport(max_request_bytes=270, max_response_bytes=128)
        payload = {
            "model": "coding",
            "input": [],
            "tools": [
                {
                    "type": "namespace",
                    "name": "math",
                    "description": "d" * 60,
                    "tools": [
                        {"type": "function", "name": "f0", "description": "x"},
                        {"type": "function", "name": "f1", "description": "x"},
                    ],
                }
            ],
        }
        self.assertEqual(len(json.dumps(payload, separators=(",", ":")).encode()), 261)

        with self.assertRaises(RequestTransformationTooLarge):
            transport.transform_request(payload)

    async def test_sse_response_reconstructs_namespaces_and_stops_at_terminal(
        self,
    ) -> None:
        transport = ResponsesTransport(max_request_bytes=1024, max_response_bytes=512)
        _request, reconstruction = transport.transform_request(
            {
                "tools": [
                    {
                        "type": "namespace",
                        "name": "math",
                        "tools": [{"type": "function", "name": "add"}],
                    }
                ]
            }
        )
        added = (
            b"event: response.output_item.added\n"
            b'data: {"type":"response.output_item.added","item":{"type":"function_call","name":"math__add"}}\n\n'
        )
        terminal = (
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"response-1"}}\n\n'
        )
        upstream = httpx.Response(
            200,
            stream=ChunkStream([added + terminal + b"data: must-not-forward\n\n"]),
        )

        stream = await transport.adapt_sse_response(upstream, reconstruction)
        body = b"".join([chunk async for chunk in stream])

        frames = body.split(b"\n\n")
        first_payload = json.loads(frames[0].split(b"data: ", 1)[1])
        self.assertEqual(first_payload["item"]["namespace"], "math")
        self.assertEqual(first_payload["item"]["name"], "add")
        self.assertIn(b"event: response.completed", frames[1])
        self.assertNotIn(b"must-not-forward", body)


if __name__ == "__main__":
    unittest.main()
