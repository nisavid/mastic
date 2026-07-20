# Codex and Hindsight release and upgrade authorities

Research date: 2026-07-20 UTC

This note answers [issue #39](https://github.com/nisavid/mastic/issues/39).
It records the upstream state observed on the research date; version numbers are
evidence, not durable pins. MASTIC must resolve the selected stable channel
again when it builds or applies a closure.

## Executive findings

1. “Newest” is channel- and owner-specific. Codex deliberately checks the
   Homebrew cask when Homebrew owns the executable, and the latest non-draft,
   non-prerelease GitHub Release for standalone and JavaScript-package installs.
   For npm-like installs, Codex additionally checks that the GitHub release's
   version is available from npm before offering the update.
2. The default migration policy should be **upgrade the installation through
   its existing owner to that owner's current stable release**. Keeping an
   older installed version is an explicit, version-pinned alternative, not the
   default. A version captured during development or validation cannot become
   the target version by accident.
3. A Codex executable under Vite+'s Node runtime can still be npm-owned. Vite+
   intercepts `npm install -g`, stores ownership metadata, and links the
   package's executable into its own bin directory. That layout is distinct
   from a package installed with `vp install -g`; the two must not be upgraded
   with the same command.
4. Hindsight is not one installable application. The Rust CLI, Python API or
   embedded daemon, container image, and Helm chart have separate owners and
   upgrade paths even though one `v*` release workflow publishes them together.
5. Integrity and currency are different claims. A digest or Sigstore
   attestation can bind bytes to a release or publishing identity. It cannot
   prove that the release is still newest. An offline closure therefore needs
   both immutable artifact evidence and a time-bounded, authenticated record of
   the channel's latest-version response.
6. Codex's standalone installer verifies SHA-256 digests from GitHub release
   metadata. Hindsight's official standalone CLI installer does not verify a
   digest or signature before replacing the executable, even though GitHub's
   release API publishes an asset digest. MASTIC must add that verification if
   it installs the Hindsight CLI.

## Authority matrix

The registry or release endpoint in the “current-version authority” column is
the final authority for that install owner. A shared source tag explains how
artifacts relate; it does not override a lagging or unavailable package-manager
channel.

| Application and owner | Current-version authority | Supported upgrade | Installed-version proof | Immutable integrity evidence | Rollback mechanism |
| --- | --- | --- | --- | --- | --- |
| Codex standalone installer | GitHub [`releases/latest`](https://api.github.com/repos/openai/codex/releases/latest) | Rerun the [official installer](https://github.com/openai/codex/blob/main/scripts/install/install.sh); no release argument means latest | `codex --version`; `codex doctor --json` also reports install context and update state | GitHub asset `digest`; installer validates SHA-256 before activation | Rerun with an explicit older `--release`; retained standalone release directories make repointing possible, but there is no documented rollback command |
| Codex npm | GitHub latest, gated on the matching version being ready in the npm registry | `npm install -g @openai/codex@latest` | `codex --version`, package-manager metadata, and `codex doctor --json` | npm `dist.integrity` and `dist.shasum`; the package manager performs its normal integrity checks | Explicit versioned npm install; this is a manual opt-in downgrade, not an automatic fallback |
| Codex bun or pnpm | GitHub latest, gated on the matching npm package release | `bun install -g @openai/codex@latest` or `pnpm add -g @openai/codex@latest` | `codex --version`, owner metadata, and `codex doctor --json` | Registry integrity metadata through the package manager | Explicit versioned package install; no Codex rollback command |
| Codex Homebrew cask | Homebrew's [`codex.json`](https://formulae.brew.sh/api/cask/codex.json), which may lag GitHub | `brew upgrade --cask codex` | `codex --version`, `brew info --cask codex`, and `codex doctor --json` | Cask SHA-256 | Homebrew version rollback is not documented by Codex; restoring an old cask is outside its update contract |
| Hindsight Rust CLI | Hindsight GitHub [`releases/latest`](https://api.github.com/repos/vectorize-io/hindsight/releases/latest) | Rerun the [official CLI installer](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/static/get-cli) | `hindsight --version` (not `hindsight version`, which queries the API) | GitHub asset `digest`; installer does not validate it | Set `HINDSIGHT_CLI_VERSION` and reinstall an exact old release; overwrite is not transactional and the installer keeps no backup |
| Hindsight Python API | PyPI project selected by the installation: [`hindsight-api`](https://pypi.org/project/hindsight-api/), [`hindsight-api-slim`](https://pypi.org/project/hindsight-api-slim/), or wrapper bundle | Upgrade with the existing Python environment/tool owner, using the same distribution variant | `importlib.metadata.version("<distribution>")`; a running API exposes `GET /version` | PyPI publishes per-file hashes and provenance attestations for these trusted-publishing releases | Reinstalling an old package is insufficient after a schema migration; restore a verified pre-upgrade data backup with a compatible application version |
| Hindsight embedded daemon | PyPI [`hindsight-embed`](https://pypi.org/project/hindsight-embed/) | Official docs recommend `uvx hindsight-embed@latest`; persistent `pipx` or other installs must be upgraded by that same owner | Owner metadata or `importlib.metadata.version("hindsight-embed")`; also verify the spawned API's `GET /version` | PyPI file hashes and provenance attestation | Stop the daemon, restore compatible package and data state; no documented one-command rollback |
| Hindsight container | Exact GHCR digest behind the selected semver tag; moving `latest` tags are discovery aliases | Pull and activate the chosen current semver digest through the existing container owner | Inspect running image digest and call API `GET /version` | Images are keylessly signed with Cosign; official docs provide the workflow identity and OIDC issuer | Reactivate the recorded old image digest only with a compatible database snapshot/schema |
| Hindsight Helm chart | OCI chart `oci://ghcr.io/vectorize-io/charts/hindsight` | `helm upgrade`; pin the resolved chart version in a closure | `helm list`/release metadata plus API `GET /version` | OCI manifest digest; deployed images additionally have Cosign signatures | Helm rollback can restore manifests, but not reverse database migrations; data rollback remains separate |

## Codex

### Release discovery and supported owners

The [Codex README](https://github.com/openai/codex/blob/main/README.md)
documents four installation paths: the standalone shell installer, npm,
Homebrew, and direct downloads from the latest GitHub Release. Current Codex
source recognizes standalone, npm, bun, pnpm, and Homebrew install contexts;
anything else is `Other`.

The [update checker](https://github.com/openai/codex/blob/main/codex-rs/tui/src/updates.rs)
uses:

- the Homebrew cask API for a Homebrew installation because that channel can
  lag the GitHub release;
- the latest GitHub Release for standalone and JavaScript-package installs;
- an additional npm-registry readiness check before it advertises the GitHub
  version to npm, bun, or pnpm users.

The corresponding
[update actions](https://github.com/openai/codex/blob/main/codex-rs/tui/src/update_action.rs)
are owner-preserving. This matters operationally: replacing an npm-owned Codex
with a second standalone installation creates ambiguous path ownership instead
of upgrading the application the user actually invokes.

At 2026-07-20 UTC, the official stable authorities agreed on Codex `0.144.6`:

- GitHub's latest release was
  [`rust-v0.144.6`](https://github.com/openai/codex/releases/tag/rust-v0.144.6),
  published 2026-07-18;
- npm's `@openai/codex` `latest` distribution was `0.144.6` and exposed a
  SHA-512 `dist.integrity` value;
- the Homebrew cask was `0.144.6` and referenced the Apple Silicon release
  archive by SHA-256.

That agreement is an observation, not a guarantee. A migration must query the
authority for the detected owner at apply time.

### Standalone activation, verification, and recovery

The [standalone installer](https://github.com/openai/codex/blob/main/scripts/install/install.sh)
defaults to `latest`, resolves it through GitHub's latest-release endpoint, and
supports an explicit version through `--release` or `CODEX_RELEASE`. It places
immutable version directories under Codex's standalone package directory,
atomically repoints a `current` symlink, and links the visible executable into
the configured install directory. It validates the selected asset against a
SHA-256 digest before activation.

The retained version directories provide useful recovery material, but the
installer does not expose a named rollback operation. The supported primitive
is another installer run selecting an exact version. MASTIC should record the
pre-upgrade executable path, owner, version, and digest; it should not silently
downgrade to that version when a latest-version upgrade fails.

Use `codex --version` for the running executable's version. Current source also
provides `codex doctor --json`; its
[update diagnostic](https://github.com/openai/codex/blob/main/codex-rs/cli/src/doctor/updates.rs)
reports install context and latest-version status and, for npm, checks that the
running package root matches the active global npm root. This catches the
common failure mode in which an update command modifies a different Codex
installation from the one on `PATH`.

### Vite+-managed npm installation

Vite+ documents two global-package modes:

- [`vp install -g` and `vp update -g`](https://viteplus.dev/guide/install),
  whose packages live in Vite+'s own package store; and
- global npm commands executed through Vite+'s npm shim.

They are different ownership records. Vite+'s
[`BinConfig`](https://github.com/voidzero-dev/vite-plus/blob/main/crates/vite_global_cli/src/commands/env/bin_config.rs)
labels binaries from the second mode with source `npm`. Its
[npm dispatch](https://github.com/voidzero-dev/vite-plus/blob/main/crates/vite_global_cli/src/shim/dispatch.rs)
runs the real `npm install -g`, links installed package binaries into Vite+'s
bin directory, and records the package and Node version. `vp env which <tool>`
then reports the resolved path, package, source, and Node version; for an npm
source it explicitly advises `npm install -g <package>` to recreate a missing
link.

Consequently, a Codex path shaped like
`VP_HOME/bin/codex -> VP_HOME/js_runtime/node/<version>/bin/codex` is evidence
for the npm-interception mode, not enough evidence for `vp install -g`. Before
mutation, confirm it with:

```console
vp env which codex
command -v codex
readlink "$(command -v codex)"
npm root -g
codex --version
codex doctor --json
```

If Vite+ reports `Source: npm` and the doctor confirms the running package root,
the owner-preserving default upgrade is:

```console
npm install -g @openai/codex@latest
```

Run it in the same environment in which `npm` resolves through Vite+. Then
repeat every discovery command and require the executable path, Vite+ metadata,
npm global root, running version, and Codex doctor result to agree. Use
`vp update -g @openai/codex` only when Vite+ reports that the package was
installed by `vp install -g`.

## Hindsight

### One release, several installation owners

Hindsight's
[release workflow](https://github.com/vectorize-io/hindsight/blob/main/.github/workflows/release.yml)
runs for a `v*` tag and publishes Python distributions, npm packages, the Rust
CLI, container images, a Helm chart, and a GitHub Release. The tag coordinates
the release, but the active installation owner still determines how to resolve,
install, and prove an upgrade.

At 2026-07-20 UTC, GitHub's latest stable release was
[`v0.8.4`](https://github.com/vectorize-io/hindsight/releases/tag/v0.8.4),
published 2026-07-01. PyPI reported `0.8.4` for `hindsight-api` and
`hindsight-embed`, and the repository's Helm chart declared `0.8.4`. Those
channels can diverge during a partial or delayed release, so MASTIC must check
the authority for every component it intends to upgrade.

### Rust CLI

The [CLI documentation](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs/sdks/cli.md)
uses the official shell installer. The installer resolves GitHub's latest
release unless `HINDSIGHT_CLI_VERSION` selects an exact version, downloads the
platform binary, and moves it over the existing executable. It preserves the
CLI configuration in the user's Hindsight directory because it only replaces
the binary.

The installer's security boundary is incomplete: the
[script](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/static/get-cli)
does not retrieve or validate a checksum, signature, or attestation. GitHub's
release API nevertheless exposed this SHA-256 digest for the `v0.8.4` Apple
Silicon CLI asset on the observation date:

```text
defe5d281f79098bbda54ab7c51e8c47575d15e33cdfffb1713ac48e182192df
```

MASTIC should fetch release metadata over authenticated TLS, require a
`sha256:` asset digest, download to a temporary file, validate it, stage the old
binary for recovery, and only then replace the executable atomically. This is a
product hardening step around an official artifact, not behavior supplied by
the upstream installer.

`hindsight --version` is the Rust CLI version because the CLI uses Clap's
package-version support. `hindsight version` calls the configured server and
reports the API version; it does not prove which CLI binary ran.

### Python API and embedded daemon

The [installation guide](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs/developer/installation.md)
documents `hindsight-api` and `hindsight-api-slim` as distinct bare-metal
distributions. The
[embedded-daemon guide](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs/sdks/embed.md)
recommends `uvx hindsight-embed@latest` for ephemeral latest-version use and
`pipx install hindsight-embed` for a persistent install. An inventory entry for
`hindsight-embed` therefore does not establish ownership of a separately
installed Rust `hindsight` CLI, and vice versa.

Upgrade each persistent Python distribution through the tool or environment
that owns it. Verify the distribution version through that owner's metadata or
Python's `importlib.metadata`; verify a running service through its documented
`GET /version` endpoint. For the embedded product, require both the
`hindsight-embed` package version and the spawned API version, because the
wrapper manages another process.

Hindsight runs Alembic migrations on API startup by default. The
[admin CLI](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs/developer/admin-cli.md)
can run them explicitly with `hindsight-admin run-db-migration`, and automatic
migrations can be disabled for a controlled deployment. The source states that
migrations must remain backward-compatible for rolling deployments, but that
is not a documented general downgrade guarantee. Migrations only target the
latest schema head; no supported schema-downgrade procedure was found.

Before an API or embedded-daemon upgrade, take and validate a data backup using
the existing compatible version. The admin backup is transactionally
consistent; restore deletes the target schema before import. A safe rollback is
therefore a coordinated recovery operation: stop the upgraded application,
restore a pre-upgrade data snapshot into a controlled target, activate the
compatible prior application artifact, and run representative recall checks.
Merely reinstalling an old wheel after the new version migrated the database is
not sufficient evidence of safety.

### Containers and Helm

The Hindsight installation guide documents moving `latest` image tags and exact
semver tags. Release images are signed keylessly with Cosign, and the guide
provides both the allowed GitHub Actions workflow identity and the GitHub OIDC
issuer. A closure should resolve a stable semver tag online, verify the
signature, record the content digest, and deploy by digest. The moving tag is a
discovery input, not an offline identity.

The Helm guide supports an exact chart version and `helm upgrade`. Helm release
history can restore Kubernetes manifests, but it cannot undo a database
migration. Chart rollback and data rollback must remain separate plans.

## Integrity, provenance, and offline currency

### Available evidence

At the observation time:

- GitHub release assets exposed SHA-256 `digest` fields for the Codex Apple
  Silicon archive and the Hindsight Apple Silicon CLI.
- npm exposed SHA-512 `dist.integrity` for `@openai/codex@0.144.6`.
- Homebrew exposed the Codex cask archive's SHA-256.
- PyPI exposed SHA-256 hashes for Hindsight wheels and source distributions;
  Hindsight publishes through GitHub Actions trusted publishing, and provenance
  attestations were available for the Python release artifacts.
- Hindsight container images were keylessly signed with Cosign. The official
  verification command binds the signature to the Hindsight release workflow
  identity and GitHub's Actions OIDC issuer.
- No first-party signature or attestation was found for the standalone
  Hindsight CLI asset. Its GitHub SHA-256 digest is integrity metadata, not an
  independent publisher-identity proof.
- Codex releases included Sigstore sidecars for some artifacts, but no macOS
  Codex sidecar was found for the observed release. The macOS closure should
  rely only on evidence actually attached to its selected asset.

### Required offline closure record

An offline machine cannot independently prove “this is still newest.” MASTIC
can make the claim auditable and time-bounded by creating a signed closure
manifest online with:

- application, install owner, channel, platform, architecture, and selected
  stable-version policy;
- the authoritative endpoint, complete response digest, observed version,
  publication time, and observation time;
- exact artifact URL or registry coordinate, content digest, size, and any
  package-manager integrity value;
- verified signature or attestation identity, issuer, and transparency-log
  evidence when the channel supplies it;
- MASTIC's own manifest signature and an explicit maximum observation age;
- the pre-upgrade path, owner, installed version, and artifact digest;
- application-specific backup, health, and rollback instructions.

At apply time, online mode should re-resolve the authority and refuse a stale
closure if a newer stable release exists. Offline mode can verify artifact and
manifest signatures and enforce the observation-age policy, but it must report
the result as “latest observed at *time*,” never as an unqualified current
version. Provenance proves who produced bytes; it does not prove freshness,
compatibility, or successful activation.

## Facts, inferences, and unresolved questions

### Directly supported facts

- Codex's official update action preserves the detected install owner.
- Codex uses a separate Homebrew authority and gates npm-like updates on npm
  package readiness.
- Vite+ distinguishes its own global package store from intercepted global npm
  installs and records that distinction per binary.
- Hindsight publishes multiple independently installed artifacts from one
  tagged workflow.
- Hindsight API startup applies forward migrations by default; the admin
  interface can separate migration from startup.
- The Hindsight CLI installer overwrites its binary without checking the asset
  digest that GitHub publishes.

### Design inferences

- The default desired state must be a channel selector such as “current stable
  for detected owner,” not a version literal copied from a test fixture,
  development lock, or earlier validation run.
- MASTIC should refuse an owner-changing upgrade unless the migration plan
  explicitly selects and explains that transition.
- A Vite+ symlink into a Node runtime strongly suggests npm interception, but
  `vp env which`, npm root metadata, and Codex doctor must confirm it before
  mutation.
- Reliable Hindsight API rollback requires application and data recovery as one
  operation even though individual Alembic migrations are intended to support
  rolling upgrades.
- Offline currency requires a freshness policy and MASTIC-signed observation
  record; upstream hashes or attestations alone cannot provide it.

### Unresolved questions for implementation

1. What maximum age should an offline “latest observed” attestation permit for
   Codex and Hindsight, and may an operator override expiry interactively?
2. Should MASTIC vendor the already-verified current artifacts into a closure,
   or permit a target to download them only when the authority response and
   digest can be revalidated online?
3. Should a Hindsight API upgrade always disable startup migrations and run
   `hindsight-admin run-db-migration` as a separate gated step, or only for
   non-embedded/production profiles?
4. What backup artifact and restore rehearsal constitute sufficient proof for
   an embedded pg0 Hindsight installation? The upstream admin guide documents
   logical backup and restore, but the target-specific storage topology still
   needs discovery.
5. Vite+ ownership should be confirmed on the target with `vp env which codex`
   and its per-binary metadata. If the metadata is missing or contradicts the
   symlink, MASTIC needs an explicit repair/migration decision rather than an
   inferred upgrade command.

## First-party source set

- OpenAI Codex: [README](https://github.com/openai/codex/blob/main/README.md),
  [standalone installer](https://github.com/openai/codex/blob/main/scripts/install/install.sh),
  [install-context detection](https://github.com/openai/codex/blob/main/codex-rs/install-context/src/lib.rs),
  [update discovery](https://github.com/openai/codex/blob/main/codex-rs/tui/src/updates.rs),
  [update actions](https://github.com/openai/codex/blob/main/codex-rs/tui/src/update_action.rs),
  and [doctor update diagnostics](https://github.com/openai/codex/blob/main/codex-rs/cli/src/doctor/updates.rs).
- Vite+: [global install guide](https://viteplus.dev/guide/install),
  [per-binary owner metadata](https://github.com/voidzero-dev/vite-plus/blob/main/crates/vite_global_cli/src/commands/env/bin_config.rs),
  [`vp env which`](https://github.com/voidzero-dev/vite-plus/blob/main/crates/vite_global_cli/src/commands/env/which.rs),
  and [npm shim dispatch](https://github.com/voidzero-dev/vite-plus/blob/main/crates/vite_global_cli/src/shim/dispatch.rs).
- Hindsight: [installation guide](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs/developer/installation.md),
  [CLI guide](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs/sdks/cli.md),
  [CLI installer](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/static/get-cli),
  [embedded-daemon guide](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs/sdks/embed.md),
  [admin CLI](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs/developer/admin-cli.md),
  [migration implementation](https://github.com/vectorize-io/hindsight/blob/main/hindsight-api-slim/hindsight_api/migrations.py),
  and [release workflow](https://github.com/vectorize-io/hindsight/blob/main/.github/workflows/release.yml).
- Live first-party registries observed on the research date: Codex
  [GitHub latest release](https://api.github.com/repos/openai/codex/releases/latest),
  [npm package](https://www.npmjs.com/package/@openai/codex), and
  [Homebrew cask](https://formulae.brew.sh/api/cask/codex.json); Hindsight
  [GitHub latest release](https://api.github.com/repos/vectorize-io/hindsight/releases/latest),
  [`hindsight-api` on PyPI](https://pypi.org/project/hindsight-api/), and
  [`hindsight-embed` on PyPI](https://pypi.org/project/hindsight-embed/).
