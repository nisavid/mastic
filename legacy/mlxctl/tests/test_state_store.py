import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mlxctl.infrastructure.state_store import (
    OperationalStateStore,
    SensitiveContentError,
)


class OperationalStateStoreTests(unittest.TestCase):
    def test_persists_operations_progress_events_snapshots_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mlxctl" / "state.sqlite3"
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

            self.assertEqual(store.snapshot("service", "chat"), second)
            self.assertEqual(store.snapshot("service", "chat", version=1), first)
            self.assertEqual(store.snapshots("service"), (first, second))

            with self.assertRaisesRegex(ValueError, "version 1 is immutable"):
                store.put_snapshot(
                    {"kind": "service", "id": "chat", "state": "failed", "version": 1}
                )


if __name__ == "__main__":
    unittest.main()
