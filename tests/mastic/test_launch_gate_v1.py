from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


_GATE = (
    Path(__file__).parents[2] / "src" / "mastic" / "infrastructure" / "_launch_gate.py"
)


class LaunchGateTests(unittest.TestCase):
    def test_raw_launch_frames_with_embedded_nul_fail_closed(self) -> None:
        invalid_envelopes = (
            {"argv": ["/runtime/bin/optiq", "serve\0unexpected"], "environment": {}},
            {
                "argv": ["/runtime/bin/optiq"],
                "environment": {"PATH": "/usr/bin\0/untrusted"},
            },
            {
                "argv": ["/runtime/bin/optiq"],
                "environment": {"PATH\0UNTRUSTED": "/usr/bin"},
            },
        )
        for envelope in invalid_envelopes:
            with self.subTest(envelope=envelope):
                result = _run_gate(_frame(envelope))

                self.assertEqual(result.returncode, 78)
                self.assertNotIn(b"Traceback", result.stderr)

    def test_malformed_frame_returns_bounded_data_error_without_traceback(self) -> None:
        result = _run_gate(b"\x00\x00\x00\x00\x00\x00\x00\x01{")

        self.assertEqual(result.returncode, 78)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_unreadable_descriptor_returns_bounded_io_error_without_traceback(
        self,
    ) -> None:
        for descriptor in ("999999", "9" * 100):
            with self.subTest(descriptor=descriptor):
                result = subprocess.run(
                    (sys.executable, str(_GATE), descriptor),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

                self.assertEqual(result.returncode, 74)
                self.assertNotIn(b"Traceback", result.stderr)


def _frame(envelope: object) -> bytes:
    payload = json.dumps(envelope, separators=(",", ":")).encode()
    return len(payload).to_bytes(8, "big") + payload


def _run_gate(frame: bytes) -> subprocess.CompletedProcess[bytes]:
    read_descriptor, write_descriptor = os.pipe()
    try:
        process = subprocess.Popen(
            (sys.executable, str(_GATE), str(read_descriptor)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(read_descriptor,),
        )
        os.close(read_descriptor)
        read_descriptor = -1
        os.write(write_descriptor, frame)
        os.close(write_descriptor)
        write_descriptor = -1
        stdout, stderr = process.communicate(timeout=2)
        return subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )
    finally:
        if read_descriptor >= 0:
            os.close(read_descriptor)
        if write_descriptor >= 0:
            os.close(write_descriptor)


if __name__ == "__main__":
    unittest.main()
