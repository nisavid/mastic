#!/usr/bin/env zsh

set -euo pipefail

fail() {
  emulate -L zsh
  print -ru2 -- "mastic release: $1"
  return 2
}

sha256() {
  emulate -L zsh
  local digest
  digest=$(shasum -a 256 "$1")
  print -r -- "${digest%% *}"
}

main() {
  emulate -L zsh
  setopt err_return no_unset pipe_fail
  (( $# == 3 )) || fail \
    'usage: stage-release-artifacts.zsh OUTPUT_DIRECTORY WHEEL SOURCE_DISTRIBUTION'

  [[ ! -L $1 ]] || fail "output path already exists: $1"
  [[ -f $2 && ! -L $2 ]] || fail \
    "expected a regular MASTIC wheel: $2"
  [[ -f $3 && ! -L $3 ]] || fail \
    "expected a regular MASTIC source distribution: $3"
  local output=${1:A} wheel=${2:A} source=${3:A}
  local wheel_name=${wheel:t} source_name=${source:t}
  [[ $wheel_name =~ '^mastic-([0-9]+\.[0-9]+\.[0-9]+)-py3-none-any\.whl$' ]] || fail \
    "expected a MASTIC wheel: $wheel_name"
  local version=$match[1]
  [[ $source_name == "mastic-${version}.tar.gz" ]] || fail \
    "expected a MASTIC source distribution for ${version}: $source_name"
  [[ ! -e $output && ! -L $output ]] || fail \
    "output path already exists: $output"

  mkdir -p -- "${output:h}"
  local stage
  stage=$(mktemp -d "${output:h}/.mastic-release.XXXXXXXX")
  local cleanup_command="rm -rf -- ${(q)stage}"
  trap "$cleanup_command" EXIT
  trap "$cleanup_command; trap - EXIT; exit 130" INT
  trap "$cleanup_command; trap - EXIT; exit 143" TERM

  local artifact digest
  for artifact in "$wheel" "$source"; do
    cp "$artifact" "$stage/${artifact:t}"
    digest=$(sha256 "$stage/${artifact:t}")
    print -r -- "$digest  ${artifact:t}" >"$stage/${artifact:t}.sha256"
  done
  chmod 0644 "$stage"/*
  mv "$stage" "$output"
  trap - EXIT INT TERM
}

main "$@"
