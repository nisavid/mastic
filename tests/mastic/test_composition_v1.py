from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mastic.application.config_schema import validate_config
from mastic.application.dispatch import OperationRequest
from mastic.infrastructure.composition import compose_application
from mastic.infrastructure.config_store import ConfigStore
from mastic.infrastructure.local_backend import LocalConfigurationMutations
from mastic.infrastructure.model_supply import CacheInventory
from mastic.infrastructure.paths_v1 import MasticPaths
from mastic.infrastructure.state_store import OperationalStateStore


class _Activator:
    def __init__(self) -> None:
        self.calls = 0

    def activate(self):
        self.calls += 1


class _Port:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, operation, parameters):
        self.calls.append((operation, dict(parameters)))
        return {"operation": operation, "state": "complete"}


class _ModelSupply(_Port):
    def search(self, query, *, mode="curated", limit=20):
        return ()

    def inventory(self):
        return CacheInventory((), "local-observed", ())


class _FalseyPort(_Port):
    def __bool__(self) -> bool:
        return False


class _TrackingConfigurationMutations(LocalConfigurationMutations):
    def __init__(self, config_store, state_store) -> None:
        super().__init__(config_store, state_store)
        self.calls = []

    def execute(self, request, preview):
        self.calls.append(request)
        return super().execute(request, preview)


class CompositionTests(unittest.TestCase):
    def test_configuration_mutations_must_share_composed_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )
            config = ConfigStore(paths.config_file, validate_config)
            state = OperationalStateStore(paths.state_db)
            mutations = LocalConfigurationMutations(
                ConfigStore(root / "other.toml", validate_config),
                state,
            )
            port = _Port()

            with self.assertRaisesRegex(ValueError, "must share"):
                compose_application(
                    paths=paths,
                    activator=_Activator(),
                    runtime_supply=port,
                    model_supply=_ModelSupply(),
                    supervisor=port,
                    setup=port,
                    applications=port,
                    application_targets=port,
                    configuration_mutations=mutations,
                    config_store=config,
                    state_store=state,
                )

    def test_configuration_mutations_must_share_composed_state_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )
            config = ConfigStore(paths.config_file, validate_config)
            state = OperationalStateStore(paths.state_db)
            mutations = LocalConfigurationMutations(
                config,
                OperationalStateStore(root / "other.db"),
            )
            port = _Port()

            with self.assertRaisesRegex(ValueError, "must share"):
                compose_application(
                    paths=paths,
                    activator=_Activator(),
                    runtime_supply=port,
                    model_supply=_ModelSupply(),
                    supervisor=port,
                    setup=port,
                    applications=port,
                    application_targets=port,
                    configuration_mutations=mutations,
                    config_store=config,
                    state_store=state,
                )

    def test_configuration_mutations_use_the_composed_stores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )
            paths.prepare()
            config = ConfigStore(paths.config_file, validate_config)
            state = OperationalStateStore(paths.state_db)
            mutations = _TrackingConfigurationMutations(config, state)
            port = _Port()

            composition = compose_application(
                paths=paths,
                activator=_Activator(),
                runtime_supply=port,
                model_supply=_ModelSupply(),
                supervisor=port,
                setup=port,
                applications=port,
                application_targets=port,
                configuration_mutations=mutations,
                config_store=config,
                state_store=state,
            )

            preview = composition.dispatcher.preview(
                OperationRequest("gateway.configure", {"port": 9001})
            )
            composition.dispatcher.execute(
                OperationRequest(
                    "gateway.configure",
                    {
                        "port": 9001,
                        "confirmed": True,
                        "preview_fingerprint": preview.value["preview_fingerprint"],
                    },
                )
            )

            self.assertEqual(
                [call.name for call in mutations.calls], ["gateway.configure"]
            )
            self.assertEqual(config.load().value.gateway.port, 9001)

    def test_public_catalogue_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )
            port = _Port()
            composition = compose_application(
                paths=paths,
                activator=_Activator(),
                runtime_supply=port,
                model_supply=_ModelSupply(),
                supervisor=port,
                setup=port,
                applications=port,
                application_targets=port,
            )

            with self.assertRaises(TypeError):
                composition.catalogue["status"] = composition.catalogue["status"]

    def test_falsey_injected_collaborators_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )
            logs = _FalseyPort()
            metrics = _FalseyPort()
            port = _Port()

            composition = compose_application(
                paths=paths,
                activator=_Activator(),
                runtime_supply=port,
                model_supply=_ModelSupply(),
                supervisor=port,
                setup=port,
                applications=port,
                application_targets=port,
                logs=logs,
                metrics=metrics,
            )

            backend = composition.dispatcher._backend
            self.assertIs(backend._logs, logs)
            self.assertIs(backend._metrics, metrics)

    def test_uninitialized_queries_compose_without_supervisor_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )
            activator = _Activator()
            port = _Port()
            composition = compose_application(
                paths=paths,
                activator=activator,
                runtime_supply=port,
                model_supply=_ModelSupply(),
                supervisor=port,
                setup=port,
                applications=port,
                application_targets=port,
            )

            status = composition.dispatcher.execute(OperationRequest("status"))
            available = composition.dispatcher.execute(
                OperationRequest("runtime.available")
            )

            self.assertEqual(status.value["services"], [])
            self.assertEqual(
                [item["key"] for item in available.value["items"]],
                ["mlx_lm", "mlx_vlm", "optiq"],
            )
            self.assertEqual(activator.calls, 0)
            self.assertEqual(port.calls, [])

    def test_mutation_uses_the_injected_owner_and_explicit_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = MasticPaths(
                root / "config", root / "state", root / "data", root / "logs"
            )
            activator = _Activator()
            runtime = _Port()
            composition = compose_application(
                paths=paths,
                activator=activator,
                runtime_supply=runtime,
                model_supply=_ModelSupply(),
                supervisor=_Port(),
                setup=_Port(),
                applications=_Port(),
                application_targets=_Port(),
            )

            preview = composition.dispatcher.preview(
                OperationRequest("runtime.install", {"runtime": "optiq"})
            )
            result = composition.dispatcher.execute(
                OperationRequest(
                    "runtime.install",
                    {
                        "runtime": "optiq",
                        "confirmed": True,
                        "preview_fingerprint": preview.value["preview_fingerprint"],
                    },
                )
            )

            self.assertEqual(activator.calls, 1)
            self.assertEqual(runtime.calls[0][0], "runtime.install")
            self.assertEqual(result.value["state"], "accepted")


if __name__ == "__main__":
    unittest.main()
