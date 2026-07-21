import json
import os
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
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
from mastic.infrastructure.codex_owner_reconciliation import (
    SubprocessDiscoveryRunner,
)


NOW = datetime(2026, 7, 20, 18, 30, tzinfo=UTC)
INSTALL_ID = "#123e4567-e89b-42d3-a456-426614174000"


class FakeRunner:
    def __init__(self, results: dict[tuple[str, ...], CommandResult]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        executable_path: Sequence[Path] = (),
    ) -> CommandResult:
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

    def run(
        self,
        argv: Sequence[str],
        *,
        executable_path: Sequence[Path] = (),
    ) -> CommandResult:
        result = super().run(argv, executable_path=executable_path)
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
        node = node_bin / "node"
        node.write_text("#!/bin/sh\nexit 0\n")
        node.chmod(0o755)
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

    def discovery(self) -> CodexViteDiscovery:
        return CodexViteDiscovery(
            vp_home=self.vp_home,
            path=self.path,
            runner=self.runner,
            observed_at=lambda: NOW,
            platform="darwin",
            architecture="arm64",
        )

    def discover(self):
        return self.discovery().discover(
            selected_installation_identity=("application-installation:codex:vite"),
            selected_release_channel="stable",
        )


class CodexViteDiscoveryTests(unittest.TestCase):
    def test_discovery_rejects_unsupported_platform_before_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))

            with self.assertRaisesRegex(ValueError, "darwin/arm64"):
                CodexViteDiscovery(
                    vp_home=fixture.vp_home,
                    path=fixture.path,
                    runner=fixture.runner,
                    observed_at=lambda: NOW,
                    platform="linux",
                    architecture="x86_64",
                )

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

    def test_owner_runtime_path_reaches_vite_plus_env_node_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary), source="npm")
            node_bin = (
                fixture.vp_home / "js_runtime" / "node" / fixture.node_version / "bin"
            )
            node = node_bin / "node"
            doctor = fixture.command_results(source="npm")[
                (str(fixture.active), "doctor", "--json")
            ].stdout
            node.write_text(
                f"""#!{sys.executable}
import sys

if sys.argv[2:] == ["--version"]:
    print("codex-cli {fixture.version}")
elif sys.argv[2:] == ["doctor", "--json"]:
    print({doctor!r})
else:
    raise SystemExit(2)
"""
            )
            node.chmod(0o755)
            ambient_bin = fixture.root / "ambient-bin"
            ambient_bin.mkdir()
            ambient_node = ambient_bin / "node"
            ambient_node.write_text(
                f"#!{sys.executable}\nprint('codex-cli 0.0.0')\n",
                encoding="utf-8",
            )
            ambient_node.chmod(0o755)
            (fixture.vp_bin / "vp").write_text(
                f"""#!{sys.executable}
print("VITE+ - The Unified Toolchain for the Web")
print()
print({str(fixture.package_bin.resolve())!r})
print("  Package:    @openai/codex")
print("  Source:     npm")
print("  Node:       {fixture.node_version}")
"""
            )
            (fixture.vp_bin / "npm").write_text(
                f"""#!{sys.executable}
print({str(fixture.npm_root)!r})
"""
            )
            runner = SubprocessDiscoveryRunner(
                {
                    "HOME": str(fixture.root),
                    "PATH": str(ambient_bin),
                }
            )
            discovery = CodexViteDiscovery(
                vp_home=fixture.vp_home,
                path=fixture.path,
                runner=runner,
                observed_at=lambda: NOW,
                platform="darwin",
                architecture="arm64",
            )

            observed = discovery.discover(
                selected_installation_identity=("application-installation:codex:vite"),
                selected_release_channel="stable",
            )

            self.assertEqual(observed.owner_identity, "vite-plus/npm-global")
            self.assertEqual(observed.owner_runtime_identity, "node:24.18.0")
            self.assertEqual(observed.installed_release, "0.144.5")

    def test_owner_runtime_path_rejects_path_separator_in_an_entry(self) -> None:
        runner = SubprocessDiscoveryRunner({"HOME": tempfile.gettempdir()})
        invalid_entry = Path(tempfile.gettempdir()) / (
            f"owner-runtime{os.pathsep}injected"
        )

        with self.assertRaisesRegex(
            CodexViteDiscoveryError, "owner_command_environment_invalid"
        ):
            runner.run(
                ("/usr/bin/true",),
                executable_path=(invalid_entry,),
            )

    def test_ansi_styled_vite_metadata_resolves_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary), source="npm")
            command = (str(fixture.vp_bin / "vp"), "env", "which", "codex")
            fixture.runner.results[command] = CommandResult(
                0,
                "\n".join(
                    (
                        "\x1b[1mVITE+ - The Unified Toolchain for the Web\x1b[0m",
                        "",
                        str(fixture.package_bin.resolve()),
                        "  \x1b[2mPackage:  \x1b[0m  \x1b[94m@openai/codex\x1b[39m",
                        "  \x1b[2mSource:   \x1b[0m  \x1b[94mnpm\x1b[39m",
                        "  \x1b[2mNode:     \x1b[0m  \x1b[92m24.18.0\x1b[39m",
                    )
                )
                + "\n",
                "",
            )

            observed = fixture.discover()

            self.assertEqual(observed.owner_identity, "vite-plus/npm-global")
            self.assertEqual(observed.owner_runtime_identity, "node:24.18.0")

    def test_colon_delimited_sgr_metadata_resolves_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary), source="npm")
            command = (str(fixture.vp_bin / "vp"), "env", "which", "codex")
            result = fixture.runner.results[command]
            fixture.runner.results[command] = replace(
                result,
                stdout=result.stdout.replace(
                    "@openai/codex",
                    "\x1b[38:2::64:128:255m@openai/codex\x1b[0m",
                ),
            )

            observed = fixture.discover()

            self.assertEqual(observed.owner_identity, "vite-plus/npm-global")
            self.assertEqual(observed.owner_runtime_identity, "node:24.18.0")

    def test_duplicate_vite_owner_labels_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary), source="npm")
            command = (str(fixture.vp_bin / "vp"), "env", "which", "codex")
            result = fixture.runner.results[command]
            fixture.runner.results[command] = replace(
                result,
                stdout=result.stdout.replace(
                    "  Package:    @openai/codex",
                    "  Package:    attacker-controlled\n  Package:    @openai/codex",
                ),
            )

            with self.assertRaises(CodexViteDiscoveryError) as invalid:
                fixture.discover()

            self.assertEqual(invalid.exception.reason_code, "owner_metadata_invalid")

    def test_multiple_vite_executable_records_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary), source="npm")
            command = (str(fixture.vp_bin / "vp"), "env", "which", "codex")
            result = fixture.runner.results[command]
            executable = str(fixture.package_bin.resolve())
            fixture.runner.results[command] = replace(
                result,
                stdout=result.stdout.replace(
                    executable,
                    f"{executable}\n/attacker-controlled/codex",
                ),
            )

            with self.assertRaises(CodexViteDiscoveryError) as invalid:
                fixture.discover()

            self.assertEqual(invalid.exception.reason_code, "owner_metadata_invalid")

    def test_non_sgr_vite_output_controls_are_rejected(self) -> None:
        for control in ("\x00", "\x85", "\u2028"):
            with self.subTest(control=ascii(control)):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = ViteFixture(Path(temporary), source="npm")
                    command = (
                        str(fixture.vp_bin / "vp"),
                        "env",
                        "which",
                        "codex",
                    )
                    result = fixture.runner.results[command]
                    fixture.runner.results[command] = replace(
                        result,
                        stdout=result.stdout.replace("VITE+ -", f"VITE+{control} -"),
                    )

                    with self.assertRaises(CodexViteDiscoveryError) as invalid:
                        fixture.discover()

                    self.assertEqual(
                        invalid.exception.reason_code,
                        "owner_metadata_invalid",
                    )

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

    def test_payload_roots_preserve_the_discovered_owner_layout(self) -> None:
        for owner in ("npm", "vp"):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temporary:
                fixture = ViteFixture(Path(temporary), source=owner)
                discovery = CodexViteDiscovery(
                    vp_home=fixture.vp_home,
                    path=fixture.path,
                    runner=fixture.runner,
                    observed_at=lambda: NOW,
                    platform="darwin",
                    architecture="arm64",
                )
                observed = discovery.discover(
                    selected_installation_identity="application-installation:codex:vite",
                    selected_release_channel="stable",
                )

                roots = discovery.payload_roots(observed)

                self.assertEqual(roots["primary"], fixture.package_root.resolve())
                self.assertEqual(
                    roots["platform"],
                    fixture.package_root.resolve()
                    / "node_modules/@openai/codex-darwin-arm64",
                )

    def test_payload_roots_reject_wrong_application_before_filesystem_access(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary), source="vp")
            observed = fixture.discover()
            fixture.bin_config_path.unlink()

            with self.assertRaises(CodexViteDiscoveryError) as invalid:
                fixture.discovery().payload_roots(
                    replace(
                        observed,
                        application_identity="external-application:hindsight",
                    )
                )

            self.assertEqual(invalid.exception.reason_code, "owner_metadata_invalid")

    def test_payload_roots_reject_owner_metadata_drift_after_discovery(self) -> None:
        for owner in ("npm", "vp"):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temporary:
                fixture = ViteFixture(Path(temporary), source=owner)
                discovery = fixture.discovery()
                observed = discovery.discover(
                    selected_installation_identity=(
                        "application-installation:codex:vite"
                    ),
                    selected_release_channel="stable",
                )
                package_path = fixture.package_root / "package.json"
                package_metadata = json.loads(package_path.read_text())
                package_metadata["tampered"] = True
                package_path.write_text(json.dumps(package_metadata))

                with self.assertRaises(CodexViteDiscoveryError) as ambiguous:
                    discovery.payload_roots(observed)

                self.assertEqual(ambiguous.exception.reason_code, "owner_ambiguous")

    def test_payload_roots_reject_symlinked_vite_package_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary), source="vp")
            discovery = fixture.discovery()
            observed = fixture.discover()
            install_root = fixture.package_root.parents[3]
            attacker_root = fixture.root / "attacker-controlled-install"
            install_root.rename(attacker_root)
            install_root.symlink_to(attacker_root, target_is_directory=True)

            with self.assertRaises(CodexViteDiscoveryError) as invalid:
                discovery.payload_roots(observed)

            self.assertEqual(invalid.exception.reason_code, "package_identity_mismatch")

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

    def test_failed_runtime_probe_is_not_a_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))
            fixture.runner.results[(str(fixture.active), "--version")] = CommandResult(
                127, "", "node unavailable"
            )

            with self.assertRaises(CodexViteDiscoveryError) as unavailable:
                fixture.discover()

            self.assertEqual(
                unavailable.exception.reason_code, "owner_runtime_unavailable"
            )

    def test_missing_owner_runtime_does_not_fall_back_to_another_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = ViteFixture(Path(temporary))
            (
                fixture.vp_home
                / "js_runtime"
                / "node"
                / fixture.node_version
                / "bin"
                / "node"
            ).unlink()
            fallback = fixture.vp_bin / "node"
            fallback.write_text("#!/bin/sh\nexit 0\n")
            fallback.chmod(0o755)

            with self.assertRaises(CodexViteDiscoveryError) as unavailable:
                fixture.discover()

            self.assertEqual(
                unavailable.exception.reason_code, "owner_runtime_unavailable"
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
