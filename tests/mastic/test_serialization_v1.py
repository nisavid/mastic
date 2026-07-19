import json
import unittest
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from mastic.application.serialization import to_plain_data


class _State(StrEnum):
    READY = "ready"


@dataclass(frozen=True)
class _Record:
    path: Path
    state: _State
    labels: frozenset[str]


class PlainDataSerializationTests(unittest.TestCase):
    def test_nested_application_values_use_one_deterministic_shape(self) -> None:
        value = {
            "record": _Record(
                Path("relative/path"), _State.READY, frozenset({"b", "a"})
            ),
            "sequence": (1, 2),
        }

        plain = to_plain_data(value)

        self.assertEqual(
            plain,
            {
                "record": {
                    "path": "relative/path",
                    "state": "ready",
                    "labels": ["a", "b"],
                },
                "sequence": [1, 2],
            },
        )
        self.assertEqual(json.loads(json.dumps(plain)), plain)

    def test_unwrapped_values_are_recursively_normalized(self) -> None:
        class Wrapped:
            def unwrap(self):
                return {"state": _State.READY, "path": Path("value")}

        self.assertEqual(to_plain_data(Wrapped()), {"state": "ready", "path": "value"})
