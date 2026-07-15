"""Version-aware runtime discovery, installation, and launch preparation."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence
from uuid import uuid4


class RuntimeSupplyError(ValueError):
    """A runtime supply operation cannot satisfy its contract."""


class UnsupportedLaunchOption(RuntimeSupplyError):
    """A launch option was not observed on an exact installation."""


@dataclass(frozen=True)
class OptionDefinition:
    """One semantic launch option and its runtime-specific flags."""

    name: str
    flag: str
    false_flag: str | None = None
    repeatable: bool = False


@dataclass(frozen=True)
class RuntimeDefinition:
    """Built-in knowledge about one supported runtime family."""

    key: str
    display_name: str
    package: str
    launcher: tuple[str, ...]
    options: Mapping[str, OptionDefinition]


@dataclass(frozen=True)
class TestedRuntimeBundle:
    """An exact, independently verified runtime lock supplied to mlxctl."""

    bundle_id: str
    runtime: str
    version: str
    python: str
    platform: str
    lock_path: str
    lock_sha256: str


@dataclass(frozen=True)
class RuntimeInstallation:
    """One exact runtime installation and its observed capabilities."""

    installation_id: str
    runtime: str
    version: str
    provenance: str
    root: Path
    launcher: tuple[str, ...]
    capabilities: frozenset[str]
    bundle_id: str | None = None


@dataclass(frozen=True)
class RuntimeProbeResult:
    """Version and launch surface observed inside one environment."""

    version: str
    launcher_relative: tuple[str, ...]
    supported_flags: frozenset[str]


class CommandRunner(Protocol):
    """Execute one argv without shell interpretation."""

    def run(self, argv: tuple[str, ...]) -> None: ...


class RuntimeProbe(Protocol):
    """Inspect an environment without relying on global PATH state."""

    def probe(
        self, definition: RuntimeDefinition, root: Path
    ) -> RuntimeProbeResult: ...


class SubprocessCommandRunner:
    """Run an exact argv directly, never through a shell."""

    def run(self, argv: tuple[str, ...]) -> None:
        subprocess.run(argv, check=True, shell=False)


@dataclass(frozen=True)
class RuntimeCatalogue:
    """Discoverable definitions plus an injected set of tested bundles."""

    definitions: tuple[RuntimeDefinition, ...]
    tested_bundles: tuple[TestedRuntimeBundle, ...] = ()

    @classmethod
    def load_builtin(
        cls, *, tested_bundles: tuple[TestedRuntimeBundle, ...] | None = None
    ) -> RuntimeCatalogue:
        resources = files("mlxctl.runtime_definitions")
        payload = json.loads(
            resources.joinpath("definitions.json").read_text(encoding="utf-8")
        )
        definitions = []
        for item in payload["runtimes"]:
            options = {
                option["name"]: OptionDefinition(
                    name=option["name"],
                    flag=option["flag"],
                    false_flag=option.get("false_flag"),
                    repeatable=option.get("repeatable", False),
                )
                for option in item["options"]
            }
            definitions.append(
                RuntimeDefinition(
                    key=item["key"],
                    display_name=item["display_name"],
                    package=item["package"],
                    launcher=tuple(item["launcher"]),
                    options=MappingProxyType(options),
                )
            )
        if tested_bundles is None:
            bundle_payload = json.loads(
                resources.joinpath("bundles.json").read_text(encoding="utf-8")
            )
            tested_bundles = tuple(
                TestedRuntimeBundle(
                    bundle_id=item["bundle_id"],
                    runtime=item["runtime"],
                    version=item["version"],
                    python=item["python"],
                    platform=item["platform"],
                    lock_path=str(resources.joinpath("locks").joinpath(item["lock"])),
                    lock_sha256=item["lock_sha256"],
                )
                for item in bundle_payload["bundles"]
            )
        return cls(tuple(definitions), tested_bundles)

    def definition(self, key: str) -> RuntimeDefinition:
        for definition in self.definitions:
            if definition.key == key:
                return definition
        raise KeyError(f"unknown runtime definition: {key}")

    def normalize_capabilities(
        self, runtime: str, supported_flags: set[str] | frozenset[str]
    ) -> frozenset[str]:
        definition = self.definition(runtime)
        return frozenset(
            name
            for name, option in definition.options.items()
            if option.flag in supported_flags
            or (option.false_flag is not None and option.false_flag in supported_flags)
        )


class RuntimeLaunchBuilder:
    """Build exact argv from capabilities probed on an installation."""

    def __init__(self, catalogue: RuntimeCatalogue) -> None:
        self._catalogue = catalogue

    def build(
        self,
        installation: RuntimeInstallation,
        *,
        model: str,
        host: str,
        port: int,
        options: Mapping[str, object] | None = None,
    ) -> tuple[str, ...]:
        definition = self._catalogue.definition(installation.runtime)
        requested: dict[str, object] = {
            "model": model,
            "host": host,
            "port": port,
            **dict(options or {}),
        }
        argv = list(installation.launcher)
        for name, value in requested.items():
            if name not in definition.options:
                raise UnsupportedLaunchOption(
                    f"runtime '{installation.runtime}' does not define launch option "
                    f"'{name}'"
                )
            if name not in installation.capabilities:
                raise UnsupportedLaunchOption(
                    f"runtime installation '{installation.installation_id}' does not "
                    f"support launch option '{name}'"
                )
            _append_option(argv, definition.options[name], value)
        return tuple(argv)


class RuntimeManager:
    """Create immutable uv environments and adopt externally managed ones."""

    def __init__(
        self,
        catalogue: RuntimeCatalogue,
        *,
        runner: CommandRunner,
        probe: RuntimeProbe,
        staging_token: Callable[[], str] | None = None,
    ) -> None:
        self._catalogue = catalogue
        self._runner = runner
        self._probe = probe
        self._staging_token = staging_token or (lambda: uuid4().hex)

    def install_tested(
        self, bundle_id: str, installation_root: Path
    ) -> RuntimeInstallation:
        bundle = self._bundle(bundle_id)
        lock_path = Path(bundle.lock_path)
        actual_hash = sha256(lock_path.read_bytes()).hexdigest()
        if actual_hash != bundle.lock_sha256:
            raise RuntimeSupplyError(
                f"tested bundle '{bundle.bundle_id}' lock integrity mismatch"
            )
        return self._install(
            installation_id=bundle.bundle_id,
            runtime=bundle.runtime,
            expected_version=bundle.version,
            python=bundle.python,
            installation_root=installation_root,
            provenance="tested",
            bundle_id=bundle.bundle_id,
            install_argv=lambda stage: (
                "uv",
                "pip",
                "sync",
                "--python",
                str(stage / "bin/python"),
                str(lock_path),
            ),
        )

    def install_custom(
        self,
        runtime: str,
        version: str,
        *,
        python: str,
        installation_root: Path,
    ) -> RuntimeInstallation:
        definition = self._catalogue.definition(runtime)
        _validate_component(version, "runtime version")
        installation_id = f"{runtime}-{version}-custom"
        return self._install(
            installation_id=installation_id,
            runtime=runtime,
            expected_version=version,
            python=python,
            installation_root=installation_root,
            provenance="custom",
            bundle_id=None,
            install_argv=lambda stage: (
                "uv",
                "pip",
                "install",
                "--python",
                str(stage / "bin/python"),
                f"{definition.package}=={version}",
            ),
        )

    def adopt_custom(self, runtime: str, root: Path) -> RuntimeInstallation:
        definition = self._catalogue.definition(runtime)
        resolved_root = root.expanduser().resolve()
        if not resolved_root.is_dir():
            raise FileNotFoundError(f"runtime environment does not exist: {root}")
        result = self._probe.probe(definition, resolved_root)
        _validate_component(result.version, "probed runtime version")
        return self._installation(
            installation_id=f"{runtime}-{result.version}-adopted",
            runtime=runtime,
            expected_version=result.version,
            provenance="adopted",
            bundle_id=None,
            root=resolved_root,
            result=result,
        )

    def _install(
        self,
        *,
        installation_id: str,
        runtime: str,
        expected_version: str,
        python: str,
        installation_root: Path,
        provenance: str,
        bundle_id: str | None,
        install_argv: Callable[[Path], tuple[str, ...]],
    ) -> RuntimeInstallation:
        _validate_component(installation_id, "installation ID")
        definition = self._catalogue.definition(runtime)
        root = installation_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        final = root / installation_id
        if final.exists():
            raise FileExistsError(f"immutable installation already exists: {final}")
        staging_token = self._staging_token()
        _validate_component(staging_token, "staging token")
        stage = root / f".{installation_id}.staging-{staging_token}"
        if stage.exists():
            raise FileExistsError(f"runtime staging path already exists: {stage}")
        try:
            self._runner.run(("uv", "venv", "--python", python, str(stage)))
            self._runner.run(install_argv(stage))
            result = self._probe.probe(definition, stage)
            installation = self._installation(
                installation_id=installation_id,
                runtime=runtime,
                expected_version=expected_version,
                provenance=provenance,
                bundle_id=bundle_id,
                root=final,
                result=result,
            )
            stage.replace(final)
            return installation
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise

    def _installation(
        self,
        *,
        installation_id: str,
        runtime: str,
        expected_version: str,
        provenance: str,
        bundle_id: str | None,
        root: Path,
        result: RuntimeProbeResult,
    ) -> RuntimeInstallation:
        if result.version != expected_version:
            raise RuntimeSupplyError(
                f"runtime probe reported version {result.version!r}; expected "
                f"{expected_version!r}"
            )
        if not result.launcher_relative:
            raise RuntimeSupplyError("runtime probe returned no launcher")
        relative = Path(result.launcher_relative[0])
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeSupplyError("runtime probe returned an unsafe launcher path")
        definition = self._catalogue.definition(runtime)
        launcher = (str(root / relative), *result.launcher_relative[1:])
        return RuntimeInstallation(
            installation_id=installation_id,
            runtime=runtime,
            version=result.version,
            provenance=provenance,
            root=root,
            launcher=launcher,
            capabilities=self._catalogue.normalize_capabilities(
                definition.key, result.supported_flags
            ),
            bundle_id=bundle_id,
        )

    def _bundle(self, bundle_id: str) -> TestedRuntimeBundle:
        for bundle in self._catalogue.tested_bundles:
            if bundle.bundle_id == bundle_id:
                return bundle
        raise KeyError(f"unknown tested runtime bundle: {bundle_id}")


@dataclass(frozen=True)
class RuntimeChangePlan:
    """A safe, inspectable transition plan; it does not mutate state."""

    operation: str
    allowed: bool
    current_installation: str
    target_installation: str | None
    referenced_services: tuple[str, ...]
    steps: tuple[str, ...]


class RuntimeChangePlanner:
    """Plan reference-aware update, rollback, and removal transitions."""

    def plan_update(
        self,
        current: RuntimeInstallation,
        target: RuntimeInstallation,
        *,
        referenced_services: tuple[str, ...] = (),
    ) -> RuntimeChangePlan:
        self._same_runtime(current, target)
        services = tuple(sorted(referenced_services))
        return RuntimeChangePlan(
            operation="update",
            allowed=True,
            current_installation=current.installation_id,
            target_installation=target.installation_id,
            referenced_services=services,
            steps=(
                f"validate {target.installation_id} with {', '.join(services) or 'no services'}",
                f"switch referenced services to {target.installation_id}",
                f"retain {current.installation_id} for rollback",
            ),
        )

    def plan_rollback(
        self,
        current: RuntimeInstallation,
        previous: RuntimeInstallation,
        *,
        referenced_services: tuple[str, ...] = (),
    ) -> RuntimeChangePlan:
        self._same_runtime(current, previous)
        services = tuple(sorted(referenced_services))
        return RuntimeChangePlan(
            operation="rollback",
            allowed=True,
            current_installation=current.installation_id,
            target_installation=previous.installation_id,
            referenced_services=services,
            steps=(
                f"validate {previous.installation_id} with {', '.join(services) or 'no services'}",
                f"switch referenced services to {previous.installation_id}",
                f"retain {current.installation_id} until rollback is verified",
            ),
        )

    def plan_remove(
        self,
        installation: RuntimeInstallation,
        *,
        referenced_services: tuple[str, ...] = (),
    ) -> RuntimeChangePlan:
        services = tuple(sorted(referenced_services))
        if services:
            steps = (
                f"reassign referenced services: {', '.join(services)}",
                f"remove {installation.installation_id} only after references reach zero",
            )
        else:
            steps = (f"remove immutable environment {installation.root}",)
        return RuntimeChangePlan(
            operation="remove",
            allowed=not services,
            current_installation=installation.installation_id,
            target_installation=None,
            referenced_services=services,
            steps=steps,
        )

    @staticmethod
    def _same_runtime(
        current: RuntimeInstallation, target: RuntimeInstallation
    ) -> None:
        if current.runtime != target.runtime:
            raise RuntimeSupplyError("runtime transitions require the same definition")


_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")


def _validate_component(value: str, label: str) -> None:
    if not _SAFE_COMPONENT.fullmatch(value):
        raise RuntimeSupplyError(f"invalid {label}: {value!r}")


def _append_option(argv: list[str], option: OptionDefinition, value: object) -> None:
    if isinstance(value, bool):
        if value:
            argv.append(option.flag)
        elif option.false_flag is not None:
            argv.append(option.false_flag)
        return
    if option.repeatable:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise RuntimeSupplyError(
                f"launch option '{option.name}' requires a sequence of values"
            )
        for item in value:
            argv.extend((option.flag, str(item)))
        return
    argv.extend((option.flag, str(value)))
