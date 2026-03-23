#!/usr/bin/env bash

common_log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
}

common_warn() {
  echo "WARNING: $*" >&2
}

common_error() {
  echo "ERROR: $*" >&2
}

common_section() {
  echo
  echo "=========================================="
  echo "$*"
  echo "=========================================="
  echo
}

common_require_tools() {
  local missing=()
  local tool

  for tool in "$@"; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      missing+=("$tool")
    fi
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    common_error "Missing required tool(s): ${missing[*]}"
    return 1
  fi
}
