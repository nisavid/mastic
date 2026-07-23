import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path

from mastic.application.current_release import ArtifactMaterialization
from mastic.application.dispatch import ApplicationError
from mastic.domain.external_applications import AuthorityReleaseObservation
from mastic.infrastructure.codex_owner_reconciliation import (
    LocalCodexOwnerReconciliation,
    SetupApplicationReconciliation,
    _CurrentResolver,
)


ViteFixture = import_module("tests.mastic.test_codex_vite_discovery_v1").ViteFixture
NOW = datetime(2026, 7, 21, 8, 0, 2, tzinfo=UTC)


class StaticAuthority:
    def resolve_current(self, _query):
        return AuthorityReleaseObservation(
            exact_release="0.150.0",
            artifact_coordinate="https://registry.npmjs.org/@openai/codex/-/codex-0.150.0.tgz",
            artifact_digest="sha512:" + "a" * 128,
            authority_identity="release-authority:npmjs:@openai/codex:latest",
            response_digest="sha256:" + "b" * 64,
            observed_at=NOW - timedelta(seconds=1),
            valid_until=NOW + timedelta(minutes=10),
        )


class StaticMaterializer:
    def materialize(self, release):
        return ArtifactMaterialization(
            coordinate=release.artifact_coordinate,
            digest=release.artifact_digest,
        )


class Unused:
    def __getattr__(self, name):
        def unexpected(*_arguments, **_parameters):
            raise AssertionError(
                f"dry-run inspection crossed mutation dependency: {name}"
            )

        return unexpected


class CodexMigrationDryRunTests(unittest.TestCase):
    def test_vite_owned_target_fixture_reports_current_upgrade_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = ViteFixture(Path(raw))
            discovery = fixture.discovery()
            current = _CurrentResolver(
                StaticAuthority(), StaticMaterializer(), clock=lambda: NOW
            )
            operation = LocalCodexOwnerReconciliation(
                discovery=discovery,
                current=current,
                closure_materializer=Unused(),
                lifecycle=Unused(),
                store=Unused(),
                planning=Unused(),
                issuer=Unused(),
                remote=Unused(),
                uid=501,
                clock=lambda: NOW,
            )

            result = operation.execute("application.inspect", {"application": "codex"})

            self.assertEqual(result["owner"], "vite-plus/npm-global")
            self.assertEqual(result["installed_version"], "0.144.5")
            self.assertEqual(result["current_version"], "0.150.0")
            self.assertEqual(result["status"], "upgrade")
            self.assertTrue(result["safe_owner_action_available"])
            invoked = tuple(command for command in fixture.runner.calls)
            self.assertNotIn("install", " ".join(" ".join(item) for item in invoked))

    def test_setup_reconciles_vite_codex_and_never_forwards_it_to_legacy_supply(
        self,
    ) -> None:
        codex = SetupCodex()
        legacy = LegacySupply()
        supply = SetupApplicationReconciliation(legacy, codex)

        with self.assertRaises(ApplicationError) as raised:
            supply.execute(
                "application.install",
                {
                    "application_targets": ("codex", "hindsight"),
                    "confirmed": True,
                },
            )

        self.assertEqual(raised.exception.code, "codex_upgrade_required")
        self.assertEqual(legacy.targets, [])
        self.assertEqual(codex.executions, 0)
        self.assertIn("mastic app upgrade codex", raised.exception.next_actions)

    def test_setup_can_explicitly_preserve_outdated_vite_codex(self) -> None:
        legacy = LegacySupply()
        codex = SetupCodex()
        supply = SetupApplicationReconciliation(legacy, codex)

        result = supply.execute(
            "application.install",
            {
                "application_targets": ("codex", "hindsight"),
                "preserve_outdated_codex": True,
                "confirmed": True,
            },
        )

        self.assertEqual(legacy.targets, [("hindsight",)])
        self.assertEqual(result["applications"]["codex"]["version"], "0.144.5")
        self.assertEqual(result["applications"]["codex"]["release_intent"], "exact")
        self.assertTrue(result["applications"]["codex"]["preserved_outdated"])
        self.assertEqual(codex.executions, 0)


class SetupCodex:
    def __init__(self) -> None:
        self.executions = 0

    def inspect(self):
        return {
            "status": "upgrade",
            "owner": "vite-plus/npm-global",
            "installed_version": "0.144.5",
            "current_version": "0.150.0",
        }

    def preview(self, _operation, _parameters):
        return {"preview_fingerprint": "sha256:" + "a" * 64}

    def execute(self, _operation, _parameters):
        self.executions += 1
        return {"target_version": "0.150.0"}


class LegacySupply:
    def __init__(self) -> None:
        self.targets = []

    def execute(self, _operation, parameters):
        targets = tuple(parameters["application_targets"])
        self.targets.append(targets)
        return {
            "applications": {
                "hindsight": {"version": "0.8.4", "provenance": "installed"}
            }
        }

    def inventory(self):
        return {}


if __name__ == "__main__":
    unittest.main()
