import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mastic.infrastructure.state_store import (
    OperationalStateStore,
    SensitiveContentError,
)


class OperationalStateStoreTests(unittest.TestCase):
    def test_rejects_a_state_directory_not_owned_by_the_current_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "mastic.infrastructure.state_store.os.getuid",
                return_value=Path(directory).stat().st_uid + 1,
            ):
                with self.assertRaises(PermissionError):
                    OperationalStateStore(Path(directory) / "state.sqlite3")

    def test_rejects_symlinked_or_non_regular_database_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.write_text("preserve", encoding="utf-8")
            database = root / "state.sqlite3"
            database.symlink_to(outside)

            with self.assertRaises(OSError):
                OperationalStateStore(database)

            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_rejects_known_credential_fields_at_any_depth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = OperationalStateStore(Path(directory) / "state.sqlite3")

            for key in (
                "api_key",
                "api-key",
                "apiKey",
                "api_token",
                "authorization",
                "password",
                "access_token",
            ):
                with self.subTest(key=key):
                    with self.assertRaisesRegex(
                        SensitiveContentError, "cannot persist credential material"
                    ):
                        store.put_operation(
                            {"id": f"op-{key}", "details": {key: "secret"}}
                        )

    def test_persists_operations_progress_events_snapshots_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mastic" / "state.sqlite3"
            store = OperationalStateStore(path)
            store.put_operation(
                {"id": "op-1", "kind": "model.install", "status": "running"}
            )
            progress = store.append_progress(
                "op-1", {"completed": 2, "total": 10, "unit": "files"}
            )
            event = store.append_event(
                {"operation_id": "op-1", "kind": "checkpoint", "label": "weights"}
            )
            store.put_snapshot(
                {"kind": "service", "id": "code", "state": "ready", "version": 3}
            )
            metric = store.record_metric(
                {"kind": "request", "service": "code", "duration_ms": 125.0}
            )

            reopened = OperationalStateStore(path)

            self.assertEqual(
                reopened.operation("op-1"),
                {"id": "op-1", "kind": "model.install", "status": "running"},
            )
            self.assertEqual(reopened.progress("op-1"), (progress,))
            self.assertEqual(reopened.events("op-1"), (progress, event))
            self.assertEqual(
                reopened.snapshot("service", "code"),
                {"id": "code", "kind": "service", "state": "ready", "version": 3},
            )
            self.assertEqual(reopened.metrics("request"), (metric,))
            self.assertEqual(
                tuple(metric), ("duration_ms", "kind", "sequence", "service")
            )
            self.assertEqual(
                reopened.metadata(), {"journal_mode": "wal", "schema_version": 1}
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_concurrent_stores_initialize_and_write_without_losing_records(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            ready = threading.Barrier(8)

            def write(index: int) -> None:
                ready.wait()
                store = OperationalStateStore(path)
                store.put_operation(
                    {"id": f"op-{index:02}", "kind": "probe", "status": "done"}
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                tuple(pool.map(write, range(8)))

            self.assertEqual(
                tuple(
                    operation["id"]
                    for operation in OperationalStateStore(path).operations()
                ),
                tuple(f"op-{index:02}" for index in range(8)),
            )

    def test_rejects_prompt_or_response_content_at_any_depth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = OperationalStateStore(Path(directory) / "state.sqlite3")

            with self.assertRaisesRegex(
                SensitiveContentError,
                "cannot persist inference content at details.prompt",
            ):
                store.put_operation(
                    {"id": "op-secret", "details": {"prompt": "do not store me"}}
                )

            self.assertIsNone(store.operation("op-secret"))

    def test_rejects_unapproved_raw_inference_content_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = OperationalStateStore(Path(directory) / "state.sqlite3")

            for key in (
                "body",
                "content",
                "input",
                "message",
                "output",
                "query",
                "text",
                "prompt-text",
                "promptText",
                "requestBody",
            ):
                with self.subTest(key=key):
                    with self.assertRaisesRegex(
                        SensitiveContentError, "cannot persist inference content"
                    ):
                        store.put_operation(
                            {"id": f"op-{key}", "details": {key: "raw content"}}
                        )

    def test_bounds_metric_retention_and_paginates_sequence_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = OperationalStateStore(
                Path(directory) / "state.sqlite3", max_metrics=3
            )
            store.put_operation({"id": "op-1"})
            for index in range(5):
                store.append_event(
                    {"operation_id": "op-1", "kind": "event", "index": index}
                )
                store.record_metric({"kind": "request", "index": index})

            self.assertEqual(
                [metric["sequence"] for metric in store.metrics()], [3, 4, 5]
            )
            self.assertEqual(
                [
                    event["sequence"]
                    for event in store.events(after_sequence=1, limit=2)
                ],
                [2, 3],
            )
            self.assertEqual(
                [
                    metric["sequence"]
                    for metric in store.metrics(after_sequence=3, limit=1)
                ],
                [4],
            )

    def test_preserves_versioned_snapshots_and_returns_the_latest_by_default(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = OperationalStateStore(Path(directory) / "state.sqlite3")
            first = store.put_snapshot(
                {"kind": "service", "id": "chat", "state": "starting", "version": 1}
            )
            second = store.put_snapshot(
                {"kind": "service", "id": "chat", "state": "ready", "version": 2}
            )
            other = store.put_snapshot(
                {"kind": "service", "id": "alpha", "state": "ready", "version": 1}
            )

            self.assertEqual(store.snapshot("service", "chat"), second)
            self.assertEqual(store.snapshot("service", "chat", version=1), first)
            self.assertEqual(store.snapshots("service"), (other, second))

            with self.assertRaisesRegex(ValueError, "version 1 is immutable"):
                store.put_snapshot(
                    {"kind": "service", "id": "chat", "state": "failed", "version": 1}
                )

    def test_progress_filters_by_kind_before_applying_its_page_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = OperationalStateStore(Path(directory) / "state.sqlite3")
            store.put_operation({"id": "op-1"})
            for index in range(1000):
                store.append_event(
                    {"operation_id": "op-1", "kind": "checkpoint", "index": index}
                )
            progress = store.append_progress("op-1", {"completed": 1, "total": 1})

            self.assertEqual(store.progress("op-1"), (progress,))


if __name__ == "__main__":
    unittest.main()
