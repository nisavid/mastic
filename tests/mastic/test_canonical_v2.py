import unittest
from datetime import UTC, datetime, timedelta, timezone

from mastic.domain.canonical import (
    canonical_fingerprint,
    canonical_json_bytes,
    canonical_timestamp,
)


class CanonicalVersionTwoTests(unittest.TestCase):
    def test_jcs_normative_unicode_and_number_vector(self) -> None:
        value = {"€": 1.0, "a": -0.0, "é": "é"}

        self.assertEqual(
            canonical_json_bytes(value),
            '{"a":0,"é":"é","€":1}'.encode(),
        )
        self.assertEqual(
            canonical_fingerprint(value),
            "sha256:052a8c41d49d769806825e6ab1ccd73e19574290b227acd97c6438db2f6e39e0",
        )

    def test_timestamps_use_the_single_canonical_utc_subset(self) -> None:
        eastern = timezone(timedelta(hours=-4))

        self.assertEqual(
            canonical_timestamp(datetime(2026, 7, 20, 18, tzinfo=UTC)),
            "2026-07-20T18:00:00Z",
        )
        self.assertEqual(
            canonical_timestamp(
                datetime(2026, 7, 20, 14, 0, 0, 120000, tzinfo=eastern)
            ),
            "2026-07-20T18:00:00.12Z",
        )

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            canonical_timestamp(datetime(2026, 7, 20, 18))


if __name__ == "__main__":
    unittest.main()
