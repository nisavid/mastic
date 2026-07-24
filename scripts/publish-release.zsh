#!/usr/bin/env zsh

set -euo pipefail

fail() {
  emulate -L zsh
  print -ru2 -- "mastic release: $1"
  return 2
}

remote_tag_target() {
  emulate -L zsh
  setopt err_return no_unset pipe_fail
  local repository=$1 tag=$2 output
  local remote="https://github.com/${repository}.git"
  output=$(git ls-remote "$remote" "refs/tags/${tag}^{}")
  if [[ -z $output ]]; then
    output=$(git ls-remote "$remote" "refs/tags/${tag}")
  fi
  [[ -n $output ]] || fail "remote tag does not exist: $tag"
  print -r -- "${output%%[[:space:]]*}"
}

verify_checksum() {
  emulate -L zsh
  setopt err_return no_unset pipe_fail
  local directory=$1 artifact_name=$2
  local checksum_name="${artifact_name}.sha256"
  local checksum_path="$directory/$checksum_name"
  local -a lines=("${(@f)$(<$checksum_path)}")
  (( ${#lines} == 1 )) || fail \
    "checksum must contain exactly one entry: $checksum_name"
  [[ $lines[1] =~ '^[0-9a-f]{64}  ([^/]+)$' ]] || fail \
    "checksum has an invalid entry: $checksum_name"
  [[ $match[1] == $artifact_name ]] || fail \
    "checksum names an unexpected artifact: $checksum_name"
  (cd "$directory" && shasum -c "$checksum_name")
}

main() {
  emulate -L zsh
  setopt err_return no_unset pipe_fail
  (( $# == 4 )) || fail \
    'usage: publish-release.zsh TAG EXPECTED_COMMIT REPOSITORY RELEASE_DIRECTORY'

  [[ -d $4 && ! -L $4 ]] || fail \
    "release directory does not exist: $4"
  local tag=$1 expected_commit=$2 repository=$3 directory=${4:A}
  [[ $tag =~ '^v([0-9]+\.[0-9]+\.[0-9]+)$' ]] || fail \
    "expected a semantic version tag: $tag"
  local version=$match[1]
  [[ $expected_commit =~ '^[0-9a-f]{40}$' ]] || fail \
    'expected a full lowercase commit SHA'
  [[ $repository =~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' ]] || fail \
    "expected an owner/repository name: $repository"
  local wheel_name="mastic-${version}-py3-none-any.whl"
  local source_name="mastic-${version}.tar.gz"
  local -a expected=(
    "$wheel_name"
    "${wheel_name}.sha256"
    "$source_name"
    "${source_name}.sha256"
  )
  local -a paths=("$directory"/*(DN))
  (( ${#paths} == ${#expected} )) || fail \
    'release directory must contain exactly four MASTIC files'

  local -A allowed
  local name input_path
  for name in $expected; do
    allowed[$name]=1
  done
  for input_path in $paths; do
    [[ -f $input_path && ! -L $input_path ]] || fail \
      "release input is not a regular file: ${input_path:t}"
    (( ${+allowed[${input_path:t}]} )) || fail \
      "release directory contains an unexpected file: ${input_path:t}"
  done
  for name in $expected; do
    [[ -f "$directory/$name" && ! -L "$directory/$name" ]] || fail \
      "release directory is missing: $name"
  done

  verify_checksum "$directory" "$wheel_name"
  verify_checksum "$directory" "$source_name"

  local target
  target=$(remote_tag_target "$repository" "$tag")
  [[ $target == $expected_commit ]] || fail \
    "remote tag target changed before draft preparation: $tag"

  local -a release_assets=(
    "$directory/$wheel_name" \
    "$directory/${wheel_name}.sha256" \
    "$directory/$source_name" \
    "$directory/${source_name}.sha256"
  )
  local draft_state
  if draft_state=$(gh release view "$tag" --repo "$repository" --json isDraft \
    --jq '.isDraft' 2>/dev/null); then
    [[ $draft_state == true ]] || fail \
      "release already exists and is not a draft: $tag"
    gh release upload "$tag" $release_assets \
      --repo "$repository" \
      --clobber
  else
    gh release create "$tag" $release_assets \
      --repo "$repository" \
      --draft \
      --verify-tag \
      --generate-notes
  fi

  local -a actual=(
    ${${(f)"$(gh release view "$tag" --repo "$repository" --json assets \
      --jq '.assets[].name')"}:#}
  )
  [[ ${(j:\n:)${(o)expected}} == ${(j:\n:)${(o)actual}} ]] || fail \
    'draft release asset set does not match the MASTIC allowlist'

  target=$(remote_tag_target "$repository" "$tag")
  [[ $target == $expected_commit ]] || fail \
    "remote tag target changed before publication: $tag"
  gh release edit "$tag" --repo "$repository" --draft=false
}

main "$@"
