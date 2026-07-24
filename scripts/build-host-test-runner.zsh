#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR=${0:A:h}

main() {
  emulate -L zsh
  setopt err_return no_unset pipe_fail
  (( $# == 3 )) || {
    print -ru2 -- 'usage: build-host-test-runner.zsh WHEEL FIXTURE OUTPUT'
    return 2
  }
  local wheel=${1:A}
  local fixture=${2:A}
  local output=${3:A}
  local template="$SCRIPT_DIR/bootstrap-mastic-host-test.zsh.in"
  [[ -f $wheel && ! -L $wheel ]] || {
    print -ru2 -- "wheel must be a regular file: $wheel"
    return 2
  }
  [[ -r $template ]] || {
    print -ru2 -- "bootstrap template is unreadable: $template"
    return 2
  }
  local filename=${wheel:t}
  [[ $filename =~ '^mastic-([0-9]+\.[0-9]+\.[0-9]+)-py3-none-any\.whl$' ]] || {
    print -ru2 -- "unexpected wheel filename: $filename"
    return 2
  }
  local version=$match[1]
  local fixture_filename=${fixture:t}
  [[ -f $fixture && ! -L $fixture ]] || {
    print -ru2 -- "host-test fixture must be a regular file: $fixture"
    return 2
  }
  [[ $fixture_filename == "mastic-host-test-fixture-${version}-macos-arm64.tar.gz" ]] || {
    print -ru2 -- "host-test fixture filename does not match wheel version ${version}: $fixture_filename"
    return 2
  }
  local wheel_digest fixture_digest content
  wheel_digest=$(shasum -a 256 "$wheel")
  wheel_digest=${wheel_digest%% *}
  fixture_digest=$(shasum -a 256 "$fixture")
  fixture_digest=${fixture_digest%% *}
  content=$(<"$template")
  content=${content//@MASTIC_VERSION@/$version}
  content=${content//@MASTIC_WHEEL_SHA256@/$wheel_digest}
  content=${content//@MASTIC_HOST_FIXTURE_SHA256@/$fixture_digest}
  [[ $content != *'@MASTIC_'* ]] || {
    print -ru2 -- 'host-test runner template contains unresolved tokens'
    return 2
  }
  mkdir -p -- "${output:h}"
  print -r -- "$content" >"$output"
  chmod 0755 "$output"
  zsh -n "$output"
  print -r -- "$output"
}

main "$@"
