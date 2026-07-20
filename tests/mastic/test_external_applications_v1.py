import unittest
from datetime import UTC, datetime, timedelta, timezone

from mastic.application.current_release import (
    ArtifactMaterialization,
    CurrentReleaseAuthorityQuery,
    CurrentReleaseResolutionError,
    ReleaseArtifactUnavailableError,
    ReleaseAuthorityUnavailableError,
    resolve_current_release,
)
from mastic.domain.external_applications import (
    AuthorityReleaseObservation,
    ExternalApplicationInstallation,
    InstallationObservation,
    ReleaseIntent,
    ReleaseIntentKind,
)


NOW = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)


def installation() -> ExternalApplicationInstallation:
    return ExternalApplicationInstallation(
        application_identity="external-application:codex",
        installation_identity="application-installation:codex:vite-npm",
        owner_identity="installation-owner:vite:npm-global",
        release_intent=ReleaseIntent.current(channel="npm:latest"),
        platform="darwin",
        architecture="arm64",
    )


def observation(**changes: object) -> InstallationObservation:
    values: dict[str, object] = {
        "application_identity": "external-application:codex",
        "installation_identity": "application-installation:codex:vite-npm",
        "owner_identity": "installation-owner:vite:npm-global",
        "release_channel": "npm:latest",
        "platform": "darwin",
        "architecture": "arm64",
        "installed_release": "0.144.5",
        "owner_installation_identity": "vite-prefix:/Users/sumi/.vite-plus",
        "installed_artifact_digest": "sha256:" + "b" * 64,
        "active_invocation": "/Users/sumi/.vite-plus/bin/codex",
        "reachable_invocations": (
            "/Users/sumi/.vite-plus/bin/codex",
            "/opt/homebrew/bin/codex",
        ),
        "observed_at": NOW,
    }
    values.update(changes)
    return InstallationObservation(**values)  # type: ignore[arg-type]


def authority_release(
    release: str,
    *,
    digest: str | None = None,
    observed_at: datetime = NOW,
    valid_until: datetime | None = None,
) -> AuthorityReleaseObservation:
    digest = digest or ("sha256:" + release.replace(".", "")[-1] * 64)
    return AuthorityReleaseObservation(
        exact_release=release,
        artifact_coordinate=f"npm:@openai/codex@{release}",
        artifact_digest=digest,
        authority_identity="release-authority:npmjs:@openai/codex",
        response_digest="sha256:" + release.replace(".", "")[-1] * 64,
        observed_at=observed_at,
        valid_until=valid_until,
    )


class SequenceAuthority:
    def __init__(self, releases: list[AuthorityReleaseObservation]) -> None:
        self.releases = releases
        self.requests: list[CurrentReleaseAuthorityQuery] = []

    def resolve_current(
        self, query: CurrentReleaseAuthorityQuery
    ) -> AuthorityReleaseObservation:
        self.requests.append(query)
        if not self.releases:
            raise AssertionError("release authority was read too many times")
        return self.releases.pop(0)


class RecordingMaterializer:
    def __init__(self, *, digest: str | None = None) -> None:
        self.digest = digest
        self.coordinates: list[str] = []

    def materialize(
        self, release: AuthorityReleaseObservation
    ) -> ArtifactMaterialization:
        self.coordinates.append(release.artifact_coordinate)
        return ArtifactMaterialization(
            coordinate=release.artifact_coordinate,
            digest=self.digest or release.artifact_digest,
        )


class UnavailableAuthority:
    def resolve_current(
        self, query: CurrentReleaseAuthorityQuery
    ) -> AuthorityReleaseObservation:
        del query
        raise ReleaseAuthorityUnavailableError("offline")


class UnavailableMaterializer:
    def materialize(
        self, release: AuthorityReleaseObservation
    ) -> ArtifactMaterialization:
        del release
        raise ReleaseArtifactUnavailableError("missing")


