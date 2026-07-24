import base64
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from mastic.application.external_application_lifecycle import (
    OwnerUpgradeCommandError,
    VerifiedArtifact,
    VerifiedArtifactClosure,
)
from mastic.domain.canonical import canonical_fingerprint
from mastic.domain.external_applications import CurrentReleaseResolution
from mastic.infrastructure.codex_artifact_closure import (
    BoundedHttpsDownloader,
    CodexViteArtifactClosureVerifier,
    DownloadedArtifact,
    NpmCodexArtifactClosureMaterializer,
)
from mastic.infrastructure.codex_npm_authority import HttpResponse


NOW = datetime(2026, 7, 20, 22, 0, tzinfo=UTC)
PACKUMENT_URL = "https://registry.npmjs.org/@openai%2Fcodex"
MAIN_URL = "https://registry.npmjs.org/@openai/codex/-/codex-0.144.6.tgz"
PLATFORM_URL = (
    "https://registry.npmjs.org/@openai/codex/-/codex-0.144.6-darwin-arm64.tgz"
)


def archive(*, version, files, optional_dependencies=None):
    package = {"name": "@openai/codex", "version": version}
    if optional_dependencies is not None:
        package["optionalDependencies"] = optional_dependencies
    payloads = {"package/package.json": json.dumps(package).encode(), **files}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, payload in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue(), payloads


MAIN, MAIN_FILES = archive(
    version="0.144.6",
    files={"package/bin/codex.js": b"wrapper"},
    optional_dependencies={
        "@openai/codex-darwin-arm64": "npm:@openai/codex@0.144.6-darwin-arm64"
    },
)
PLATFORM, PLATFORM_FILES = archive(
    version="0.144.6-darwin-arm64",
    files={"package/vendor/aarch64-apple-darwin/codex/codex": b"native"},
)


def sri(payload):
    return "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode()


def packument(*, platform_integrity=None):
    return json.dumps(
        {
            "name": "@openai/codex",
            "dist-tags": {"latest": "0.144.6"},
            "versions": {
                "0.144.6": {
                    "name": "@openai/codex",
                    "version": "0.144.6",
                    "dist": {"tarball": MAIN_URL, "integrity": sri(MAIN)},
                },
                "0.144.6-darwin-arm64": {
                    "name": "@openai/codex",
                    "version": "0.144.6-darwin-arm64",
                    "dist": {
                        "tarball": PLATFORM_URL,
                        "integrity": platform_integrity or sri(PLATFORM),
                    },
                },
            },
        }
    ).encode()


def packument_for(main_payload, platform_payload=PLATFORM):
    return json.dumps(
        {
            "name": "@openai/codex",
            "dist-tags": {"latest": "0.144.6"},
            "versions": {
                "0.144.6": {
                    "name": "@openai/codex",
                    "version": "0.144.6",
                    "dist": {"tarball": MAIN_URL, "integrity": sri(main_payload)},
                },
                "0.144.6-darwin-arm64": {
                    "name": "@openai/codex",
                    "version": "0.144.6-darwin-arm64",
                    "dist": {
                        "tarball": PLATFORM_URL,
                        "integrity": sri(platform_payload),
                    },
                },
            },
        }
    ).encode()


