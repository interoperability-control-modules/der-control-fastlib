#!/usr/bin/env bash
# Refuse to build an agent image from a dirty checkout, unless ALLOW_DIRTY=1
# (#68). Shared by every docker/<agent>/check-clean.sh wrapper, so a pytest
# test can drive it directly against a scratch git checkout.
#
# Usage: check-clean.sh <path-to-checkout>
set -euo pipefail

main() {
  if [ "$#" -ne 1 ]; then
    echo "usage: check-clean.sh <path-to-checkout>" >&2
    exit 2
  fi
  local checkout="$1"

  if [ ! -e "${checkout}" ]; then
    echo "check-clean: ${checkout} does not exist" >&2
    exit 1
  fi

  if ! git -C "${checkout}" rev-parse --git-dir >/dev/null 2>&1; then
    echo "check-clean: ${checkout} is not a git checkout" >&2
    exit 1
  fi

  # build-agent.sh archives HEAD of this path, and from a subdirectory that
  # would silently archive only the subtree (#148).
  local top abs
  if ! top="$(git -C "${checkout}" rev-parse --show-toplevel 2>/dev/null)"; then
    echo "check-clean: ${checkout} is not the top of a git work tree" >&2
    exit 1
  fi
  abs="$(cd "${checkout}" && pwd -P)"
  top="$(cd "${top}" && pwd -P)"
  if [ "${abs}" != "${top}" ]; then
    echo "check-clean: ${checkout} is a subdirectory of the work tree at ${top}; pass the top of the checkout" >&2
    exit 1
  fi

  # ALLOW_DIRTY bypasses only the dirtiness check, never the path checks above.
  if [ "${ALLOW_DIRTY:-0}" = "1" ]; then
    echo "check-clean: ALLOW_DIRTY=1, skipping the dirty-checkout check for ${checkout}" >&2
    exit 0
  fi

  local status
  status="$(git -C "${checkout}" status --porcelain)"
  if [ -n "${status}" ]; then
    echo "check-clean: ${checkout} has uncommitted changes; refusing to build from it:" >&2
    echo "${status}" >&2
    echo "check-clean: set ALLOW_DIRTY=1 to build anyway." >&2
    exit 1
  fi

  echo "check-clean: ${checkout} is clean." >&2
}

main "$@"
