#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR=${0:A:h}
readonly REPOSITORY_ROOT=${SCRIPT_DIR:h}
readonly UV_VERSION='0.11.29'
readonly UV_SHA256='61c04acc52a33ef0f331e494bdfbedcdb6c26c6970c022ed3699e5860f8930e3'
readonly PYTHON_VERSION='3.11.15'
readonly CODEX_VERSION='0.144.1'
readonly CODEX_SHA256='88e72ac8bd30815f7d18e62dac333dc20ce3ad1cba94be1649a1977dd9bfdbb8'
readonly CODEX_URL="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-aarch64-apple-darwin.tar.gz"
readonly HINDSIGHT_VERSION='0.8.4'
readonly HINDSIGHT_SHA256='defe5d281f79098bbda54ab7c51e8c47575d15e33cdfffb1713ac48e182192df'
readonly HINDSIGHT_URL="https://github.com/vectorize-io/hindsight/releases/download/v${HINDSIGHT_VERSION}/hindsight-darwin-arm64"

fail() {
  emulate -L zsh
  print -ru2 -- "mastic closure: $1"
  return 1
}

sha256() {
  emulate -L zsh
  local actual
  actual=$(shasum -a 256 "$1")
  print -r -- "${actual%% *}"
}

verify_sha256() {
  emulate -L zsh
  local actual
  actual=$(sha256 "$1")
  [[ $actual == $2 ]] || fail "digest verification failed for ${1:t}"
}

