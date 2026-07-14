import unittest

from mlxctl.dashboard import DashboardRow, DashboardSnapshot, render_plain


class DashboardRenderingTests(unittest.TestCase):
    def test_full_snapshot_renders_every_lifecycle_and_null_metrics(self) -> None:
        rows = tuple(
            DashboardRow(
                server_id=f"server-{lifecycle}",
                model_alias="tiny" if lifecycle == "ready" else None,
                lifecycle=lifecycle,
                client_endpoint="http://127.0.0.1:8080"
                if lifecycle == "ready"
                else None,
                pid=4321 if lifecycle == "ready" else None,
                advertised_models=("repo/tiny", "repo/other")
                if lifecycle == "ready"
                else (),
                request_count=10 if lifecycle == "ready" else None,
                success_count=8 if lifecycle == "ready" else None,
                failure_count=2 if lifecycle == "ready" else None,
                total_tokens=42 if lifecycle == "ready" else None,
                average_duration_ms=12.5 if lifecycle == "ready" else None,
                average_ttft_ms=3.25 if lifecycle == "ready" else None,
                peak_rss_bytes=2048 if lifecycle == "ready" else None,
                average_cpu_percent=25.0 if lifecycle == "ready" else None,
            )
            for lifecycle in (
                "stopped",
                "starting",
                "ready",
                "unhealthy",
                "stopping",
                "failed",
            )
        )

        rendered = render_plain(DashboardSnapshot(rows=rows, selected_index=2), 120)

        self.assertEqual(
            rendered,
            "\n".join(
                (
                    "MLX server dashboard",
                    "  server-stopped / - [stopped]",
                    "    endpoint - | PID - | models -",
                    "    requests - | success - | failure - | tokens - | latency - | TTFT - | peak RSS - | CPU -",
                    "  server-starting / - [starting]",
                    "    endpoint - | PID - | models -",
                    "    requests - | success - | failure - | tokens - | latency - | TTFT - | peak RSS - | CPU -",
                    "> server-ready / tiny [ready]",
                    "    endpoint http://127.0.0.1:8080 | PID 4321 | models repo/tiny, repo/other",
                    "    requests 10 | success 8 | failure 2 | tokens 42 | latency 12.5 ms | TTFT 3.2 ms | peak RSS 2.0 KiB | CPU 25.0%",
                    "  server-unhealthy / - [unhealthy]",
                    "    endpoint - | PID - | models -",
                    "    requests - | success - | failure - | tokens - | latency - | TTFT - | peak RSS - | CPU -",
                    "  server-stopping / - [stopping]",
                    "    endpoint - | PID - | models -",
                    "    requests - | success - | failure - | tokens - | latency - | TTFT - | peak RSS - | CPU -",
                    "  server-failed / - [failed]",
                    "    endpoint - | PID - | models -",
                    "    requests - | success - | failure - | tokens - | latency - | TTFT - | peak RSS - | CPU -",
                )
            ),
        )

    def test_empty_and_configuration_error_snapshots_are_explicit(self) -> None:
        self.assertEqual(
            render_plain(DashboardSnapshot(), 120),
            "MLX server dashboard\nNo configured or running servers.",
        )
        self.assertEqual(
            render_plain(
                DashboardSnapshot(
                    config_error="models table is invalid",
                    control_error="metrics unavailable",
                ),
                120,
            ),
            "\n".join(
                (
                    "MLX server dashboard",
                    "Configuration error: models table is invalid",
                    "Control error: metrics unavailable",
                    "No configured or running servers.",
                )
            ),
        )

    def test_narrow_plain_snapshot_remains_readable(self) -> None:
        snapshot = DashboardSnapshot(
            rows=(
                DashboardRow(
                    "chat",
                    "tiny",
                    "ready",
                    client_endpoint="http://127.0.0.1:8080",
                    pid=4321,
                    advertised_models=("repo/tiny",),
                    request_count=10,
                    success_count=8,
                    failure_count=2,
                    total_tokens=42,
                    average_duration_ms=12.5,
                    average_ttft_ms=3.25,
                    peak_rss_bytes=2048,
                    average_cpu_percent=25.0,
                ),
            )
        )

        self.assertEqual(
            render_plain(snapshot, 40),
            "\n".join(
                (
                    "MLX server dashboard",
                    "> chat/tiny [ready]",
                    "  http://127.0.0.1:8080 | PID 4321",
                    "  models repo/tiny",
                    "  10 req | 8 ok | 2 failed | 42 tok",
                    "  latency 12.5 ms | TTFT 3.2 ms",
                    "  peak RSS 2.0 KiB | CPU 25.0%",
                )
            ),
        )


if __name__ == "__main__":
    unittest.main()
