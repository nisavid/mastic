import stat
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mastic.infrastructure.config_store import ConfigChange, ConfigStore


class ConfigStoreTests(unittest.TestCase):
    def test_rejects_symlinked_config_history_lock_and_journal_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.write_text("preserve", encoding="utf-8")

            config = root / "config.toml"
            config.symlink_to(outside)
            with self.assertRaises(OSError):
                ConfigStore(config, lambda data: data)
            config.unlink()

            history = root / ".config.toml.history"
            shutil.rmtree(history)
            history.symlink_to(root, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                ConfigStore(config, lambda data: data)
            history.unlink()

            store = ConfigStore(config, lambda data: data)
            lock = root / "config.toml.lock"
            lock.symlink_to(outside)
            with self.assertRaises(OSError):
                store.import_text("schema_version = 1\n")
            lock.unlink()

            journal = root / ".config.toml.journal.jsonl"
            journal.symlink_to(outside)
            with self.assertRaises(OSError):
                store.import_text("schema_version = 1\n")

            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_rejects_a_private_directory_not_owned_by_the_current_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "mastic.infrastructure.config_store.os.getuid",
                return_value=Path(directory).stat().st_uid + 1,
            ):
                with self.assertRaises(PermissionError):
                    ConfigStore(Path(directory) / "config.toml", lambda data: data)

    def test_exists_distinguishes_uninitialized_from_saved_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.toml", lambda data: data)

            self.assertFalse(store.exists)
            store.import_text("schema_version = 1\n")
            self.assertTrue(store.exists)

    def test_round_trips_comments_and_returns_validated_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mastic" / "config.toml"
            store = ConfigStore(path, lambda data: int(data["schema_version"]))

            saved = store.import_text(
                "# operator note\nschema_version = 1\n\n[gateway]\nport = 8766\n"
            )
            saved.document["gateway"]["port"] = 9000
            loaded = store.save(saved.document)

            self.assertEqual(loaded.value, 1)
            self.assertIn("# operator note", store.export_text())
            self.assertIn("port = 9000", store.export_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_failed_validation_does_not_replace_the_current_document(self) -> None:
        def validate(data: object) -> int:
            version = int(data["schema_version"])  # type: ignore[index]
            if version != 1:
                raise ValueError("unsupported schema")
            return version

        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.toml", validate)
            store.import_text("schema_version = 1\n")

            with self.assertRaisesRegex(ValueError, "unsupported schema"):
                store.import_text("schema_version = 2\n")

            self.assertEqual(store.export_text(), "schema_version = 1\n")

    def test_records_semantic_history_and_restores_an_exact_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(
                Path(directory) / "config.toml", lambda data: data["schema_version"]
            )
            first = store.import_text(
                "# first\nschema_version = 1\n[gateway]\nport = 8766\n"
            )
            second = store.import_text(
                "# second\nschema_version = 1\n[gateway]\nport = 9000\n"
            )

            self.assertEqual(
                store.diff(first.document),
                (ConfigChange(("gateway", "port"), 9000, 8766),),
            )
            self.assertEqual(
                tuple(item.revision for item in store.history()),
                (first.revision, second.revision),
            )

            restored = store.restore(first.revision)

            self.assertEqual(restored.revision, first.revision)
            self.assertEqual(store.export_text(), first.document.as_string())

    def test_serializes_concurrent_semantic_edits_without_losing_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(
                Path(directory) / "config.toml", lambda data: int(data["count"])
            )
            store.import_text("count = 0\n")
            ready = threading.Barrier(8)

            def increment() -> None:
                ready.wait()
                store.edit(
                    lambda document: document.__setitem__(
                        "count", document["count"] + 1
                    )
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                tuple(pool.map(lambda _index: increment(), range(8)))

            self.assertEqual(store.load().value, 8)

    def test_recovers_a_replaced_config_when_the_journal_commit_was_interrupted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            store = ConfigStore(path, lambda data: int(data["generation"]))
            store.import_text("generation = 1\n")
            current = store.import_text("generation = 2\n")
            journal = path.parent / ".config.toml.journal.jsonl"
            entries = journal.read_bytes().splitlines(keepends=True)
            journal.write_bytes(entries[0] + b'{"revision":')

            recovered = ConfigStore(path, lambda data: int(data["generation"]))

            self.assertEqual(recovered.load().value, 2)
            self.assertEqual(recovered.history()[-1].revision, current.revision)
            self.assertEqual(recovered.history()[-1].action, "recovered")

    def test_reports_a_complete_corrupt_journal_entry_instead_of_discarding_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            store = ConfigStore(path, lambda data: int(data["schema_version"]))
            store.import_text("schema_version = 1\n")
            journal = path.parent / ".config.toml.journal.jsonl"
            with journal.open("ab") as stream:
                stream.write(b"not-json\n")

            with self.assertRaisesRegex(RuntimeError, "config journal is corrupt"):
                store.history()


if __name__ == "__main__":
    unittest.main()
