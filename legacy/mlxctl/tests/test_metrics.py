import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from mlxctl.metrics import (
    MetricQuery,
    MetricsEngine,
    ProcessSample,
    RequestMetricEvent,
    RequestOutcome,
)


class MetricsEngineTests(unittest.TestCase):
    def test_aggregates_request_metrics_from_known_literals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = MetricsEngine(Path(directory) / "metrics.sqlite3")
            engine.record(
                RequestMetricEvent(
                    server_id="chat",
                    model_alias="tiny",
                    run_id="run-1",
                    started_at=datetime(2026, 7, 13, 12, tzinfo=UTC),
                    duration_ms=100,
                    ttft_ms=20,
                    status_code=200,
                    outcome=RequestOutcome.COMPLETED,
                    prompt_tokens=10,
                    completion_tokens=4,
                    total_tokens=14,
                    cached_tokens=3,
                )
            )
            engine.record(
                RequestMetricEvent(
                    server_id="chat",
                    model_alias="tiny",
                    run_id="run-1",
                    started_at=datetime(2026, 7, 13, 12, 1, tzinfo=UTC),
                    duration_ms=300,
                    ttft_ms=40,
                    status_code=503,
                    outcome=RequestOutcome.COMPLETED,
                    prompt_tokens=20,
                    completion_tokens=6,
                    total_tokens=26,
                    cached_tokens=5,
                )
            )

            summaries = engine.query(MetricQuery())

            self.assertEqual(len(summaries), 1)
            summary = summaries[0]
            self.assertEqual(summary.server_id, "chat")
            self.assertEqual(summary.model_alias, "tiny")
            self.assertEqual(summary.request_count, 2)
            self.assertEqual(summary.success_count, 1)
            self.assertEqual(summary.failure_count, 1)
            self.assertEqual(summary.prompt_tokens, 30)
            self.assertEqual(summary.completion_tokens, 10)
            self.assertEqual(summary.total_tokens, 40)
            self.assertEqual(summary.cached_tokens, 8)
            self.assertEqual(summary.average_duration_ms, 200)
            self.assertEqual(summary.average_ttft_ms, 30)

    def test_preserves_null_usage_and_aggregates_process_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = MetricsEngine(Path(directory) / "metrics.sqlite3")
            engine.record(self._request())
            engine.record(
                ProcessSample(
                    "chat",
                    "tiny",
                    "run-1",
                    datetime(2026, 7, 13, 12, tzinfo=UTC),
                    1000,
                    25,
                )
            )
            engine.record(
                ProcessSample(
                    "chat",
                    "tiny",
                    "run-1",
                    datetime(2026, 7, 13, 12, 1, tzinfo=UTC),
                    3000,
                    75,
                )
            )

            summary = engine.query(MetricQuery())[0]

            self.assertIsNone(summary.prompt_tokens)
            self.assertIsNone(summary.completion_tokens)
            self.assertIsNone(summary.total_tokens)
            self.assertIsNone(summary.cached_tokens)
            self.assertIsNone(summary.average_ttft_ms)
            self.assertEqual(summary.peak_rss_bytes, 3000)
            self.assertEqual(summary.average_cpu_percent, 50)

    def test_reopens_durable_data_and_filters_by_identity_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.sqlite3"
            MetricsEngine(path).record(self._request())

            summaries = MetricsEngine(path).query(
                MetricQuery(
                    server_id="chat",
                    model_alias="tiny",
                    start_time=datetime(2026, 7, 13, 11, tzinfo=UTC),
                    end_time=datetime(2026, 7, 13, 13, tzinfo=UTC),
                )
            )

            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].request_count, 1)
            self.assertEqual(
                MetricsEngine(path).query(MetricQuery(server_id="other")), ()
            )

    def test_reader_and_writer_can_use_the_engine_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = MetricsEngine(Path(directory) / "metrics.sqlite3")

            def write(index: int) -> None:
                engine.record(replace(self._request(), run_id=f"run-{index}"))

            with ThreadPoolExecutor(max_workers=8) as pool:
                writes = [pool.submit(write, index) for index in range(40)]
                reads = [pool.submit(engine.query, MetricQuery()) for _ in range(20)]
                for future in (*writes, *reads):
                    future.result()

            self.assertEqual(engine.query(MetricQuery())[0].request_count, 40)

    def test_two_engines_can_initialize_one_new_database_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.sqlite3"
            ready = threading.Barrier(2)

            def construct() -> MetricsEngine:
                ready.wait()
                return MetricsEngine(path)

            with ThreadPoolExecutor(max_workers=2) as pool:
                engines = tuple(pool.map(lambda _index: construct(), range(2)))

            self.assertEqual(len(engines), 2)
            for index, engine in enumerate(engines):
                engine.record(replace(self._request(), run_id=f"run-{index}"))
            self.assertEqual(engines[0].query(MetricQuery())[0].request_count, 2)

    def test_prune_deletes_only_rows_older_than_retention_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = MetricsEngine(
                Path(directory) / "metrics.sqlite3", retention_days=1
            )
            for hour in (11, 12, 13):
                event = self._request()
                engine.record(
                    RequestMetricEvent(
                        event.server_id,
                        event.model_alias,
                        event.run_id,
                        datetime(2026, 7, 12, hour, tzinfo=UTC),
                        event.duration_ms,
                        event.ttft_ms,
                        event.status_code,
                        event.outcome,
                        event.prompt_tokens,
                        event.completion_tokens,
                        event.total_tokens,
                        event.cached_tokens,
                    )
                )

            engine.prune(datetime(2026, 7, 13, 12, tzinfo=UTC))

            self.assertEqual(engine.query(MetricQuery())[0].request_count, 2)

    def test_classifies_every_closed_request_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = MetricsEngine(Path(directory) / "metrics.sqlite3")
            cases = (
                (RequestOutcome.COMPLETED, 200),
                (RequestOutcome.COMPLETED, 503),
                (RequestOutcome.UPSTREAM_ERROR, 502),
                (RequestOutcome.CLIENT_DISCONNECT, 200),
            )
            for outcome, status_code in cases:
                engine.record(
                    replace(self._request(), outcome=outcome, status_code=status_code)
                )

            summary = engine.query(MetricQuery())[0]

            self.assertEqual(summary.request_count, 4)
            self.assertEqual(summary.success_count, 1)
            self.assertEqual(summary.failure_count, 3)

    def test_rejects_an_arbitrary_request_outcome(self) -> None:
        with self.assertRaisesRegex(TypeError, "outcome must be a RequestOutcome"):
            replace(self._request(), outcome="completed")

    @staticmethod
    def _request() -> RequestMetricEvent:
        return RequestMetricEvent(
            server_id="chat",
            model_alias="tiny",
            run_id="run-1",
            started_at=datetime(2026, 7, 13, 12, tzinfo=UTC),
            duration_ms=100,
            ttft_ms=None,
            status_code=200,
            outcome=RequestOutcome.COMPLETED,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            cached_tokens=None,
        )


if __name__ == "__main__":
    unittest.main()
