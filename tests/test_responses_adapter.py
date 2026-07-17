from __future__ import annotations

import copy
import json
import unittest

from mastic.infrastructure.responses_adapter import (
    CodexNamespaceAdapter,
    NamespaceState,
    TransformationError,
    flatten_request,
    transform_response,
)


def _remember_response(
    adapter: CodexNamespaceAdapter,
    data: object,
    reconstruction: dict[str, tuple[str, str]],
) -> None:
    raw = json.dumps(data, separators=(",", ":")).encode()
    adapter.transform_response_body(data, raw, reconstruction, max_bytes=4096)


class RequestTransformTests(unittest.TestCase):
    def test_tools_history_and_passthrough_items_are_transformed_losslessly(
        self,
    ) -> None:
        request = {
            "model": "local",
            "tools": [
                {
                    "type": "namespace",
                    "name": "math",
                    "description": "Math tools",
                    "tools": [
                        {
                            "type": "function",
                            "name": "add",
                            "description": "Add",
                            "parameters": {},
                        }
                    ],
                },
                {"type": "function", "name": "plain", "parameters": {}},
            ],
            "input": [
                {
                    "type": "function_call",
                    "namespace": "math",
                    "name": "add",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "one", "output": "5"},
                {
                    "type": "agent_message",
                    "author": "a",
                    "recipient": "b",
                    "content": [],
                },
                {"type": "encrypted_content", "encrypted_content": "opaque"},
            ],
        }
        original = copy.deepcopy(request)

        transformed, reconstruction = flatten_request(request)

        self.assertEqual(original, request)
        self.assertEqual(reconstruction, {"math__add": ("math", "add")})
        self.assertEqual(transformed["tools"][0]["name"], "math__add")
        self.assertEqual(
            transformed["tools"][0]["description"], "[math] Math tools\n\nAdd"
        )
        self.assertEqual(transformed["tools"][1], request["tools"][1])
        self.assertEqual(transformed["input"][0]["name"], "math__add")
        self.assertNotIn("namespace", transformed["input"][0])
        self.assertEqual(transformed["input"][1:], request["input"][1:])

    def test_ordinary_double_underscore_name_is_not_treated_as_namespace(self) -> None:
        transformed, reconstruction = flatten_request(
            {
                "tools": [
                    {"type": "function", "name": "ordinary__function", "parameters": {}}
                ]
            }
        )
        response = {"output": [{"type": "function_call", "name": "ordinary__function"}]}

        self.assertEqual(reconstruction, {})
        self.assertEqual(transformed["tools"][0]["name"], "ordinary__function")
        self.assertEqual(transform_response(response, reconstruction), response)

    def test_collisions_and_unsupported_namespace_shapes_fail_closed(self) -> None:
        cases = [
            {
                "tools": [
                    {"type": "function", "name": "math__add"},
                    {
                        "type": "namespace",
                        "name": "math",
                        "tools": [{"type": "function", "name": "add"}],
                    },
                ]
            },
            {
                "tools": [
                    {
                        "type": "namespace",
                        "name": "a__b",
                        "tools": [{"type": "function", "name": "c"}],
                    },
                    {
                        "type": "namespace",
                        "name": "a",
                        "tools": [{"type": "function", "name": "b__c"}],
                    },
                ]
            },
            {
                "tools": [
                    {
                        "type": "namespace",
                        "name": "math",
                        "tools": [{"type": "custom", "name": "opaque"}],
                    }
                ]
            },
            {
                "input": [
                    {
                        "type": "custom_tool_call",
                        "namespace": "math",
                        "name": "opaque",
                    }
                ]
            },
        ]
        for request in cases:
            with self.subTest(request=request), self.assertRaises(TransformationError):
                flatten_request(request)

    def test_missing_or_malformed_names_fail_closed(self) -> None:
        cases = [
            {"tools": [{"type": "function"}]},
            {"tools": [{"type": "custom", "name": ""}]},
            {
                "tools": [
                    {"type": "namespace", "name": "", "tools": []},
                ]
            },
            {"input": [{"type": "function_call", "namespace": "math", "name": ""}]},
        ]
        for request in cases:
            with self.subTest(request=request), self.assertRaises(TransformationError):
                flatten_request(request)


class ResponseTransformTests(unittest.TestCase):
    def test_response_transform_reconstructs_only_mapped_function_calls(self) -> None:
        response = {
            "id": "response-one",
            "output": [
                {"type": "function_call", "name": "math__add", "arguments": "{}"},
                {"type": "custom_tool_call", "name": "math__add", "input": "x"},
                {"type": "function_call", "name": "unmapped__call", "arguments": "{}"},
            ],
        }
        original = copy.deepcopy(response)

        transformed = transform_response(response, {"math__add": ("math", "add")})

        self.assertEqual(response, original)
        self.assertEqual(
            transformed["output"][0],
            {
                "type": "function_call",
                "namespace": "math",
                "name": "add",
                "arguments": "{}",
            },
        )
        self.assertEqual(transformed["output"][1:], response["output"][1:])

    def test_sse_transform_preserves_unchanged_bytes_and_reconstructs_changed_frame(
        self,
    ) -> None:
        adapter = CodexNamespaceAdapter()
        changed = (
            b"event: response.output_item.added\r\n"
            b'data: {"type":"response.output_item.added",\r\n'
            b'data: "item":{"type":"function_call","name":"math__add"}}\r\n\r\n'
        )
        unchanged = (
            b"event: response.output_text.delta\n"
            b'data:  {"type":"response.output_text.delta","delta":"hello"}\n\n'
        )

        transformed = adapter.transform_sse_frame(
            changed, {"math__add": ("math", "add")}
        )

        self.assertIn(b'"name":"add","namespace":"math"', transformed)
        self.assertTrue(transformed.endswith(b"\r\n\r\n"))
        self.assertEqual(
            adapter.transform_sse_frame(unchanged, {"math__add": ("math", "add")}),
            unchanged,
        )


