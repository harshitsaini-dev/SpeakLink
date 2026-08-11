#!/usr/bin/env bash
# Project-local GitHub CLI, for Git Bash / WSL / macOS / Linux.
#
# Same idea as gh.cmd: point GH_CONFIG_DIR at this repository so the token
# lives beside the project instead of in the account-wide login. Use it as
# `./gh.sh <command>`, or source it to set the variable for a whole shell:
#
#     source ./gh.sh          # sets GH_CONFIG_DIR for this shell only
#     ./gh.sh auth status     # runs one command with it set
#
# Log in with `./gh.sh auth login --insecure-storage`, otherwise gh stores the
# token in the OS keyring and this wrapper changes nothing while appearing to
# work.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export GH_CONFIG_DIR="$here/.gh-config"

# Sourced with no arguments: just export and return.
if [ "${BASH_SOURCE[0]}" != "${0}" ] && [ "$#" -eq 0 ]; then
  echo "GH_CONFIG_DIR=$GH_CONFIG_DIR"
  return 0 2>/dev/null || true
fi

exec "/c/Program Files/GitHub CLI/gh.exe" "$@"
