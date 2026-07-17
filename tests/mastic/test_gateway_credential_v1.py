from __future__ import annotations

import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from mastic.infrastructure.gateway_credential import GatewayCredential


class GatewayCredentialTests(unittest.TestCase):
    def test_generates_one_private_persistent_token_and_authenticates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path = root / "gateway.token"
            credential = GatewayCredential(path)

            first = credential.load_or_create()
            second = credential.load_or_create()

            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
            self.assertFalse(credential.authenticate(None))
            self.assertFalse(credential.authenticate("Bearer wrong"))
            self.assertTrue(credential.authenticate(f"Bearer {first}"))

    def test_rejects_symlink_non_private_and_non_regular_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            target = root / "target"
            target.write_text("keep", encoding="utf-8")
            link = root / "gateway.token"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                GatewayCredential(link).load_or_create()
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

            link.unlink()
            link.mkdir()
            with self.assertRaisesRegex(PermissionError, "regular file"):
                GatewayCredential(link).load_or_create()

            link.rmdir()
            link.write_text("a" * 43 + "\n", encoding="utf-8")
            link.chmod(0o644)
            with self.assertRaisesRegex(PermissionError, "mode 0600"):
                GatewayCredential(link).load_or_create()

    def test_rejects_unsafe_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o755)
            with self.assertRaisesRegex(PermissionError, "mode 0700"):
                GatewayCredential(root / "gateway.token").load_or_create()

    def test_concurrent_creation_converges_on_one_complete_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.token"
            barrier = threading.Barrier(8)
            tokens: list[str] = []

            def create() -> None:
                barrier.wait()
                tokens.append(GatewayCredential(path).load_or_create())

            threads = [threading.Thread(target=create) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)

            self.assertEqual(len(tokens), 8)
            self.assertEqual(len(set(tokens)), 1)
            self.assertEqual(tokens[0], GatewayCredential(path).load_or_create())


if __name__ == "__main__":
    unittest.main()