class ExternalApplicationIdentityTests(unittest.TestCase):
    def test_release_intent_distinguishes_current_from_exact(self) -> None:
        current = ReleaseIntent.current(channel="npm:latest")
        exact = ReleaseIntent.exact(channel="npm:latest", release="0.144.6")

        self.assertEqual(current.kind, ReleaseIntentKind.CURRENT)
        self.assertIsNone(current.exact_release)
        self.assertEqual(exact.kind, ReleaseIntentKind.EXACT)
        self.assertEqual(exact.exact_release, "0.144.6")

    def test_installation_identity_requires_its_owner_and_native_channel(self) -> None:
        selected = installation()

        self.assertEqual(selected.owner_identity, "installation-owner:vite:npm-global")
        self.assertEqual(selected.release_intent.channel, "npm:latest")

        with self.assertRaisesRegex(ValueError, "installation identity"):
            ExternalApplicationInstallation(
                application_identity="external-application:codex",
                installation_identity="",
                owner_identity="installation-owner:vite:npm-global",
                release_intent=ReleaseIntent.current(channel="npm:latest"),
                platform="darwin",
                architecture="arm64",
            )

    def test_installation_observation_has_a_deterministic_exact_fingerprint(
        self,
    ) -> None:
        first = observation(
            reachable_invocations=(
                "/opt/homebrew/bin/codex",
                "/Users/sumi/.vite-plus/bin/codex",
            )
        )
        second = observation()

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertTrue(first.fingerprint.startswith("sha256:"))
        with self.assertRaisesRegex(ValueError, "active invocation"):
            observation(active_invocation="relative/codex")

    def test_observation_fingerprint_normalizes_equivalent_times_to_utc(self) -> None:
        eastern = timezone(timedelta(hours=-4))

        self.assertEqual(
            observation(observed_at=NOW).fingerprint,
            observation(observed_at=NOW.astimezone(eastern)).fingerprint,
        )

    def test_release_records_require_aware_times_and_sha256_digests(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            authority_release("0.144.6", observed_at=NOW.replace(tzinfo=None))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            AuthorityReleaseObservation(
                exact_release="0.144.6",
                artifact_coordinate="npm:@openai/codex@0.144.6",
                artifact_digest="sha256:short",
                authority_identity="release-authority:npmjs:@openai/codex",
                response_digest="sha256:" + "a" * 64,
                observed_at=NOW,
            )


class CurrentReleaseResolutionTests(unittest.TestCase):
    def test_stable_online_resolution_binds_owner_channel_artifact_and_expiry(
        self,
    ) -> None:
        first = authority_release("0.144.6", observed_at=NOW)
        second = authority_release("0.144.6", observed_at=NOW + timedelta(seconds=2))
        authority = SequenceAuthority([first, second])
        materializer = RecordingMaterializer()

        result = resolve_current_release(
            installation(),
            observation(),
            authority=authority,
            materializer=materializer,
            maximum_age=timedelta(minutes=15),
            resolver_policy_identity="current-online:v1",
            validation_profile_identity="phase1-codex-current:v1",
            clock=lambda: NOW + timedelta(seconds=2),
        )

        self.assertEqual(
            result.installation_identity, installation().installation_identity
        )
        self.assertEqual(
            result.installation_observation_fingerprint, observation().fingerprint
        )
        self.assertEqual(result.owner_identity, installation().owner_identity)
        self.assertEqual(result.release_channel, "npm:latest")
        self.assertEqual(result.exact_release, "0.144.6")
        self.assertEqual(result.artifact_digest, first.artifact_digest)
        self.assertEqual(result.observed_at, second.observed_at)
        self.assertEqual(result.expires_at, second.observed_at + timedelta(minutes=15))
        self.assertEqual(
            authority.requests,
            [
                CurrentReleaseAuthorityQuery(
                    application_identity="external-application:codex",
                    installation_identity=("application-installation:codex:vite-npm"),
                    installation_observation_fingerprint=observation().fingerprint,
                    owner_identity="installation-owner:vite:npm-global",
                    owner_installation_identity=("vite-prefix:/Users/sumi/.vite-plus"),
                    release_channel="npm:latest",
                    platform="darwin",
                    architecture="arm64",
                )
            ]
            * 2,
        )
        self.assertEqual(materializer.coordinates, ["npm:@openai/codex@0.144.6"])
        self.assertNotIn("signature_binding", result.canonical_payload())

    def test_authority_validity_can_shorten_profile_expiry(self) -> None:
        valid_until = NOW + timedelta(minutes=5)
        first = authority_release("0.144.6", valid_until=valid_until)
        second = authority_release("0.144.6", valid_until=valid_until)

        result = resolve_current_release(
            installation(),
            observation(),
            authority=SequenceAuthority([first, second]),
            materializer=RecordingMaterializer(),
            maximum_age=timedelta(minutes=15),
            resolver_policy_identity="current-online:v1",
            validation_profile_identity="phase1-codex-current:v1",
            clock=lambda: NOW,
        )

        self.assertEqual(result.expires_at, valid_until)

    def test_forward_authority_timestamp_fails_with_structured_error(self) -> None:
        first = authority_release("0.144.6", observed_at=NOW)
        second = authority_release("0.144.6", observed_at=NOW + timedelta(seconds=2))

        with self.assertRaises(CurrentReleaseResolutionError) as raised:
            resolve_current_release(
                installation(),
                observation(),
                authority=SequenceAuthority([first, second]),
                materializer=RecordingMaterializer(),
                maximum_age=timedelta(seconds=1),
                resolver_policy_identity="current-online:v1",
                validation_profile_identity="phase1-codex-current:v1",
                clock=lambda: NOW + timedelta(seconds=1),
            )

        self.assertEqual(raised.exception.reason_code, "authority_invalid_response")

    def test_far_future_authority_observation_fails_closed(self) -> None:
        future = authority_release("0.144.6", observed_at=NOW + timedelta(hours=1))

        with self.assertRaises(CurrentReleaseResolutionError) as raised:
            resolve_current_release(
                installation(),
                observation(),
                authority=SequenceAuthority([future, future]),
                materializer=RecordingMaterializer(),
                maximum_age=timedelta(minutes=15),
                resolver_policy_identity="current-online:v1",
                validation_profile_identity="phase1-codex-current:v1",
                clock=lambda: NOW,
            )

        self.assertEqual(raised.exception.reason_code, "authority_invalid_response")

    def test_changed_authority_result_retries_the_complete_observation(self) -> None:
        authority = SequenceAuthority(
            [
                authority_release("0.144.5"),
                authority_release("0.144.6"),
                authority_release("0.144.6"),
                authority_release("0.144.6"),
            ]
        )
        materializer = RecordingMaterializer()

        result = resolve_current_release(
            installation(),
            observation(),
            authority=authority,
            materializer=materializer,
            maximum_age=timedelta(minutes=15),
            resolver_policy_identity="current-online:v1",
            validation_profile_identity="phase1-codex-current:v1",
            max_attempts=2,
            clock=lambda: NOW,
        )

        self.assertEqual(result.exact_release, "0.144.6")
        self.assertEqual(
            materializer.coordinates,
            ["npm:@openai/codex@0.144.5", "npm:@openai/codex@0.144.6"],
        )

    def test_persistent_authority_change_fails_closed_for_this_installation(
        self,
    ) -> None:
        authority = SequenceAuthority(
            [
                authority_release("0.144.5"),
                authority_release("0.144.6"),
                authority_release("0.144.6"),
                authority_release("0.144.7"),
            ]
        )

        with self.assertRaises(CurrentReleaseResolutionError) as raised:
            resolve_current_release(
                installation(),
                observation(),
                authority=authority,
                materializer=RecordingMaterializer(),
                maximum_age=timedelta(minutes=15),
                resolver_policy_identity="current-online:v1",
                validation_profile_identity="phase1-codex-current:v1",
                clock=lambda: NOW,
                max_attempts=2,
            )

        self.assertEqual(raised.exception.reason_code, "authority_unstable")
        self.assertEqual(
            raised.exception.installation_identity,
            installation().installation_identity,
        )

    def test_artifact_digest_mismatch_is_never_a_current_resolution(self) -> None:
        release = authority_release("0.144.6")

        with self.assertRaises(CurrentReleaseResolutionError) as raised:
            resolve_current_release(
                installation(),
                observation(),
                authority=SequenceAuthority([release, release]),
                materializer=RecordingMaterializer(digest="sha256:" + "f" * 64),
                maximum_age=timedelta(minutes=15),
                resolver_policy_identity="current-online:v1",
                validation_profile_identity="phase1-codex-current:v1",
                clock=lambda: NOW,
            )

        self.assertEqual(raised.exception.reason_code, "artifact_mismatch")

    def test_port_availability_failures_remain_distinct_from_instability(self) -> None:
        with self.assertRaises(CurrentReleaseResolutionError) as authority_failure:
            resolve_current_release(
                installation(),
                observation(),
                authority=UnavailableAuthority(),
                materializer=RecordingMaterializer(),
                maximum_age=timedelta(minutes=15),
                resolver_policy_identity="current-online:v1",
                validation_profile_identity="phase1-codex-current:v1",
            )
        self.assertEqual(
            authority_failure.exception.reason_code, "authority_unavailable"
        )

        release = authority_release("0.144.6")
        with self.assertRaises(CurrentReleaseResolutionError) as artifact_failure:
            resolve_current_release(
                installation(),
                observation(),
                authority=SequenceAuthority([release]),
                materializer=UnavailableMaterializer(),
                maximum_age=timedelta(minutes=15),
                resolver_policy_identity="current-online:v1",
                validation_profile_identity="phase1-codex-current:v1",
            )
        self.assertEqual(artifact_failure.exception.reason_code, "artifact_unavailable")

    def test_exact_release_intent_cannot_make_a_currency_claim(self) -> None:
        exact_installation = ExternalApplicationInstallation(
            application_identity="external-application:codex",
            installation_identity="application-installation:codex:vite-npm",
            owner_identity="installation-owner:vite:npm-global",
            release_intent=ReleaseIntent.exact(channel="npm:latest", release="0.144.6"),
            platform="darwin",
            architecture="arm64",
        )

        with self.assertRaises(CurrentReleaseResolutionError) as raised:
            resolve_current_release(
                exact_installation,
                observation(),
                authority=SequenceAuthority([]),
                materializer=RecordingMaterializer(),
                maximum_age=timedelta(minutes=15),
                resolver_policy_identity="current-online:v1",
                validation_profile_identity="phase1-codex-current:v1",
            )

        self.assertEqual(raised.exception.reason_code, "not_current_intent")

    def test_observation_binding_mismatch_fails_before_authority_io(self) -> None:
        authority = SequenceAuthority([])

        with self.assertRaises(CurrentReleaseResolutionError) as raised:
            resolve_current_release(
                installation(),
                observation(owner_identity="installation-owner:npm:ambient"),
                authority=authority,
                materializer=RecordingMaterializer(),
                maximum_age=timedelta(minutes=15),
                resolver_policy_identity="current-online:v1",
                validation_profile_identity="phase1-codex-current:v1",
            )

        self.assertEqual(raised.exception.reason_code, "owner_mismatch")
        self.assertEqual(authority.requests, [])

    def test_invalid_policy_identity_fails_before_any_port_io(self) -> None:
        release = authority_release("0.144.6")
        for field_name in (
            "resolver_policy_identity",
            "validation_profile_identity",
        ):
            with self.subTest(field_name=field_name):
                authority = SequenceAuthority([release, release])
                materializer = RecordingMaterializer()
                arguments = {
                    "resolver_policy_identity": "current-online:v1",
                    "validation_profile_identity": "phase1-codex-current:v1",
                }
                arguments[field_name] = "   "

                with self.assertRaisesRegex(ValueError, "identity"):
                    resolve_current_release(
                        installation(),
                        observation(),
                        authority=authority,
                        materializer=materializer,
                        maximum_age=timedelta(minutes=15),
                        **arguments,
                    )

                self.assertEqual(authority.requests, [])
                self.assertEqual(materializer.coordinates, [])


if __name__ == "__main__":
    unittest.main()
