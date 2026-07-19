#!/usr/bin/env zsh

set -euo pipefail

readonly SCRIPT_DIR=${0:A:h}

main() {
  emulate -L zsh
  setopt err_return no_unset pipe_fail
  (( $# == 3 || $# == 4 )) || {
    print -ru2 -- 'usage: build-bootstrap.zsh WHEEL CLOSURE OUTPUT [RELEASE_TAG]'
    return 2
  }
  local wheel=${1:A}
  local closure=${2:A}
  local output=${3:A}
  local template="$SCRIPT_DIR/bootstrap-mastic.zsh.in"
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
  local closure_filename=${closure:t}
  [[ -f $closure && ! -L $closure ]] || {
    print -ru2 -- "closure must be a regular file: $closure"
    return 2
  }
  [[ $closure_filename == "mastic-bootstrap-closure-${version}-macos-arm64.tar.gz" ]] || {
    print -ru2 -- "closure filename does not match wheel version ${version}: $closure_filename"
    return 2
  }
  local release_tag=${4:-}
  if [[ -n $release_tag && $release_tag != "v${version}" ]]; then
    print -ru2 -- "release tag ${release_tag} does not match wheel version ${version}"
    return 2
  fi
  local wheel_digest closure_digest content
  wheel_digest=$(shasum -a 256 "$wheel")
  wheel_digest=${wheel_digest%% *}
  closure_digest=$(shasum -a 256 "$closure")
  closure_digest=${closure_digest%% *}
  content=$(<"$template")
  content=${content//@MASTIC_VERSION@/$version}
  content=${content//@MASTIC_WHEEL_SHA256@/$wheel_digest}
  content=${content//@MASTIC_CLOSURE_SHA256@/$closure_digest}
  [[ $content != *'@MASTIC_'* ]] || {
    print -ru2 -- 'bootstrap template contains unresolved release tokens'
    return 2
  }
  mkdir -p -- "${output:h}"
  print -r -- "$content" >"$output"
  chmod 0755 "$output"
  zsh -n "$output"
  print -r -- "$output"
}

main "$@"
