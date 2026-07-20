import json
import stat
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence
from unittest.mock import patch

from mastic.infrastructure.codex_vite_discovery import (
    _MAX_PACKAGE_BYTES,
    _package_tree_digest,
    CodexViteDiscovery,
    CodexViteDiscoveryError,
    CommandResult,
)


NOW = datetime(2026, 7, 20, 18, 30, tzinfo=UTC)
INSTALL_ID = "#123e4567-e89b-42d3-a456-426614174000"


class FakeRunner:
    def __init__(self, results: dict[tuple[str, ...], CommandResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> CommandResult:
        key = tuple(argv)
        self.calls.append(key)
        if key not in self.results:
            raise AssertionError(f"unexpected command: {key}")
        return self.results[key]


class MutatingRunner(FakeRunner):
    def __init__(
        self,
        results: dict[tuple[str, ...], CommandResult],
        *,
        mutate_on: tuple[str, ...],
        mutation,
    ) -> None:
        super().__init__(results)
        self._mutate_on = mutate_on
        self._mutation = mutation

    def run(self, argv: Sequence[str]) -> CommandResult:
        result = super().run(argv)
        if tuple(argv) == self._mutate_on:
            self._mutation()
        return result


class ViteFixture:
    def __init__(
        self,
        root: Path,
        *,
        source: str = "npm",
        vite_install_id: str = INSTALL_ID,
    ) -> None:
        self.root = root
        self.vp_home = root / "vp-home"
        self.vp_bin = self.vp_home / "bin"
        self.vp_bin.mkdir(parents=True)
        self.path = (self.vp_bin,)
        self.version = "0.144.5"
        self.node_version = "24.18.0"
        self.bin_config_path = self.vp_home / "bins" / "codex.json"
        self.bin_config_path.parent.mkdir(parents=True)
        self.npm_root = (
            self.vp_home
            / "js_runtime"
            / "node"
            / self.node_version
            / "lib"
            / "node_modules"
        )

        if source == "npm":
            self.package_root = self.npm_root / "@openai" / "codex"
            bin_config_version = ""
            self.vite_metadata_path = None
        else:
            self.vite_metadata_path = (
                self.vp_home / "packages" / "@openai" / "codex.json"
            )
            self.vite_metadata_path.parent.mkdir(parents=True)
            self.vite_metadata_path.write_text(
                json.dumps(
                    {
                        "name": "@openai/codex",
                        "version": self.version,
                        "installId": vite_install_id,
                        "platform": {"node": self.node_version, "npm": "11.0.0"},
                        "bins": ["codex"],
                        "manager": "npm",
                        "installedAt": "2026-07-08T22:00:00Z",
                    }
                )
            )
            install_root = (
                self.vp_home / "packages" / "@openai" / f"codex{vite_install_id}"
            )
            self.package_root = (
                install_root / "lib" / "node_modules" / "@openai" / "codex"
            )
            bin_config_version = self.version

        self.package_bin = self.package_root / "bin" / "codex.js"
        self.package_bin.parent.mkdir(parents=True)
        self.package_bin.write_text("#!/usr/bin/env node\nconsole.log('codex')\n")
        self.package_bin.chmod(0o755)
        (self.package_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@openai/codex",
                    "version": self.version,
                    "bin": {"codex": "bin/codex.js"},
                }
            )
        )

        node_bin = self.vp_home / "js_runtime" / "node" / self.node_version / "bin"
        node_bin.mkdir(parents=True)
        node_codex = node_bin / "codex"
        node_codex.symlink_to(self.package_bin)
        self.active = self.vp_bin / "codex"
        self.active.symlink_to(node_codex if source == "npm" else self.package_bin)
        vp = self.vp_bin / "vp"
        npm = self.vp_bin / "npm"
        for executable in (vp, npm):
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)

        self.write_bin_config(source=source, version=bin_config_version)
        self.runner = FakeRunner(self.command_results(source=source))

    def write_bin_config(self, *, source: str, version: str) -> None:
        self.bin_config_path.write_text(
            json.dumps(
                {
                    "name": "codex",
                    "package": "@openai/codex",
                    "version": version,
                    "nodeVersion": self.node_version,
                    "source": source,
                }
            )
        )

    def command_results(self, *, source: str) -> dict[tuple[str, ...], CommandResult]:
        which_lines = [
            "VITE+ - The Unified Toolchain for the Web",
            "",
            str(self.package_bin.resolve()),
            (
                "  Package:    @openai/codex"
                if source == "npm"
                else f"  Package:    @openai/codex@{self.version}"
            ),
        ]
        if source == "npm":
            which_lines.append("  Source:     npm")
        else:
            which_lines.extend(["  Binaries:   codex", "  Installed:  2026-07-08"])
        which_lines.append(f"  Node:       {self.node_version}")
        doctor = {
            "schemaVersion": 1,
            "codexVersion": self.version,
            "checks": {
                "installation": {
                    "status": "ok",
                    "details": {
                        "install context": "npm",
                        "managed by npm": "true",
                        "npm update target": str(self.package_root.resolve()),
                    },
                },
                "updates.status": {
                    "status": "ok",
                    "details": {"update action": "npm install -g @openai/codex"},
                },
            },
        }
        return {
            (str(self.vp_bin / "vp"), "env", "which", "codex"): CommandResult(
                0, "\n".join(which_lines) + "\n", ""
            ),
            (str(self.active), "--version"): CommandResult(
                0, f"codex-cli {self.version}\n", ""
            ),
            (str(self.active), "doctor", "--json"): CommandResult(
                0, json.dumps(doctor), ""
            ),
            (str(self.vp_bin / "npm"), "root", "-g"): CommandResult(
                0, str(self.npm_root) + "\n", ""
            ),
        }

    def discover(self):
        return CodexViteDiscovery(
            vp_home=self.vp_home,
            path=self.path,
            runner=self.runner,
            observed_at=lambda: NOW,
            platform="darwin",
            architecture="arm64",
        ).discover(
            selected_installation_identity=("application-installation:codex:vite"),
            selected_release_channel="stable",
        )


