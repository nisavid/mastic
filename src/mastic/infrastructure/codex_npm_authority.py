"""Npm release authority and artifact verification for Vite+-managed Codex."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

from mastic.application.current_release import (
    ArtifactMaterialization,
    CurrentReleaseAuthorityQuery,
    ReleaseArtifactUnavailableError,
    ReleaseAuthorityInvalidResponseError,
    ReleaseAuthorityUnavailableError,
)
from mastic.domain.canonical import canonical_fingerprint
from mastic.domain.external_applications import AuthorityReleaseObservation


_PACKUMENT_URL = "https://registry.npmjs.org/@openai%2Fcodex"
_AUTHORITY_IDENTITY = "release-authority:npmjs:@openai/codex:dist-tag:latest"
_PACKUMENT_MAX_BYTES = 8 * 1024 * 1024
_TARBALL_MAX_BYTES = 64 * 1024 * 1024
_SEMVER_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    final_url: str
    body: bytes


class HttpFetcher(Protocol):
    def fetch(self, url: str, *, maximum_bytes: int) -> HttpResponse: ...


class _UrlOpener(Protocol):
    def open(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
    ): ...


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        del req, fp, code, msg, headers, newurl
        return None


class BoundedHttpsFetcher:
    """Fetch one bounded HTTPS response while exposing its final URL."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        opener: _UrlOpener | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("HTTP timeout must be positive")
        self._timeout = timeout_seconds
        self._opener = opener or urllib.request.build_opener(_RejectRedirects())

    def fetch(self, url: str, *, maximum_bytes: int) -> HttpResponse:
        if maximum_bytes <= 0:
            raise ValueError("maximum response size must be positive")
        _require_https(url)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": (
                    "application/vnd.npm.install-v1+json, "
                    "application/octet-stream;q=0.9"
                ),
                "User-Agent": "mastic-current-release/1",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as opened:
                content_length = opened.headers.get("Content-Length")
                if content_length is not None and int(content_length) > maximum_bytes:
                    raise OSError("HTTP response exceeds the configured size limit")
                body = opened.read(maximum_bytes + 1)
                if len(body) > maximum_bytes:
                    raise OSError("HTTP response exceeds the configured size limit")
                return HttpResponse(
                    status=opened.status,
                    final_url=opened.geturl(),
                    body=body,
                )
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise OSError("bounded HTTPS request failed") from error


