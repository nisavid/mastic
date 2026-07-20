import base64
import hashlib
import io
import json
import tarfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Sequence
from urllib.request import Request

from mastic.application.current_release import (
    CurrentReleaseAuthorityQuery,
    ReleaseArtifactUnavailableError,
    ReleaseAuthorityInvalidResponseError,
    ReleaseAuthorityUnavailableError,
    resolve_current_release,
)
from mastic.application.application_upgrade_policy import build_upgrade_candidate
from mastic.domain.application_lifecycle import ReleaseTransitionKind
from mastic.domain.external_applications import (
    ExternalApplicationInstallation,
    InstallationObservation,
    ReleaseIntent,
)
from mastic.infrastructure.codex_npm_authority import (
    BoundedHttpsFetcher,
    HttpResponse,
    NpmCodexArtifactMaterializer,
    NpmCodexReleaseAuthority,
    _RejectRedirects,
)


NOW = datetime(2026, 7, 20, 20, 30, tzinfo=UTC)
PACKUMENT_URL = "https://registry.npmjs.org/@openai%2Fcodex"
TARBALL_URL = "https://registry.npmjs.org/@openai/codex/-/codex-0.150.0.tgz"


def npm_tarball(*, name: str = "@openai/codex", version: str = "0.150.0") -> bytes:
    package_json = json.dumps({"name": name, "version": version}).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(package_json)
        archive.addfile(info, io.BytesIO(package_json))
        readme = b"Codex"
        info = tarfile.TarInfo("package/README.md")
        info.size = len(readme)
        archive.addfile(info, io.BytesIO(readme))
    return buffer.getvalue()


TARBALL = npm_tarball()
TARBALL_SHA512 = hashlib.sha512(TARBALL).hexdigest()
TARBALL_SRI = "sha512-" + base64.b64encode(hashlib.sha512(TARBALL).digest()).decode(
    "ascii"
)


class FakeFetcher:
    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, int]] = []

    def fetch(self, url: str, *, maximum_bytes: int) -> HttpResponse:
        self.requests.append((url, maximum_bytes))
        response = self.responses[url]
        if len(response.body) > maximum_bytes:
            raise OSError("response too large")
        return response


class FakeOpenedResponse:
    def __init__(self, url: str, body: bytes) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self._url = url
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, maximum: int) -> bytes:
        return self._body[:maximum]

    def geturl(self) -> str:
        return self._url


class FakeOpener:
    def __init__(self, opened: FakeOpenedResponse) -> None:
        self.opened = opened
        self.requests: list[Request] = []

    def open(self, request: Request, *, timeout: float):
        del timeout
        self.requests.append(request)
        return self.opened


def packument(*, integrity: str = TARBALL_SRI, latest: str = "0.150.0") -> bytes:
    return json.dumps(
        {
            "name": "@openai/codex",
            "dist-tags": {"latest": latest},
            "versions": {
                "0.150.0": {
                    "name": "@openai/codex",
                    "version": "0.150.0",
                    "dist": {
                        "integrity": integrity,
                        "tarball": TARBALL_URL,
                    },
                }
            },
        }
    ).encode()


def response(url: str, body: bytes, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, final_url=url, body=body)


def query(**changes: str) -> CurrentReleaseAuthorityQuery:
    values = {
        "application_identity": "external-application:codex",
        "installation_identity": "application-installation:codex:vite",
        "installation_observation_fingerprint": "sha256:" + "a" * 64,
        "owner_identity": "vite-plus/npm-global",
        "owner_installation_identity": "sha256:" + "b" * 64,
        "release_channel": "npm:latest",
        "platform": "darwin",
        "architecture": "arm64",
    }
    values.update(changes)
    return CurrentReleaseAuthorityQuery(**values)