class CodexViteDiscoveryTests(unittest.TestCase):
    def test_package_tree_bound_covers_current_apple_silicon_closure(self) -> None:
        self.assertGreaterEqual(_MAX_PACKAGE_BYTES, 768 * 1024 * 1024)

    def test_package_tree_digest_streams_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "payload").write_bytes(b"x" * (2 * 1024 * 1024))

            with patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("package files must be streamed"),
            ):
                digest = _package_tree_digest(root)

            self.assertTrue(digest.startswith("sha256:"))

    def test_package_tree_digest_rejects_symlinked_files_at_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.write_bytes(b"outside")
            package = root / "package"
            package.mkdir()
            (package / "payload").symlink_to(outside)

            with self.assertRaisesRegex(
                CodexViteDiscoveryError, "package_identity_mismatch"
            ):
                _package_tree_digest(package)

    def test_intercepted_npm_install_converges_on_one_owner_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary), source="npm")

            observed = fixture.discover()

            self.assertEqual(observed.owner_identity, "vite-plus/npm-global")
            self.assertEqual(observed.release_channel, "stable")
            self.assertEqual(observed.installed_release, "0.144.5")
            self.assertEqual(observed.active_invocation, str(fixture.active))
            self.assertEqual(observed.reachable_invocations, (str(fixture.active),))
            self.assertTrue(observed.installed_artifact_digest.startswith("sha256:"))
            self.assertTrue(observed.owner_installation_identity.startswith("sha256:"))

    def test_vite_native_metadata_selects_vite_lifecycle_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary), source="vp")

            observed = fixture.discover()

            self.assertEqual(observed.owner_identity, "vite-plus/global-package")
            self.assertEqual(observed.installed_release, fixture.version)
            self.assertNotIn(
                (str(fixture.vp_bin / "npm"), "root", "-g"),
                fixture.runner.calls,
            )

    def test_vite_package_root_rejects_symlinked_install_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary), source="vp")
            install_root = fixture.package_root.parents[3]
            attacker_root = fixture.root / "attacker-controlled-install"
            install_root.rename(attacker_root)
            install_root.symlink_to(attacker_root, target_is_directory=True)

            with self.assertRaises(CodexViteDiscoveryError) as invalid:
                fixture.discover()

            self.assertEqual(
                invalid.exception.reason_code,
                "package_identity_mismatch",
            )

    def test_vite_legacy_empty_install_id_uses_the_supported_legacy_layout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(
                Path(temporary),
                source="vp",
                vite_install_id="",
            )

            observed = fixture.discover()

            self.assertEqual(observed.owner_identity, "vite-plus/global-package")
            self.assertEqual(observed.installed_release, fixture.version)

    def test_missing_or_unknown_owner_metadata_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))
            fixture.bin_config_path.unlink()

            with self.assertRaises(CodexViteDiscoveryError) as missing:
                fixture.discover()
            self.assertEqual(missing.exception.reason_code, "owner_unresolved")

        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))
            fixture.write_bin_config(source="other", version="")

            with self.assertRaises(CodexViteDiscoveryError) as unknown:
                fixture.discover()
            self.assertEqual(unknown.exception.reason_code, "owner_metadata_invalid")

    def test_npm_doctor_must_prove_the_exact_update_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))
            command = (str(fixture.active), "doctor", "--json")
            doctor = json.loads(fixture.runner.results[command].stdout)
            doctor["checks"]["installation"]["details"]["npm update target"] = str(
                fixture.root / "different-install"
            )
            fixture.runner.results[command] = CommandResult(0, json.dumps(doctor), "")

            with self.assertRaises(CodexViteDiscoveryError) as mismatch:
                fixture.discover()
            self.assertEqual(
                mismatch.exception.reason_code, "owner_update_target_mismatch"
            )

    def test_runtime_version_must_match_owner_package_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))
            fixture.runner.results[(str(fixture.active), "--version")] = CommandResult(
                0, "codex-cli 0.144.6\n", ""
            )

            with self.assertRaises(CodexViteDiscoveryError) as mismatch:
                fixture.discover()
            self.assertEqual(
                mismatch.exception.reason_code, "installed_version_mismatch"
            )

    def test_malformed_doctor_contract_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))
            fixture.runner.results[(str(fixture.active), "doctor", "--json")] = (
                CommandResult(0, "{}", "")
            )

            with self.assertRaises(CodexViteDiscoveryError) as unavailable:
                fixture.discover()
            self.assertEqual(
                unavailable.exception.reason_code, "doctor_contract_unavailable"
            )

    def test_doctor_requires_supported_schema_and_successful_checks(self) -> None:
        for source, check_name in (
            ("npm", "updates.status"),
            ("vp", "installation"),
        ):
            with self.subTest(source=source, check_name=check_name):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = ViteFixture(Path(temporary), source=source)
                    command = (str(fixture.active), "doctor", "--json")
                    doctor = json.loads(fixture.runner.results[command].stdout)
                    doctor["checks"][check_name]["status"] = "error"
                    fixture.runner.results[command] = CommandResult(
                        0, json.dumps(doctor), ""
                    )

                    with self.assertRaises(CodexViteDiscoveryError) as unavailable:
                        fixture.discover()
                    self.assertEqual(
                        unavailable.exception.reason_code,
                        "doctor_contract_unavailable",
                    )

        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))
            command = (str(fixture.active), "doctor", "--json")
            doctor = json.loads(fixture.runner.results[command].stdout)
            doctor["schemaVersion"] = 2
            fixture.runner.results[command] = CommandResult(0, json.dumps(doctor), "")

            with self.assertRaises(CodexViteDiscoveryError) as unavailable:
                fixture.discover()
            self.assertEqual(
                unavailable.exception.reason_code, "doctor_contract_unavailable"
            )

    def test_oversized_package_metadata_is_a_package_identity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))
            (fixture.package_root / "package.json").write_bytes(b"x" * 1_048_577)

            with self.assertRaises(CodexViteDiscoveryError) as invalid:
                fixture.discover()
            self.assertEqual(invalid.exception.reason_code, "package_identity_mismatch")

    def test_package_tree_io_failure_is_a_typed_identity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))

            with (
                patch(
                    "mastic.infrastructure.codex_vite_discovery._file_digest",
                    side_effect=OSError("raced"),
                ),
                self.assertRaises(CodexViteDiscoveryError) as invalid,
            ):
                fixture.discover()
            self.assertEqual(invalid.exception.reason_code, "package_identity_mismatch")

    def test_owner_metadata_drift_during_discovery_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))
            doctor_command = (str(fixture.active), "doctor", "--json")

            def mutate_owner_metadata() -> None:
                fixture.write_bin_config(source="npm", version="0.144.6")

            fixture.runner = MutatingRunner(
                fixture.runner.results,
                mutate_on=doctor_command,
                mutation=mutate_owner_metadata,
            )

            with self.assertRaises(CodexViteDiscoveryError) as ambiguous:
                fixture.discover()
            self.assertEqual(ambiguous.exception.reason_code, "owner_ambiguous")

    def test_distinct_reachable_duplicate_blocks_but_same_target_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))
            alias_dir = fixture.root / "alias-bin"
            alias_dir.mkdir()
            (alias_dir / "codex").symlink_to(fixture.package_bin)
            fixture.path = (fixture.vp_bin, alias_dir)

            observed = fixture.discover()
            self.assertEqual(
                observed.reachable_invocations,
                tuple(sorted((str(fixture.active), str(alias_dir / "codex")))),
            )

            different = fixture.root / "different-codex"
            different.write_text("#!/bin/sh\n")
            different.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            (alias_dir / "codex").unlink()
            (alias_dir / "codex").symlink_to(different)

            with self.assertRaises(CodexViteDiscoveryError) as conflict:
                fixture.discover()
            self.assertEqual(
                conflict.exception.reason_code, "reachable_invocation_conflict"
            )


if __name__ == "__main__":
    unittest.main()