class NpmCodexReleaseAuthority:
    """Resolve npm's exact `latest` dist-tag for Vite's Codex installation."""

    def __init__(
        self,
        *,
        fetcher: HttpFetcher | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetcher = fetcher or BoundedHttpsFetcher()
        self._clock = clock

    def resolve_current(
        self, query: CurrentReleaseAuthorityQuery
    ) -> AuthorityReleaseObservation:
        _validate_query(query)
        try:
            response = self._fetcher.fetch(
                _PACKUMENT_URL,
                maximum_bytes=_PACKUMENT_MAX_BYTES,
            )
        except OSError as error:
            raise ReleaseAuthorityUnavailableError(
                "npm release authority is unavailable"
            ) from error
        if response.status != 200:
            raise ReleaseAuthorityUnavailableError(
                "npm release authority returned a non-success response"
            )
        if response.final_url != _PACKUMENT_URL:
            raise ReleaseAuthorityInvalidResponseError(
                "npm release authority redirected unexpectedly"
            )
        selected = _parse_packument(response.body)
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("authority clock must return timezone-aware time")
        return AuthorityReleaseObservation(
            exact_release=selected.version,
            artifact_coordinate=selected.tarball_url,
            artifact_digest=selected.integrity_digest,
            authority_identity=_AUTHORITY_IDENTITY,
            response_digest=canonical_fingerprint(
                {
                    "dist_tag": "latest",
                    "integrity_digest": selected.integrity_digest,
                    "package": "@openai/codex",
                    "tarball_url": selected.tarball_url,
                    "version": selected.version,
                }
            ),
            observed_at=observed_at,
            valid_until=None,
        )


class NpmCodexArtifactMaterializer:
    """Download and verify the exact npm tarball selected by the authority."""

    def __init__(self, fetcher: HttpFetcher | None = None) -> None:
        self._fetcher = fetcher or BoundedHttpsFetcher()

    def materialize(
        self, release: AuthorityReleaseObservation
    ) -> ArtifactMaterialization:
        if release.authority_identity != _AUTHORITY_IDENTITY:
            raise ReleaseArtifactUnavailableError(
                "release did not come from the Codex npm authority"
            )
        try:
            _validate_tarball_url(release.artifact_coordinate)
            algorithm, expected = release.artifact_digest.split(":", 1)
            if algorithm != "sha512" or len(expected) != 128:
                raise ValueError("unsupported npm integrity digest")
            response = self._fetcher.fetch(
                release.artifact_coordinate,
                maximum_bytes=_TARBALL_MAX_BYTES,
            )
        except (OSError, ValueError) as error:
            raise ReleaseArtifactUnavailableError(
                "authority-selected npm artifact is unavailable"
            ) from error
        if response.status != 200 or response.final_url != release.artifact_coordinate:
            raise ReleaseArtifactUnavailableError(
                "authority-selected npm artifact response is not exact"
            )
        actual = hashlib.sha512(response.body).hexdigest()
        if actual != expected:
            raise ReleaseArtifactUnavailableError(
                "authority-selected npm artifact failed integrity verification"
            )
        try:
            _validate_tarball_package(response.body, release.exact_release)
        except (OSError, ValueError, tarfile.TarError) as error:
            raise ReleaseArtifactUnavailableError(
                "authority-selected npm artifact has invalid package identity"
            ) from error
        return ArtifactMaterialization(
            coordinate=release.artifact_coordinate,
            digest=f"sha512:{actual}",
        )


@dataclass(frozen=True, slots=True)
class _SelectedRelease:
    version: str
    tarball_url: str
    integrity_digest: str


def _validate_query(query: CurrentReleaseAuthorityQuery) -> None:
    if (
        query.application_identity != "external-application:codex"
        or query.owner_identity
        not in {"vite-plus/npm-global", "vite-plus/global-package"}
        or query.release_channel != "npm:latest"
        or query.platform != "darwin"
        or query.architecture != "arm64"
    ):
        raise ReleaseAuthorityInvalidResponseError(
            "query is outside the Codex npm authority scope"
        )


def _parse_packument(payload: bytes) -> _SelectedRelease:
    try:
        value = json.loads(payload)
        if not isinstance(value, Mapping) or value.get("name") != "@openai/codex":
            raise ValueError("unexpected npm package")
        tags = value["dist-tags"]
        versions = value["versions"]
        if not isinstance(tags, Mapping) or not isinstance(versions, Mapping):
            raise ValueError("npm package metadata is incomplete")
        version = tags["latest"]
        if not isinstance(version, str) or not _SEMVER_PATTERN.fullmatch(version):
            raise ValueError("npm latest release is not a semantic version")
        package = versions[version]
        if (
            not isinstance(package, Mapping)
            or package.get("name") != "@openai/codex"
            or package.get("version") != version
        ):
            raise ValueError("npm latest release is inconsistent")
        dist = package["dist"]
        if not isinstance(dist, Mapping):
            raise ValueError("npm distribution metadata is missing")
        tarball_url = dist["tarball"]
        integrity = dist["integrity"]
        if not isinstance(tarball_url, str) or not isinstance(integrity, str):
            raise ValueError("npm distribution metadata has invalid types")
        _validate_tarball_url(tarball_url)
        integrity_digest = _parse_sha512_integrity(integrity)
    except (
        KeyError,
        binascii.Error,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise ReleaseAuthorityInvalidResponseError(
            "npm release authority response is invalid"
        ) from error
    return _SelectedRelease(version, tarball_url, integrity_digest)


def _parse_sha512_integrity(value: str) -> str:
    if not value.startswith("sha512-") or " " in value:
        raise ValueError("npm integrity must be one SHA-512 SRI value")
    decoded = base64.b64decode(value.removeprefix("sha512-"), validate=True)
    if len(decoded) != 64:
        raise ValueError("npm integrity has the wrong SHA-512 size")
    return "sha512:" + decoded.hex()


def _validate_tarball_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "registry.npmjs.org"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/@openai/codex/-/codex-")
        or not parsed.path.endswith(".tgz")
    ):
        raise ValueError("npm tarball URL is outside the trusted package scope")


def _require_https(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        raise ValueError("HTTPS URL is required")


def _validate_tarball_package(payload: bytes, expected_version: str) -> None:
    file_count = 0
    total_size = 0
    package_json: bytes | None = None
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive:
            file_count += 1
            total_size += member.size
            if file_count > 20_000 or total_size > 512 * 1024 * 1024:
                raise ValueError("npm package archive exceeds inspection limits")
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("npm package archive contains an unsafe path")
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise ValueError("npm package archive contains an unsafe entry")
            if member.name != "package/package.json":
                continue
            if (
                package_json is not None
                or not member.isfile()
                or member.size > 1_048_576
            ):
                raise ValueError("npm package identity metadata is invalid")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError("npm package identity metadata is unreadable")
            with extracted:
                package_json = extracted.read(1_048_577)
    if package_json is None or len(package_json) > 1_048_576:
        raise ValueError("npm package identity metadata is missing")
    try:
        identity = json.loads(package_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("npm package identity metadata is invalid") from error
    if (
        not isinstance(identity, Mapping)
        or identity.get("name") != "@openai/codex"
        or identity.get("version") != expected_version
    ):
        raise ValueError("npm package identity does not match the selected release")