class ContinuationStateTests(unittest.TestCase):
    def test_byte_budget_evicts_least_recently_used_response_mapping(self) -> None:
        adapter = CodexNamespaceAdapter(capacity=10, byte_capacity=8)
        _remember_response(adapter, {"id": "a"}, {"a": ("n", "a")})
        _remember_response(adapter, {"id": "b"}, {"b": ("n", "b")})

        adapter.transform_request({"previous_response_id": "a"})
        _remember_response(adapter, {"id": "c"}, {"c": ("n", "c")})

        self.assertEqual(
            adapter.transform_request({"previous_response_id": "a"})[1],
            {"a": ("n", "a")},
        )
        self.assertEqual(
            adapter.transform_request({"previous_response_id": "c"})[1],
            {"c": ("n", "c")},
        )
        with self.assertRaisesRegex(TransformationError, "state is unavailable"):
            adapter.transform_request({"previous_response_id": "b"})

    def test_oversized_mapping_is_not_retained_for_continuation(self) -> None:
        adapter = CodexNamespaceAdapter(capacity=10, byte_capacity=5)

        _remember_response(adapter, {"id": "a"}, {"tool": ("ns", "call")})

        with self.assertRaisesRegex(TransformationError, "state is unavailable"):
            adapter.transform_request({"previous_response_id": "a"})

    def test_oversized_response_id_is_not_retained_for_continuation(self) -> None:
        adapter = CodexNamespaceAdapter(capacity=10, byte_capacity=8)

        _remember_response(adapter, {"id": "response-id-too-large"}, {})

        with self.assertRaisesRegex(TransformationError, "state is unavailable"):
            adapter.transform_request({"previous_response_id": "response-id-too-large"})

    def test_replacing_response_mapping_updates_its_byte_accounting(self) -> None:
        adapter = CodexNamespaceAdapter(capacity=10, byte_capacity=10)
        _remember_response(adapter, {"id": "same"}, {"a": ("n", "a")})
        replacement = {"bb": ("nn", "bb")}

        _remember_response(adapter, {"id": "same"}, replacement)

        self.assertEqual(
            adapter.transform_request({"previous_response_id": "same"})[1], replacement
        )

    def test_previous_response_id_inherits_exact_mapping(self) -> None:
        adapter = CodexNamespaceAdapter()
        first_request, first_mapping = adapter.transform_request(
            {
                "tools": [
                    {
                        "type": "namespace",
                        "name": "math",
                        "tools": [{"type": "function", "name": "add"}],
                    }
                ],
                "input": [],
            }
        )
        self.assertEqual(first_request["tools"][0]["name"], "math__add")
        _remember_response(adapter, {"id": "response-one", "output": []}, first_mapping)

        continued, mapping = adapter.transform_request(
            {"previous_response_id": "response-one", "input": []}
        )

        self.assertEqual(continued["previous_response_id"], "response-one")
        self.assertEqual(mapping, {"math__add": ("math", "add")})

    def test_known_empty_mapping_is_distinct_from_unknown_continuation(self) -> None:
        adapter = CodexNamespaceAdapter()
        request, mapping = adapter.transform_request({"tools": [], "input": []})
        _remember_response(adapter, {"id": "ordinary-one", "output": []}, mapping)

        continued, inherited = adapter.transform_request(
            {"previous_response_id": "ordinary-one", "input": []}
        )

        self.assertEqual(request, {"tools": [], "input": []})
        self.assertEqual(continued["previous_response_id"], "ordinary-one")
        self.assertEqual(inherited, {})
        with self.assertRaisesRegex(TransformationError, "state is unavailable"):
            adapter.transform_request(
                {"previous_response_id": "unknown-response", "input": []}
            )

    def test_inherited_mapping_rejects_new_ordinary_name_collision(self) -> None:
        adapter = CodexNamespaceAdapter()
        _request, mapping = adapter.transform_request(
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
        _remember_response(adapter, {"id": "mapped-one", "output": []}, mapping)

        with self.assertRaisesRegex(TransformationError, "collides"):
            adapter.transform_request(
                {
                    "previous_response_id": "mapped-one",
                    "tools": [{"type": "function", "name": "math__add"}],
                }
            )

    def test_state_is_bounded_and_uses_least_recently_used_eviction(self) -> None:
        state = NamespaceState(capacity=2)
        state.remember("one", {"a": ("n", "a")})
        state.remember("two", {"b": ("n", "b")})
        self.assertEqual(state.get("one"), {"a": ("n", "a")})
        state.remember("three", {"c": ("n", "c")})

        self.assertIsNone(state.get("two"))
        self.assertEqual(state.get("one"), {"a": ("n", "a")})
        self.assertEqual(state.get("three"), {"c": ("n", "c")})


if __name__ == "__main__":
    unittest.main()