class CodexNpmReleaseAuthorityTests(unittest.TestCase):
    def test_bounded_fetcher_requests_the_abbreviated_npm_packument(self) -> None:
        opener = FakeOpener(FakeOpenedResponse(PACKUMENT_URL, b"{}"))
        fetcher = BoundedHttpsFetcher(opener=opener)

        fetcher.fetch(PACKUMENT_URL, maximum_bytes=1024)

        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(
            opener.requests[0].get_header("Accept"),
            "application/vnd.npm.install-v1+json, application/octet-stream;q=0.9",
        )

    def test_latest_dist_tag_resolves_exact_npm_integrity_without_mutation(
        self,
    ) -> None:
        fetcher = FakeFetcher({PACKUMENT_URL: response(PACKUMENT_URL, packument())})
        authority = NpmCodexReleaseAuthority(fetcher=fetcher, clock=lambda: NOW)

        observed = authority.resolve_current(query())

        self.assertEqual(observed.exact_release, "0.150.0")
        self.assertEqual(observed.artifact_coordinate, TARBALL_URL)
        self.assertEqual(observed.artifact_digest, "sha512:" + TARBALL_SHA512)
        self.assertEqual(
            observed.authority_identity,
            "release-authority:npmjs:@openai/codex:dist-tag:latest",
        )
        self.assertEqual(observed.observed_at, NOW)
        self.assertIsNone(observed.valid_until)
        self.assertTrue(observed.response_digest.startswith("sha256:"))
        self.assertEqual(fetcher.requests, [(PACKUMENT_URL, 8 * 1024 * 1024)])

    def test_wrong_owner_channel_or_subject_fails_before_network(self) -> None:
        fetcher = FakeFetcher({})
        authority = NpmCodexReleaseAuthority(fetcher=fetcher, clock=lambda: NOW)

        for changed in (
            {"application_identity": "external-application:other"},
            {"owner_identity": "homebrew"},
            {"release_channel": "npm:next"},
            {"platform": "linux"},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ReleaseAuthorityInvalidResponseError):
                    authority.resolve_current(query(**changed))
        self.assertEqual(fetcher.requests, [])

    def test_non_semver_latest_cannot_become_an_owner_command_target(self) -> None:
        malformed = json.loads(packument())
        package = malformed["versions"].pop("0.150.0")
        malformed["dist-tags"]["latest"] = "latest"
        package["version"] = "latest"
        malformed["versions"]["latest"] = package
        authority = NpmCodexReleaseAuthority(
            fetcher=FakeFetcher(
                {PACKUMENT_URL: response(PACKUMENT_URL, json.dumps(malformed).encode())}
            ),
            clock=lambda: NOW,
        )

        with self.assertRaises(ReleaseAuthorityInvalidResponseError):
            authority.resolve_current(query())

    def test_vite_native_owner_uses_the_same_selected_npm_release_authority(
        self,
    ) -> None:
        authority = NpmCodexReleaseAuthority(
            fetcher=FakeFetcher({PACKUMENT_URL: response(PACKUMENT_URL, packument())}),
            clock=lambda: NOW,
        )

        observed = authority.resolve_current(
            query(owner_identity="vite-plus/global-package")
        )

        self.assertEqual(observed.exact_release, "0.150.0")
        self.assertEqual(observed.artifact_digest, "sha512:" + TARBALL_SHA512)

    def test_malformed_integrity_or_untrusted_tarball_fails_closed(self) -> None:
        bad_payloads: Sequence[bytes] = (
            packument(integrity="sha1-deadbeef"),
            packument(integrity="sha512-%%%"),
            packument(latest="0.151.0"),
            packument().replace(
                TARBALL_URL.encode(),
                b"https://example.com/codex.tgz",
            ),
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                authority = NpmCodexReleaseAuthority(
                    fetcher=FakeFetcher(
                        {PACKUMENT_URL: response(PACKUMENT_URL, payload)}
                    ),
                    clock=lambda: NOW,
                )
                with self.assertRaises(ReleaseAuthorityInvalidResponseError):
                    authority.resolve_current(query())

    def test_latest_dist_tag_must_be_a_strict_semantic_version(self) -> None:
        invalid_versions: Sequence[object] = (
            ["0.150.0"],
            "latest",
            "01.2.3",
            "1.2",
            "1.2.3-01",
            "1.2٢.3",
            "1.2.3-1١",
        )
        for version in invalid_versions:
            payload = json.dumps(
                {
                    "name": "@openai/codex",
                    "dist-tags": {"latest": version},
                    "versions": {
                        str(version): {
                            "name": "@openai/codex",
                            "version": version,
                            "dist": {
                                "integrity": TARBALL_SRI,
                                "tarball": TARBALL_URL,
                            },
                        }
                    },
                }
            ).encode()
            with self.subTest(version=version):
                authority = NpmCodexReleaseAuthority(
                    fetcher=FakeFetcher(
                        {PACKUMENT_URL: response(PACKUMENT_URL, payload)}
                    ),
                    clock=lambda: NOW,
                )
                with self.assertRaises(ReleaseAuthorityInvalidResponseError):
                    authority.resolve_current(query())

    def test_redirected_or_failed_packument_is_not_authority_evidence(self) -> None:
        redirected = NpmCodexReleaseAuthority(
            fetcher=FakeFetcher(
                {PACKUMENT_URL: response("https://example.com/packument", packument())}
            ),
            clock=lambda: NOW,
        )
        with self.assertRaises(ReleaseAuthorityInvalidResponseError):
            redirected.resolve_current(query())

        unavailable = NpmCodexReleaseAuthority(
            fetcher=FakeFetcher(
                {
                    PACKUMENT_URL: response(
                        PACKUMENT_URL,
                        packument(),
                        status=503,
                    )
                }
            ),
            clock=lambda: NOW,
        )
        with self.assertRaises(ReleaseAuthorityUnavailableError):
            unavailable.resolve_current(query())


class CodexNpmMaterializerTests(unittest.TestCase):
    def test_materializer_downloads_and_verifies_the_exact_authority_tarball(
        self,
    ) -> None:
        fetcher = FakeFetcher({TARBALL_URL: response(TARBALL_URL, TARBALL)})
        authority = NpmCodexReleaseAuthority(
            fetcher=FakeFetcher({PACKUMENT_URL: response(PACKUMENT_URL, packument())}),
            clock=lambda: NOW,
        )
        selected = authority.resolve_current(query())

        materialized = NpmCodexArtifactMaterializer(fetcher).materialize(selected)

        self.assertEqual(materialized.coordinate, TARBALL_URL)
        self.assertEqual(materialized.digest, "sha512:" + TARBALL_SHA512)
        self.assertEqual(fetcher.requests, [(TARBALL_URL, 64 * 1024 * 1024)])

    def test_materializer_rejects_digest_mismatch_redirect_or_wrong_authority(
        self,
    ) -> None:
        authority = NpmCodexReleaseAuthority(
            fetcher=FakeFetcher({PACKUMENT_URL: response(PACKUMENT_URL, packument())}),
            clock=lambda: NOW,
        )
        selected = authority.resolve_current(query())
        cases = (
            response(TARBALL_URL, b"tampered"),
            response("https://example.com/codex.tgz", TARBALL),
        )
        for item in cases:
            with self.subTest(response=item):
                materializer = NpmCodexArtifactMaterializer(
                    FakeFetcher({TARBALL_URL: item})
                )
                with self.assertRaises(ReleaseArtifactUnavailableError):
                    materializer.materialize(selected)

        wrong_authority = replace(
            selected,
            authority_identity="release-authority:evil",
        )
        materializer = NpmCodexArtifactMaterializer(
            FakeFetcher({TARBALL_URL: response(TARBALL_URL, TARBALL)})
        )
        with self.assertRaises(ReleaseArtifactUnavailableError):
            materializer.materialize(wrong_authority)

    def test_materializer_rejects_tarball_package_identity_or_version_mismatch(
        self,
    ) -> None:
        authority = NpmCodexReleaseAuthority(
            fetcher=FakeFetcher({PACKUMENT_URL: response(PACKUMENT_URL, packument())}),
            clock=lambda: NOW,
        )
        selected = authority.resolve_current(query())
        for payload in (
            npm_tarball(name="other"),
            npm_tarball(version="0.149.0"),
        ):
            changed = type(selected)(
                exact_release=selected.exact_release,
                artifact_coordinate=selected.artifact_coordinate,
                artifact_digest="sha512:" + hashlib.sha512(payload).hexdigest(),
                authority_identity=selected.authority_identity,
                response_digest=selected.response_digest,
                observed_at=selected.observed_at,
                valid_until=selected.valid_until,
            )
            with self.subTest(payload=payload):
                materializer = NpmCodexArtifactMaterializer(
                    FakeFetcher({TARBALL_URL: response(TARBALL_URL, payload)})
                )
                with self.assertRaises(ReleaseArtifactUnavailableError):
                    materializer.materialize(changed)


class CodexNpmIntegrationTests(unittest.TestCase):
    def test_authority_materialization_and_upgrade_candidate_accept_npm_sha512(
        self,
    ) -> None:
        selected = ExternalApplicationInstallation(
            application_identity="external-application:codex",
            installation_identity="application-installation:codex:vite",
            owner_identity="vite-plus/npm-global",
            release_intent=ReleaseIntent.current(channel="npm:latest"),
            platform="darwin",
            architecture="arm64",
        )
        observed = InstallationObservation(
            application_identity=selected.application_identity,
            installation_identity=selected.installation_identity,
            owner_identity=selected.owner_identity,
            owner_installation_identity="sha256:" + "b" * 64,
            owner_runtime_identity="node:24.18.0",
            release_channel="npm:latest",
            platform="darwin",
            architecture="arm64",
            installed_release="0.144.5",
            installed_artifact_digest="sha256:" + "d" * 64,
            active_invocation="/Users/test/.vite-plus/bin/codex",
            reachable_invocations=("/Users/test/.vite-plus/bin/codex",),
            observed_at=NOW - timedelta(minutes=1),
        )
        fetcher = FakeFetcher(
            {
                PACKUMENT_URL: response(PACKUMENT_URL, packument()),
                TARBALL_URL: response(TARBALL_URL, TARBALL),
            }
        )
        authority_times = iter((NOW, NOW + timedelta(seconds=1)))
        authority = NpmCodexReleaseAuthority(
            fetcher=fetcher,
            clock=authority_times.__next__,
        )

        resolved = resolve_current_release(
            selected,
            observed,
            authority=authority,
            materializer=NpmCodexArtifactMaterializer(fetcher),
            maximum_age=timedelta(minutes=5),
            resolver_policy_identity="current-online:v1",
            validation_profile_identity="codex-current:v1",
            clock=lambda: NOW + timedelta(seconds=1),
        )
        candidate = build_upgrade_candidate(
            selected,
            observed,
            resolved,
            transition=ReleaseTransitionKind.UPGRADE,
        )

        self.assertEqual(candidate.target_artifact_digest, "sha512:" + TARBALL_SHA512)
        self.assertEqual(resolved.observed_at, NOW + timedelta(seconds=1))
        self.assertEqual(
            [url for url, _limit in fetcher.requests],
            [PACKUMENT_URL, TARBALL_URL, PACKUMENT_URL],
        )

    def test_bounded_fetcher_disables_redirects_and_stops_at_size_limit(self) -> None:
        handler = _RejectRedirects()
        self.assertIsNone(
            handler.redirect_request(
                Request(PACKUMENT_URL),
                None,
                302,
                "Found",
                {},
                "http://127.0.0.1/private",
            )
        )

        opener = FakeOpener(FakeOpenedResponse(PACKUMENT_URL, b"12345"))
        fetcher = BoundedHttpsFetcher(opener=opener)
        with self.assertRaises(OSError):
            fetcher.fetch(PACKUMENT_URL, maximum_bytes=4)
        self.assertEqual(len(opener.requests), 1)


if __name__ == "__main__":
    unittest.main()
