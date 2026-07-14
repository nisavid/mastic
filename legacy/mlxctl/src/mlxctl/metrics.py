"""Persist and aggregate local inference metrics."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Iterator


_SCHEMA_LOCK = threading.Lock()


class RequestOutcome(StrEnum):
    COMPLETED = "completed"
    UPSTREAM_ERROR = "upstream_error"
    CLIENT_DISCONNECT = "client_disconnect"


@dataclass(frozen=True, slots=True)
class RequestMetricEvent:
    server_id: str
    model_alias: str
    run_id: str
    started_at: datetime
    duration_ms: float
    ttft_ms: float | None
    status_code: int | None
    outcome: RequestOutcome
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_tokens: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, RequestOutcome):
            raise TypeError("outcome must be a RequestOutcome")


@dataclass(frozen=True, slots=True)
class ProcessSample:
    server_id: str
    model_alias: str
    run_id: str
    sampled_at: datetime
    rss_bytes: int
    cpu_percent: float


@dataclass(frozen=True, slots=True)
class MetricQuery:
    server_id: str | None = None
    model_alias: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class MetricSummary:
    server_id: str
    model_alias: str
    request_count: int
    success_count: int
    failure_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_tokens: int | None
    average_duration_ms: float | None
    average_ttft_ms: float | None
    peak_rss_bytes: int | None
    average_cpu_percent: float | None


class MetricsEngine:
    """Own durable metric storage, retention, and grouped aggregation."""

    _SCHEMA_VERSION = 1

    def __init__(self, path: str | Path, retention_days: int = 30) -> None:
        if type(retention_days) is not int or retention_days <= 0:
            raise ValueError("retention_days must be a positive integer")
        self._path = Path(path)
        self._retention = timedelta(days=retention_days)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _SCHEMA_LOCK, self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, self._SCHEMA_VERSION):
                raise RuntimeError(f"unsupported metrics schema version {version}")
            if version == 0:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS request_metrics (
                        id INTEGER PRIMARY KEY,
                        server_id TEXT NOT NULL,
                        model_alias TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        duration_ms REAL NOT NULL,
                        ttft_ms REAL,
                        status_code INTEGER,
                        outcome TEXT NOT NULL CHECK (
                            outcome IN ('completed', 'upstream_error', 'client_disconnect')
                        ),
                        prompt_tokens INTEGER,
                        completion_tokens INTEGER,
                        total_tokens INTEGER,
                        cached_tokens INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS request_metrics_query
                        ON request_metrics(server_id, model_alias, started_at);
                    CREATE TABLE IF NOT EXISTS process_samples (
                        id INTEGER PRIMARY KEY,
                        server_id TEXT NOT NULL,
                        model_alias TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        sampled_at REAL NOT NULL,
                        rss_bytes INTEGER NOT NULL,
                        cpu_percent REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS process_samples_query
                        ON process_samples(server_id, model_alias, sampled_at);
                    PRAGMA user_version = 1;
                    COMMIT;
                    """
                )

    def record(self, event: RequestMetricEvent | ProcessSample) -> None:
        with self._connection() as connection:
            if isinstance(event, RequestMetricEvent):
                connection.execute(
                    """
                    INSERT INTO request_metrics (
                        server_id, model_alias, run_id, started_at,
                        duration_ms, ttft_ms, status_code, outcome,
                        prompt_tokens, completion_tokens, total_tokens, cached_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.server_id,
                        event.model_alias,
                        event.run_id,
                        _timestamp(event.started_at),
                        event.duration_ms,
                        event.ttft_ms,
                        event.status_code,
                        event.outcome.value,
                        event.prompt_tokens,
                        event.completion_tokens,
                        event.total_tokens,
                        event.cached_tokens,
                    ),
                )
            elif isinstance(event, ProcessSample):
                connection.execute(
                    """
                    INSERT INTO process_samples (
                        server_id, model_alias, run_id, sampled_at,
                        rss_bytes, cpu_percent
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.server_id,
                        event.model_alias,
                        event.run_id,
                        _timestamp(event.sampled_at),
                        event.rss_bytes,
                        event.cpu_percent,
                    ),
                )
            else:
                raise TypeError("event must be RequestMetricEvent or ProcessSample")

    def query(self, query: MetricQuery) -> tuple[MetricSummary, ...]:
        request_where, request_params = _filters(query, "started_at")
        sample_where, sample_params = _filters(query, "sampled_at")
        sql = f"""
            WITH request_totals AS (
                SELECT server_id, model_alias,
                       COUNT(*) AS request_count,
                       SUM(CASE WHEN
                           outcome = 'completed'
                           AND status_code IS NOT NULL AND status_code < 400
                           THEN 1 ELSE 0 END) AS success_count,
                       COUNT(*) - SUM(CASE WHEN
                           outcome = 'completed'
                           AND status_code IS NOT NULL AND status_code < 400
                           THEN 1 ELSE 0 END) AS failure_count,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(total_tokens) AS total_tokens,
                       SUM(cached_tokens) AS cached_tokens,
                       AVG(duration_ms) AS average_duration_ms,
                       AVG(ttft_ms) AS average_ttft_ms
                FROM request_metrics {request_where}
                GROUP BY server_id, model_alias
            ), sample_totals AS (
                SELECT server_id, model_alias,
                       MAX(rss_bytes) AS peak_rss_bytes,
                       AVG(cpu_percent) AS average_cpu_percent
                FROM process_samples {sample_where}
                GROUP BY server_id, model_alias
            ), metric_keys AS (
                SELECT server_id, model_alias FROM request_totals
                UNION
                SELECT server_id, model_alias FROM sample_totals
            )
            SELECT metric_keys.server_id, metric_keys.model_alias,
                   COALESCE(request_count, 0), COALESCE(success_count, 0),
                   COALESCE(failure_count, 0), prompt_tokens, completion_tokens,
                   total_tokens, cached_tokens, average_duration_ms,
                   average_ttft_ms, peak_rss_bytes, average_cpu_percent
            FROM metric_keys
            LEFT JOIN request_totals USING (server_id, model_alias)
            LEFT JOIN sample_totals USING (server_id, model_alias)
            ORDER BY metric_keys.server_id, metric_keys.model_alias
        """
        with self._connection() as connection:
            rows = connection.execute(sql, (*request_params, *sample_params)).fetchall()
        return tuple(MetricSummary(*row) for row in rows)

    def prune(self, now: datetime) -> None:
        cutoff = _timestamp(now - self._retention)
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM request_metrics WHERE started_at < ?", (cutoff,)
            )
            connection.execute(
                "DELETE FROM process_samples WHERE sampled_at < ?", (cutoff,)
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _timestamp(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("metric timestamps must be timezone-aware")
    return value.astimezone(UTC).timestamp()


def _filters(query: MetricQuery, time_column: str) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if query.server_id is not None:
        clauses.append("server_id = ?")
        params.append(query.server_id)
    if query.model_alias is not None:
        clauses.append("model_alias = ?")
        params.append(query.model_alias)
    if query.start_time is not None:
        clauses.append(f"{time_column} >= ?")
        params.append(_timestamp(query.start_time))
    if query.end_time is not None:
        clauses.append(f"{time_column} < ?")
        params.append(_timestamp(query.end_time))
    return ("WHERE " + " AND ".join(clauses) if clauses else "", params)