def archive_with_duplicate_package_json():
    package = json.dumps(
        {
            "name": "@openai/codex",
            "version": "0.144.6",
            "optionalDependencies": {
                "@openai/codex-darwin-arm64": ("npm:@openai/codex@0.144.6-darwin-arm64")
            },
        }
    ).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for payload in (package, package):
            info = tarfile.TarInfo("package/package.json")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def installed_payload_digest(files):
    records = []
    for path, payload in files.items():
        relative = path.removeprefix("package/")
        if "node_modules" in Path(relative).parts:
            continue
        records.append(
            {
                "path": relative,
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    return canonical_fingerprint(sorted(records, key=lambda item: item["path"]))


class Fetcher:
    def __init__(self, responses):
        self.responses = iter(responses)

    def fetch(self, url, *, maximum_bytes):
        self.last = (url, maximum_bytes)
        return HttpResponse(200, url, next(self.responses))


class Downloader:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def download(self, url, destination, *, maximum_bytes):
        payload = self.payloads[url]
        self.calls.append((url, destination, maximum_bytes))
        destination.write_bytes(payload)
        return DownloadedArtifact(
            status=200,
            final_url=url,
            size=len(payload),
            sha512_digest="sha512:" + hashlib.sha512(payload).hexdigest(),
        )


class NpmConfigLoadingRunner:
    def __init__(self, npm: Path, node: Path):
        self.npm = npm.resolve()
        self.node = node.resolve()
        self.calls = []

    def run(self, argv, *, cwd, environment, timeout_seconds):
        completed = subprocess.run(
            (self.node, self.npm, "config", "get", "cache"),
            cwd=cwd,
            env=environment,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        self.calls.append((tuple(argv), dict(environment), completed))
        return completed.returncode


def resolution(*, main_payload=MAIN):
    return CurrentReleaseResolution(
        installation_identity="application-installation:codex:vite",
        installation_observation_fingerprint="sha256:" + "a" * 64,
        owner_identity="vite-plus/npm-global",
        release_channel="npm:latest",
        platform="darwin",
        architecture="arm64",
        exact_release="0.144.6",
        artifact_coordinate=MAIN_URL,
        artifact_digest="sha512:" + hashlib.sha512(main_payload).hexdigest(),
        authority_identity="release-authority:npmjs:@openai/codex:dist-tag:latest",
        authority_response_digest="sha256:" + "b" * 64,
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        resolver_policy_identity="current-online:v1",
        validation_profile_identity="codex-current:v1",
    )


class ArtifactClosureMaterializerTests(unittest.TestCase):
    def test_downloader_preserves_response_open_failure(self):
        class FailingOpener:
            def open(self, request, *, timeout):
                raise urllib.error.URLError("network unavailable")

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.tgz"
            downloader = BoundedHttpsDownloader(opener=FailingOpener())

            with self.assertRaisesRegex(
                urllib.error.URLError,
                "network unavailable",
            ):
                downloader.download(MAIN_URL, destination, maximum_bytes=1024)

            self.assertFalse(destination.exists())

    def test_stages_stable_main_and_platform_artifacts_with_payload_proofs(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher = Fetcher([packument(), packument()])
            downloader = Downloader({MAIN_URL: MAIN, PLATFORM_URL: PLATFORM})
            materializer = NpmCodexArtifactClosureMaterializer(
                stage_root=Path(directory), fetcher=fetcher, downloader=downloader
            )

            selected = materializer.materialize(resolution())

            self.assertEqual(selected.exact_release, "0.144.6")
            self.assertEqual(
                selected.artifact("primary").installed_payload_digest,
                installed_payload_digest(MAIN_FILES),
            )
            self.assertEqual(
                selected.artifact("platform").installed_payload_digest,
                installed_payload_digest(PLATFORM_FILES),
            )
            self.assertTrue(selected.artifact("primary").staged_path.is_file())
            self.assertTrue(selected.artifact("platform").staged_path.is_file())
            self.assertEqual(selected.staging_directory.stat().st_mode & 0o077, 0)
            self.assertEqual(selected.cache_directory.stat().st_mode & 0o077, 0)
            self.assertEqual(
                [call[0] for call in downloader.calls], [MAIN_URL, PLATFORM_URL]
            )

            with (
                patch(
                    "mastic.infrastructure.codex_artifact_closure.shutil.rmtree",
                    side_effect=OSError("blocked"),
                ),
                self.assertRaisesRegex(
                    OwnerUpgradeCommandError, "artifact_release_failed"
                ),
            ):
                materializer.release(selected)
            self.assertTrue(selected.staging_directory.exists())

            materializer.release(selected)
            materializer.release(selected)
            self.assertFalse(selected.staging_directory.exists())

    def test_packument_drift_discards_the_candidate_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            materializer = NpmCodexArtifactClosureMaterializer(
                stage_root=Path(directory),
                fetcher=Fetcher(
                    [
                        packument(),
                        packument(platform_integrity=sri(PLATFORM + b"changed")),
                    ]
                ),
                downloader=Downloader({MAIN_URL: MAIN, PLATFORM_URL: PLATFORM}),
            )

            with self.assertRaisesRegex(OwnerUpgradeCommandError, "authority_changed"):
                materializer.materialize(resolution())

    def test_corrupt_or_duplicate_archive_is_rejected_as_archive_invalid(self):
        for main_payload in (b"not-a-gzip", archive_with_duplicate_package_json()):
            with (
                self.subTest(size=len(main_payload)),
                tempfile.TemporaryDirectory() as directory,
            ):
                authority = packument_for(main_payload)
                materializer = NpmCodexArtifactClosureMaterializer(
                    stage_root=Path(directory),
                    fetcher=Fetcher([authority, authority]),
                    downloader=Downloader(
                        {MAIN_URL: main_payload, PLATFORM_URL: PLATFORM}
                    ),
                )

                with self.assertRaisesRegex(
                    OwnerUpgradeCommandError, "archive_invalid"
                ):
                    materializer.materialize(resolution(main_payload=main_payload))


class Runner:
    def __init__(self):
        self.calls = []
        self.config_snapshots = []

    def run(self, argv, *, cwd, environment, timeout_seconds):
        self.calls.append((tuple(argv), cwd, dict(environment), timeout_seconds))
        snapshot = {}
        for name in ("NPM_CONFIG_USERCONFIG", "NPM_CONFIG_GLOBALCONFIG"):
            if name in environment:
                path = Path(environment[name])
                snapshot[name] = (path, path.lstat(), path.read_bytes())
        if snapshot:
            self.config_snapshots.append(snapshot)
        return 0


class Roots:
    def __init__(self, roots):
        self.roots = roots

    def locate(self, _observation):
        return self.roots


def write_installed(root, files):
    for path, payload in files.items():
        relative = path.removeprefix("package/")
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)


class ArtifactClosureVerifierTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("npm") and shutil.which("node"),
        "real npm and Node.js are unavailable",
    )
    def test_private_configs_cross_the_real_npm_loading_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = NpmCodexArtifactClosureMaterializer(
                stage_root=root,
                fetcher=Fetcher([packument(), packument()]),
                downloader=Downloader({MAIN_URL: MAIN, PLATFORM_URL: PLATFORM}),
            ).materialize(resolution())
            runner = NpmConfigLoadingRunner(
                Path(str(shutil.which("npm"))),
                Path(str(shutil.which("node"))),
            )
            verifier = CodexViteArtifactClosureVerifier(
                vp_home=root,
                roots=Roots({}),
                runner=runner,
                base_environment={"HOME": str(root)},
            )

            verifier.prepare(artifacts, "node:24.18.0")

            self.assertEqual(len(runner.calls), 2)
            for _argv, environment, completed in runner.calls:
                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                self.assertNotEqual(
                    environment["NPM_CONFIG_USERCONFIG"],
                    environment["NPM_CONFIG_GLOBALCONFIG"],
                )

    def test_retained_closure_can_prepare_the_cache_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = NpmCodexArtifactClosureMaterializer(
                stage_root=root,
                fetcher=Fetcher([packument(), packument()]),
                downloader=Downloader({MAIN_URL: MAIN, PLATFORM_URL: PLATFORM}),
            ).materialize(resolution())
            runner = Runner()
            verifier = CodexViteArtifactClosureVerifier(
                vp_home=root,
                roots=Roots({}),
                runner=runner,
                base_environment={"HOME": str(root)},
            )

            verifier.prepare(artifacts, "node:24.18.0")
            verifier.prepare(artifacts, "node:24.18.0")

            self.assertEqual(len(runner.calls), 4)
            self.assertFalse((artifacts.staging_directory / "npm-user.config").exists())
            self.assertFalse(
                (artifacts.staging_directory / "npm-global.config").exists()
            )

    def test_failed_private_config_creation_removes_its_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = NpmCodexArtifactClosureMaterializer(
                stage_root=root,
                fetcher=Fetcher([packument(), packument()]),
                downloader=Downloader({MAIN_URL: MAIN, PLATFORM_URL: PLATFORM}),
            ).materialize(resolution())
            verifier = CodexViteArtifactClosureVerifier(
                vp_home=root,
                roots=Roots({}),
                runner=Runner(),
                base_environment={"HOME": str(root)},
            )

            with (
                patch(
                    "mastic.infrastructure.codex_artifact_closure.os.fsync",
                    side_effect=OSError("sync failed"),
                ),
                self.assertRaisesRegex(
                    OwnerUpgradeCommandError,
                    "artifact_config_prepare_failed",
                ),
            ):
                verifier.prepare(artifacts, "node:24.18.0")

            self.assertFalse((artifacts.staging_directory / "npm-user.config").exists())

    def test_second_private_config_failure_removes_the_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = NpmCodexArtifactClosureMaterializer(
                stage_root=root,
                fetcher=Fetcher([packument(), packument()]),
                downloader=Downloader({MAIN_URL: MAIN, PLATFORM_URL: PLATFORM}),
            ).materialize(resolution())
            verifier = CodexViteArtifactClosureVerifier(
                vp_home=root,
                roots=Roots({}),
                runner=Runner(),
                base_environment={"HOME": str(root)},
            )
            real_open = os.open
            calls = 0

            def fail_second_open(path, flags, mode):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second config failed")
                return real_open(path, flags, mode)

            with (
                patch(
                    "mastic.infrastructure.codex_artifact_closure.os.open",
                    side_effect=fail_second_open,
                ),
                self.assertRaisesRegex(
                    OwnerUpgradeCommandError,
                    "artifact_config_prepare_failed",
                ),
            ):
                verifier.prepare(artifacts, "node:24.18.0")

            self.assertFalse((artifacts.staging_directory / "npm-user.config").exists())
            self.assertFalse(
                (artifacts.staging_directory / "npm-global.config").exists()
            )

    def test_prepares_private_cache_and_verifies_both_installed_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "stage"
            stage.mkdir(mode=0o700)
            cache = stage / "cache"
            cache.mkdir(mode=0o700)
            main_path = stage / "main.tgz"
            platform_path = stage / "platform.tgz"
            main_path.write_bytes(MAIN)
            platform_path.write_bytes(PLATFORM)
            artifacts = VerifiedArtifactClosure(
                application_identity="external-application:codex",
                exact_release="0.144.6",
                artifacts=(
                    VerifiedArtifact(
                        "primary",
                        "@openai/codex",
                        "0.144.6",
                        MAIN_URL,
                        "sha512:" + hashlib.sha512(MAIN).hexdigest(),
                        installed_payload_digest(MAIN_FILES),
                        main_path,
                    ),
                    VerifiedArtifact(
                        "platform",
                        "@openai/codex-darwin-arm64",
                        "0.144.6-darwin-arm64",
                        PLATFORM_URL,
                        "sha512:" + hashlib.sha512(PLATFORM).hexdigest(),
                        installed_payload_digest(PLATFORM_FILES),
                        platform_path,
                    ),
                ),
                staging_directory=stage,
                cache_directory=cache,
            )
            install_root = Path(directory) / "packages" / "@openai" / "codex#test"
            main_root = install_root / "lib/node_modules/@openai/codex"
            platform_root = main_root / "node_modules/@openai/codex-darwin-arm64"
            write_installed(main_root, MAIN_FILES)
            write_installed(platform_root, PLATFORM_FILES)
            runner = Runner()
            verifier = CodexViteArtifactClosureVerifier(
                vp_home=Path(directory),
                roots=Roots({"primary": main_root, "platform": platform_root}),
                runner=runner,
                base_environment={"HOME": "/Users/test"},
            )

            verifier.prepare(artifacts, "node:24.18.0")
            verifier.verify_staged(artifacts)
            verifier.verify_installed(artifacts, object())

            self.assertEqual(len(runner.calls), 2)
            self.assertTrue(all("cache" in call[0] for call in runner.calls))
            self.assertTrue(
                all(call[2]["NPM_CONFIG_OFFLINE"] == "true" for call in runner.calls)
            )
            self.assertTrue(
                all("NPM_CONFIG_ALWAYS_AUTH" not in call[2] for call in runner.calls)
            )
            for snapshot in runner.config_snapshots:
                user_config = snapshot["NPM_CONFIG_USERCONFIG"][0]
                global_config = snapshot["NPM_CONFIG_GLOBALCONFIG"][0]
                self.assertNotEqual(user_config, global_config)
                for config, metadata, payload in snapshot.values():
                    self.assertTrue(stat.S_ISREG(metadata.st_mode))
                    self.assertFalse(stat.S_ISLNK(metadata.st_mode))
                    self.assertEqual(payload, b"")
                    self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                    self.assertEqual(config.parent, stage)
                    self.assertFalse(config.exists())

            extra_dependency = main_root / "node_modules/extra"
            extra_dependency.mkdir()
            with self.assertRaisesRegex(
                OwnerUpgradeCommandError, "installed_topology_mismatch"
            ):
                verifier.verify_installed(artifacts, object())
            extra_dependency.rmdir()

            escape_home = Path(directory) / "trusted-home"
            escape_home.mkdir()
            escaped_main = Path(directory) / "escaped-main"
            escaped_platform = escaped_main / "node_modules/@openai/codex-darwin-arm64"
            write_installed(escaped_main, MAIN_FILES)
            write_installed(escaped_platform, PLATFORM_FILES)
            escaping_verifier = CodexViteArtifactClosureVerifier(
                vp_home=escape_home,
                roots=Roots(
                    {
                        "primary": escape_home / ".." / escaped_main.name,
                        "platform": escape_home
                        / ".."
                        / escaped_platform.relative_to(Path(directory)),
                    }
                ),
                runner=Runner(),
                base_environment={"HOME": "/Users/test"},
            )
            with self.assertRaisesRegex(
                OwnerUpgradeCommandError, "installed_topology_mismatch"
            ):
                escaping_verifier.verify_installed(artifacts, object())

            relocated_install = Path(directory) / "attacker-controlled-install"
            install_root.rename(relocated_install)
            install_root.symlink_to(relocated_install, target_is_directory=True)
            with self.assertRaisesRegex(
                OwnerUpgradeCommandError, "installed_topology_mismatch"
            ):
                verifier.verify_installed(artifacts, object())
            install_root.unlink()
            relocated_install.rename(install_root)

            platform_target = Path(directory) / "relocated-platform"
            platform_root.rename(platform_target)
            platform_root.symlink_to(platform_target, target_is_directory=True)
            with self.assertRaisesRegex(
                OwnerUpgradeCommandError, "installed_topology_mismatch"
            ):
                verifier.verify_installed(artifacts, object())

            main_path.write_bytes(MAIN + b"changed")
            with self.assertRaisesRegex(
                OwnerUpgradeCommandError, "staged_archive_changed"
            ):
                verifier.verify_staged(artifacts)

            with (
                patch(
                    "mastic.infrastructure.codex_artifact_closure._file_sha512",
                    side_effect=OSError("raced"),
                ),
                self.assertRaisesRegex(
                    OwnerUpgradeCommandError, "staged_archive_changed"
                ),
            ):
                verifier.verify_staged(artifacts)


if __name__ == "__main__":
    unittest.main()