write_manifest() {
  emulate -L zsh
  setopt err_return no_unset pipe_fail
  local root=$1 output=$2 file relative
  local -a files
  files=("$root"/**/*(N.))
  : >"$output"
  for file in ${(o)files}; do
    [[ $file == "$output" ]] && continue
    relative=${file#$root/}
    print -r -- "$(sha256 "$file")  $relative" >>"$output"
  done
}

download_wheelhouse() {
  emulate -L zsh
  setopt err_return no_unset pipe_fail
  local python=$1 requirements=$2 destination=$3
  mkdir -p -- "$destination"
  "$python" -m pip download \
    --disable-pip-version-check \
    --quiet \
    --dest "$destination" \
    --require-hashes \
    --only-binary=:all: \
    --platform macosx_15_0_arm64 \
    --python-version 311 \
    --implementation cp \
    --abi cp311 \
    --requirement "$requirements"
}

main() {
  emulate -L zsh
  setopt err_return no_unset pipe_fail extended_glob
  (( $# == 2 || $# == 3 )) || {
    print -ru2 -- 'usage: build-bootstrap-closure.zsh WHEEL OUTPUT [RELEASE_TAG]'
    return 2
  }
  command -v curl >/dev/null || fail 'curl is required'
  command -v shasum >/dev/null || fail 'shasum is required'
  command -v tar >/dev/null || fail 'tar is required'
  local wheel=${1:A} output=${2:A} release_tag=${3:-}
  [[ -f $wheel && ! -L $wheel ]] || fail "wheel must be a regular file: $wheel"
  local wheel_name=${wheel:t}
  [[ $wheel_name =~ '^mastic-([0-9]+\.[0-9]+\.[0-9]+)-py3-none-any\.whl$' ]] || fail "unexpected wheel filename: $wheel_name"
  local version=$match[1]
  [[ ${output:t} == "mastic-bootstrap-closure-${version}-macos-arm64.tar.gz" ]] || fail 'output filename must match the MASTIC wheel version'
  if [[ -n $release_tag && $release_tag != "v${version}" ]]; then
    fail "release tag ${release_tag} does not match wheel version ${version}"
  fi

  local work
  work=$(mktemp -d "${TMPDIR:-/tmp}/mastic-closure.XXXXXXXX")
  local cleanup_command="rm -rf -- ${(q)work}"
  trap "$cleanup_command" EXIT
  trap "$cleanup_command; trap - EXIT; exit 130" INT
  trap "$cleanup_command; trap - EXIT; exit 143" TERM
  local stage="$work/stage"
  mkdir -p -- "$stage/uv" "$stage/wheels" "$stage/application-targets-v1/artifacts"

  local uv_archive="$work/uv.tar.gz"
  curl --fail --silent --show-error --location \
    --connect-timeout 30 --max-time 1800 --retry 3 --retry-delay 2 \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-aarch64-apple-darwin.tar.gz" \
    --output "$uv_archive"
  verify_sha256 "$uv_archive" $UV_SHA256
  tar -xzf "$uv_archive" -C "$work"
  local uv="$work/uv-aarch64-apple-darwin/uv"
  [[ -x $uv && ! -L $uv ]] || fail 'verified uv archive did not contain the expected executable'
  cp "$uv" "$stage/uv/uv"
  chmod 0755 "$stage/uv/uv"

  local python_installs="$work/python-installs"
  "$uv" python install \
    --quiet \
    --install-dir "$python_installs" \
    --no-bin \
    --no-cache \
    --managed-python \
    "$PYTHON_VERSION"
  local -a python_roots=("$python_installs"/*(N/))
  (( ${#python_roots} == 1 )) || fail 'exact Python install did not produce one runtime'
  cp -RL "$python_roots[1]" "$stage/python"
  local python="$stage/python/bin/python3.11"
  [[ -x $python && ! -L $python ]] || fail 'exact Python 3.11 runtime is incomplete'
  "$python" -m pip --version >/dev/null || fail 'exact Python runtime does not provide pip for wheelhouse construction'

  local mastic_lock="$stage/mastic-requirements.lock"
  "$uv" export \
    --quiet \
    --project "$REPOSITORY_ROOT" \
    --frozen \
    --no-dev \
    --no-emit-project \
    --format requirements.txt \
    --output-file "$mastic_lock"
  download_wheelhouse "$python" "$mastic_lock" "$stage/wheels"
  cp "$wheel" "$stage/wheels/$wheel_name"

  local hindsight_root="$work/hindsight-api"
  mkdir -p -- "$hindsight_root/wheels"
  cp "$REPOSITORY_ROOT/packaging/hindsight-api-0.8.4-macos-arm64.lock" "$hindsight_root/requirements.lock"
  download_wheelhouse "$python" "$hindsight_root/requirements.lock" "$hindsight_root/wheels"
  write_manifest "$hindsight_root" "$hindsight_root/SHA256SUMS"
  local hindsight_api_bundle="$stage/application-targets-v1/artifacts/hindsight-api-0.8.4-macos-arm64.tar.gz"
  COPYFILE_DISABLE=1 tar -czf "$hindsight_api_bundle" -C "$hindsight_root" .
  local hindsight_api_sha256
  hindsight_api_sha256=$(sha256 "$hindsight_api_bundle")

  local codex="$stage/application-targets-v1/artifacts/codex-aarch64-apple-darwin.tar.gz"
  curl --fail --silent --show-error --location \
    --connect-timeout 30 --max-time 1800 --retry 3 --retry-delay 2 \
    "$CODEX_URL" --output "$codex"
  verify_sha256 "$codex" $CODEX_SHA256
  local hindsight="$stage/application-targets-v1/artifacts/hindsight-darwin-arm64"
  curl --fail --silent --show-error --location \
    --connect-timeout 30 --max-time 1800 --retry 3 --retry-delay 2 \
    "$HINDSIGHT_URL" --output "$hindsight"
  verify_sha256 "$hindsight" $HINDSIGHT_SHA256
  chmod 0755 "$hindsight"

  local api_url="https://github.com/nisavid/mastic/releases/download/v${version}/hindsight-api-0.8.4-macos-arm64.tar.gz"
  local app_manifest="$stage/application-targets-v1/manifest.json"
  print -r -- "{\"schema_version\":1,\"platform\":\"macos-arm64\",\"artifacts\":[{\"id\":\"codex-cli\",\"version\":\"${CODEX_VERSION}\",\"filename\":\"codex-aarch64-apple-darwin.tar.gz\",\"sha256\":\"${CODEX_SHA256}\",\"source_url\":\"${CODEX_URL}\",\"install_kind\":\"standalone-tar\",\"probe_argv\":[\"--version\"],\"probe_output\":\"codex-cli ${CODEX_VERSION}\"},{\"id\":\"hindsight-cli\",\"version\":\"${HINDSIGHT_VERSION}\",\"filename\":\"hindsight-darwin-arm64\",\"sha256\":\"${HINDSIGHT_SHA256}\",\"source_url\":\"${HINDSIGHT_URL}\",\"install_kind\":\"standalone\",\"probe_argv\":[\"--version\"],\"probe_output\":\"hindsight ${HINDSIGHT_VERSION}\"},{\"id\":\"hindsight-api\",\"version\":\"${HINDSIGHT_VERSION}\",\"filename\":\"hindsight-api-0.8.4-macos-arm64.tar.gz\",\"sha256\":\"${hindsight_api_sha256}\",\"source_url\":\"${api_url}\",\"install_kind\":\"uv-tool-offline\",\"probe_argv\":[\"python-metadata\",\"hindsight-api\"],\"probe_output\":\"${HINDSIGHT_VERSION}\"}]}" >"$app_manifest"

  write_manifest "$stage" "$stage/SHA256SUMS"
  mkdir -p -- "${output:h}"
  COPYFILE_DISABLE=1 tar -czf "$output" -C "$stage" .
  print -r -- "$output"
}

main "$@"
