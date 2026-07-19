import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mastic.infrastructure.application_target_persistence as persistence


class ApplicationTargetPersistenceTests(unittest.TestCase):
    def test_atomic_replace_is_pinned_when_visible_parent_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managed = root / "managed"
            detached = root / "detached"
            outside = root / "outside"
            managed.mkdir()
            outside.mkdir()
            target = managed / "owner.json"
            outside_target = outside / target.name
            outside_target.write_bytes(b"outside")
            original_replace = persistence.os.replace

            def swap_then_replace(source, destination, **kwargs):
                managed.rename(detached)
                managed.symlink_to(outside, target_is_directory=True)
                return original_replace(source, destination, **kwargs)

            with patch.object(persistence.os, "replace", side_effect=swap_then_replace):
                persistence._atomic_replace(target, b"managed")

            self.assertEqual((detached / target.name).read_bytes(), b"managed")
            self.assertEqual(outside_target.read_bytes(), b"outside")

    def test_read_is_pinned_when_visible_parent_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managed = root / "managed"
            detached = root / "detached"
            outside = root / "outside"
            managed.mkdir()
            outside.mkdir()
            target = managed / "owner.json"
            target.write_bytes(b"managed")
            (outside / target.name).write_bytes(b"outside")
            original_open = persistence.os.open

            def swap_then_open(path, flags, *args, **kwargs):
                if path == target.name and kwargs.get("dir_fd") is not None:
                    managed.rename(detached)
                    managed.symlink_to(outside, target_is_directory=True)
                return original_open(path, flags, *args, **kwargs)

            with patch.object(persistence.os, "open", side_effect=swap_then_open):
                payload, existed = persistence._read(target)

            self.assertTrue(existed)
            self.assertEqual(payload, b"managed")


if __name__ == "__main__":
    unittest.main()
