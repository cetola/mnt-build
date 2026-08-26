#!/usr/bin/env bash
set -euo pipefail

# Resolves a bootloader artifact for MNT sysimages.
# - Sourceable API: get_fsbl_artifact
# - Standalone CLI: build-fsbl.sh --sysimage ...

: "${SCRIPT_DIR:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
readonly FSBL_SCRIPT_PATH="${BASH_SOURCE[0]}"
readonly FSBL_SCRIPT_DIR="$(cd "$(dirname "$FSBL_SCRIPT_PATH")" && pwd)"
readonly FSBL_TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

# shellcheck source=/dev/null
source "$FSBL_SCRIPT_DIR/common.sh"

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  FSBL_STANDALONE=1
else
  FSBL_STANDALONE=0
fi

# Public outputs
FSBL_LOGFILE=""
BOOTLOADER_ARTIFACT_PATH=""
FSBL_SHOW_HELP=0
FSBL_DIFF=0
FSBL_TRACE="${FSBL_TRACE:-0}"
FSBL_TRACE_ENABLED=0

# Internal state
FSBL_LOG_REDIRECTED=0
FSBL_RESOLVED_REF=""
FSBL_REF_KIND=""
FSBL_SOURCE_REPO_DIR=""
FSBL_REPO_URL=""

fsbl_log() {
  common_log "$@"
}

fsbl_log_warn() {
  common_warn "$@"
}

fsbl_log_error() {
  common_error "$@"
}

fsbl_die() {
  fsbl_log_error "$*"
  return 1
}

fsbl_usage() {
  cat <<'USAGE'
Usage:
  build-fsbl.sh --sysimage <name> [--mode prebuilt|source] [--ref <ref>] [--sha <sha>] [--repo <url>] [--downloads-dir <path>] [--work-dir <path>] [--build-cmd <cmd>] [--artifact-path <path>] [--artifact-glob <glob>] [--cross-compile <prefix>] [--defconfig <name>] [--diff] [--trace]

Options:
  --sysimage <name>        Target sysimage (required for standalone mode)
  --mode <mode>            Bootloader source mode: prebuilt (default) or source
  --ref <ref>              Git ref (tag/branch/SHA) override
  --sha <sha>              Exact git commit SHA override (takes priority over --ref)
  --repo <url>             U-Boot repository URL override
  --downloads-dir <path>   Override downloads directory (default: <work-dir>/downloads)
  --work-dir <path>        Override work directory (default: $(pwd)/image-gen)
  --build-cmd <cmd>        Build command to run inside source checkout (source mode)
  --artifact-path <path>   Exact artifact path inside source checkout (source mode)
  --artifact-glob <glob>   Artifact glob inside source checkout (source mode)
  --cross-compile <prefix> CROSS_COMPILE prefix for default make flow (default: aarch64-linux-gnu-)
  --defconfig <name>       Defconfig for default make flow (source mode)
  --diff                   In source mode (standalone), compare built artifact with matching prebuilt artifact
  --trace                  Enable shell command tracing (xtrace)
  --help                   Show this help text
USAGE
}

fsbl_init_defaults() {
  : "${WORK_DIR:=$(pwd)/image-gen}"
  : "${DOWNLOADS_DIR:=${WORK_DIR}/downloads}"
  : "${BOOTLOADER_SOURCE_MODE:=prebuilt}"

  # Optional caller-provided overrides.
  : "${UBOOT_REF:=}"
  : "${UBOOT_SHA:=}"
  : "${UBOOT_REPO_URL:=}"
  : "${UBOOT_BUILD_CMD:=}"
  : "${UBOOT_ARTIFACT_PATH:=}"
  : "${UBOOT_ARTIFACT_GLOB:=}"
  : "${UBOOT_CROSS_COMPILE:=aarch64-linux-gnu-}"
  : "${UBOOT_DEFCONFIG:=}"

  # Keep deterministic fallback name if caller didn't set BOOTLOADER_FILENAME.
  if [[ -z "${BOOTLOADER_FILENAME:-}" && -n "${DTBPATH:-}" ]]; then
    BOOTLOADER_FILENAME="$(basename "${DTBPATH%.dtb}")-flash.bin"
  fi

  # Patches subdir under xtra-patches/ (default u-boot for backward compat).
  : "${FSBL_PATCHES_SUBDIR:=u-boot}"

  mkdir -p "$WORK_DIR" "$DOWNLOADS_DIR"

  if [[ -z "${FSBL_LOGFILE:-}" ]]; then
    FSBL_LOGFILE="${WORK_DIR}/fsbl-build-${SYSIMAGE:-unknown}-${FSBL_TIMESTAMP}.log"
  fi
}

fsbl_init_logging() {
  if [[ "$FSBL_LOG_REDIRECTED" -eq 1 ]]; then
    return 0
  fi

  # When sourced, preserve caller streams and restore on function exit.
  if [[ "$FSBL_STANDALONE" -eq 0 ]]; then
    exec 9>&1 10>&2
  fi

  exec > >(tee -a "$FSBL_LOGFILE") 2>&1

  # Prefix xtrace with file:line for reproducibility.
  export PS4='+ [${BASH_SOURCE##*/}:${LINENO}] '

  FSBL_LOG_REDIRECTED=1
  fsbl_log "Logging to: $FSBL_LOGFILE"
  fsbl_log "Started at: $(date)"
  fsbl_log "Mode: ${BOOTLOADER_SOURCE_MODE}"
  fsbl_log "Sysimage: ${SYSIMAGE:-unset}"
}

fsbl_restore_logging() {
  if [[ "$FSBL_LOG_REDIRECTED" -eq 0 ]]; then
    return 0
  fi

  if [[ "$FSBL_STANDALONE" -eq 0 ]]; then
    exec 1>&9 2>&10
    exec 9>&- 10>&-
  fi

  FSBL_LOG_REDIRECTED=0
}

fsbl_enable_command_trace() {
  case "${FSBL_TRACE}" in
    1|true|TRUE|yes|YES|on|ON)
      FSBL_TRACE_ENABLED=1
      set -x
      ;;
    *)
      FSBL_TRACE_ENABLED=0
      ;;
  esac
}

fsbl_disable_command_trace() {
  if [[ "$FSBL_TRACE_ENABLED" -eq 1 ]]; then
    set +x
  fi
}

fsbl_require_tools() {
  common_require_tools "$@" || return 1
}

fsbl_validate_required_context() {
  local missing=()

  [[ -n "${SYSIMAGE:-}" ]] || missing+=("SYSIMAGE")
  [[ -n "${BOOTLOADER_PROJECT:-}" ]] || missing+=("BOOTLOADER_PROJECT")
  [[ -n "${BOOTLOADER_TAG:-}" ]] || missing+=("BOOTLOADER_TAG")
  [[ -n "${BOOTLOADER_FILENAME:-}" ]] || missing+=("BOOTLOADER_FILENAME")
  [[ -n "${DOWNLOADS_DIR:-}" ]] || missing+=("DOWNLOADS_DIR")

  if [[ "${SD_BOOT:-false}" == "true" ]]; then
    [[ -n "${BOOTLOADER_OFFSET:-}" ]] || missing+=("BOOTLOADER_OFFSET")
    [[ -n "${FLASHBIN_OFFSET:-}" ]] || missing+=("FLASHBIN_OFFSET")
  fi

  if [[ ${#missing[@]} -gt 0 ]]; then
    fsbl_die "Missing required variables: ${missing[*]}"
  fi
}

fsbl_validate_mode() {
  case "${BOOTLOADER_SOURCE_MODE}" in
    prebuilt | source)
      ;;
    *)
      fsbl_die "Invalid BOOTLOADER_SOURCE_MODE='${BOOTLOADER_SOURCE_MODE}'. Expected prebuilt or source."
      ;;
  esac
}

fsbl_resolve_ref() {
  if [[ -n "${UBOOT_SHA}" ]]; then
    FSBL_RESOLVED_REF="${UBOOT_SHA}"
    FSBL_REF_KIND="sha"
  elif [[ -n "${UBOOT_REF}" ]]; then
    FSBL_RESOLVED_REF="${UBOOT_REF}"
    FSBL_REF_KIND="ref"
  else
    FSBL_RESOLVED_REF="${BOOTLOADER_TAG}"
    FSBL_REF_KIND="tag"
  fi
}

fsbl_emit_artifact_metadata() {
  local artifact="$1"
  local size sha1 sha256

  size="$(stat -c%s "$artifact")"
  sha1="$(sha1sum "$artifact" | awk '{print $1}')"
  sha256="$(sha256sum "$artifact" | awk '{print $1}')"

  fsbl_log "Artifact metadata:"
  fsbl_log "  path: $artifact"
  fsbl_log "  size: $size"
  fsbl_log "  sha1: $sha1"
  fsbl_log "  sha256: $sha256"

  if [[ -n "${BOOTLOADER_SHA1:-}" ]]; then
    if [[ "$sha1" == "$BOOTLOADER_SHA1" ]]; then
      fsbl_log "  expected sha1: $BOOTLOADER_SHA1 (match)"
    else
      if [[ "$BOOTLOADER_SOURCE_MODE" == "prebuilt" ]]; then
        fsbl_die "Prebuilt bootloader checksum mismatch (expected $BOOTLOADER_SHA1, got $sha1)."
      else
        fsbl_log_warn "Source-built artifact checksum differs from machine config sha1 (expected $BOOTLOADER_SHA1, got $sha1)."
      fi
    fi
  fi
}

fsbl_emit_next_step_commands() {
  local artifact="$1"
  local seek_blocks=0
  local skip_blocks=0

  if [[ "${SD_BOOT:-false}" != "true" ]]; then
    fsbl_log "Platform does not require SD bootloader raw write (SD_BOOT=false)."
    return 0
  fi

  seek_blocks=$((BOOTLOADER_OFFSET / 512))
  skip_blocks=$((FLASHBIN_OFFSET / 512))

  echo
  echo "=========================================="
  echo "FSBL Flash Commands"
  echo "=========================================="
  echo
  echo "Artifact:           ${artifact}"
  echo "BOOTLOADER_OFFSET:  ${BOOTLOADER_OFFSET}"
  echo "FLASHBIN_OFFSET:    ${FLASHBIN_OFFSET}"
  echo "seek blocks:        ${seek_blocks}"
  echo "skip blocks:        ${skip_blocks}"
  echo
  echo "SD card:"
  echo "  sudo dd if='${artifact}' of=/dev/<sd-device> conv=notrunc bs=512 seek=${seek_blocks} skip=${skip_blocks}"
  echo
  echo "eMMC:"
  echo "  sudo dd if='${artifact}' of=/dev/<emmc-device> conv=notrunc bs=512 seek=${seek_blocks} skip=${skip_blocks}"
  echo
}

fsbl_set_default_repo_url() {
  if [[ -n "$UBOOT_REPO_URL" ]]; then
    FSBL_REPO_URL="$UBOOT_REPO_URL"
  else
    FSBL_REPO_URL="https://source.mnt.re/reform/${BOOTLOADER_PROJECT}.git"
  fi
}

fsbl_download_prebuilt() {
  local bootloader_url="$1"
  local output_path="$2"
  local tmp_path="${output_path}.part"

  fsbl_require_tools curl sha1sum sha256sum stat awk

  if [[ -f "$output_path" && -n "${BOOTLOADER_SHA1:-}" ]]; then
    local existing_sha1
    existing_sha1="$(sha1sum "$output_path" | awk '{print $1}')"
    if [[ "$existing_sha1" == "$BOOTLOADER_SHA1" ]]; then
      fsbl_log "Using cached bootloader artifact: $output_path"
      BOOTLOADER_ARTIFACT_PATH="$output_path"
      return 0
    fi
    fsbl_log_warn "Cached artifact checksum mismatch, re-downloading: $output_path"
    rm -f "$output_path"
  fi

  fsbl_log "Downloading prebuilt bootloader artifact..."
  curl -fL --retry 3 --retry-delay 2 -o "$tmp_path" "$bootloader_url" || {
    fsbl_die "Failed to download bootloader artifact from $bootloader_url"
    return 1
  }
  mv "$tmp_path" "$output_path" || {
    fsbl_die "Failed to move downloaded artifact into place: $output_path"
    return 1
  }
  BOOTLOADER_ARTIFACT_PATH="$output_path"
}

fsbl_checkout_source_repo() {
  fsbl_require_tools git

  FSBL_SOURCE_REPO_DIR="${WORK_DIR}/bootloader-src/${BOOTLOADER_PROJECT}"
  mkdir -p "$(dirname "$FSBL_SOURCE_REPO_DIR")"

  if [[ -d "$FSBL_SOURCE_REPO_DIR/.git" ]]; then
    fsbl_log "Reusing source checkout: $FSBL_SOURCE_REPO_DIR"
    git -C "$FSBL_SOURCE_REPO_DIR" remote set-url origin "$FSBL_REPO_URL" || {
      fsbl_die "Failed to set source repo origin URL"
      return 1
    }
    git -C "$FSBL_SOURCE_REPO_DIR" fetch --tags origin || {
      fsbl_die "Failed to fetch source repo tags from $FSBL_REPO_URL"
      return 1
    }
  else
    fsbl_log "Cloning source repo: $FSBL_REPO_URL"
    git clone "$FSBL_REPO_URL" "$FSBL_SOURCE_REPO_DIR" || {
      fsbl_die "Failed to clone source repo: $FSBL_REPO_URL"
      return 1
    }
    git -C "$FSBL_SOURCE_REPO_DIR" fetch --tags origin || {
      fsbl_die "Failed to fetch source repo tags from $FSBL_REPO_URL"
      return 1
    }
  fi

  fsbl_log "Checking out ref: $FSBL_RESOLVED_REF"
  git -C "$FSBL_SOURCE_REPO_DIR" checkout --detach "$FSBL_RESOLVED_REF" || {
    fsbl_die "Failed to checkout source ref: $FSBL_RESOLVED_REF"
    return 1
  }
}

fsbl_apply_xtra_patches() {
  local subdir="${FSBL_PATCHES_SUBDIR:-u-boot}"
  local patches_dir="${FSBL_SCRIPT_DIR}/../xtra-patches/${subdir}/${BOOTLOADER_PROJECT}"
  # Fall back to flat layout (no per-project subdir) if the project subdir doesn't exist.
  if [[ ! -d "$patches_dir" ]]; then
    patches_dir="${FSBL_SCRIPT_DIR}/../xtra-patches/${subdir}"
  fi
  [[ -d "$patches_dir" ]] || return 0
  local patches=("$patches_dir"/*.patch)
  [[ -e "${patches[0]}" ]] || return 0
  fsbl_log "Applying xtra patches from: $patches_dir"
  git -C "$FSBL_SOURCE_REPO_DIR" am "${patches[@]}"
}

fsbl_run_default_build() {
  fsbl_require_tools make nproc

  if [[ -n "$UBOOT_DEFCONFIG" ]]; then
    fsbl_log "Running defconfig: $UBOOT_DEFCONFIG"
    (cd "$FSBL_SOURCE_REPO_DIR" && make "CROSS_COMPILE=${UBOOT_CROSS_COMPILE}" "$UBOOT_DEFCONFIG")
  fi

  fsbl_log "Running default make build"
  (cd "$FSBL_SOURCE_REPO_DIR" && make -j"$(nproc)" "CROSS_COMPILE=${UBOOT_CROSS_COMPILE}")
}

fsbl_run_source_build() {
  local makeflags_jobs=""

  # Force parallel make unless caller already supplied MAKEFLAGS.
  if [[ -n "${MAKEFLAGS:-}" ]]; then
    makeflags_jobs="$MAKEFLAGS"
  else
    makeflags_jobs="-j$(nproc)"
  fi
  fsbl_log "Using MAKEFLAGS=${makeflags_jobs}"

  if [[ -n "$UBOOT_BUILD_CMD" ]]; then
    fsbl_log "Running custom build command"
    (cd "$FSBL_SOURCE_REPO_DIR" && MAKEFLAGS="$makeflags_jobs" bash -lc "$UBOOT_BUILD_CMD")
    return 0
  fi

  if ! command -v "${UBOOT_CROSS_COMPILE}gcc" >/dev/null 2>&1; then
    fsbl_die "Missing required cross compiler: ${UBOOT_CROSS_COMPILE}gcc"
    return 1
  fi

  if [[ -x "$FSBL_SOURCE_REPO_DIR/build.sh" ]]; then
    fsbl_log "Running project build script with bash: build.sh"
    (cd "$FSBL_SOURCE_REPO_DIR" && MAKEFLAGS="$makeflags_jobs" bash ./build.sh)
    return 0
  fi

  if [[ -f "$FSBL_SOURCE_REPO_DIR/Makefile" ]]; then
    fsbl_run_default_build
    return 0
  fi

  fsbl_die "Unable to determine build method. Provide --build-cmd explicitly."
}

fsbl_find_candidate_artifact() {
  local repo_dir="$1"
  local pattern="$2"

  find "$repo_dir" -type f -name "$pattern" -printf '%T@ %p\n' 2>/dev/null \
    | sort -nr \
    | head -n 1 \
    | cut -d' ' -f2-
}

fsbl_resolve_source_artifact() {
  local candidate=""

  fsbl_require_tools find sort head cut

  if [[ -n "$UBOOT_ARTIFACT_PATH" ]]; then
    if [[ "$UBOOT_ARTIFACT_PATH" = /* ]]; then
      candidate="$UBOOT_ARTIFACT_PATH"
    else
      candidate="$FSBL_SOURCE_REPO_DIR/$UBOOT_ARTIFACT_PATH"
    fi
    [[ -f "$candidate" ]] || fsbl_die "Configured artifact path not found: $candidate"
    printf '%s\n' "$candidate"
    return 0
  fi

  if [[ -n "$UBOOT_ARTIFACT_GLOB" ]]; then
    candidate="$(fsbl_find_candidate_artifact "$FSBL_SOURCE_REPO_DIR" "$UBOOT_ARTIFACT_GLOB")"
    if [[ -n "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi

  candidate="$(fsbl_find_candidate_artifact "$FSBL_SOURCE_REPO_DIR" "$BOOTLOADER_FILENAME")"
  if [[ -n "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  candidate="$(fsbl_find_candidate_artifact "$FSBL_SOURCE_REPO_DIR" "flash.bin")"
  if [[ -n "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  candidate="$(fsbl_find_candidate_artifact "$FSBL_SOURCE_REPO_DIR" "*-flash.bin")"
  if [[ -n "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  fsbl_die "Failed to locate built artifact. Use --artifact-path or --artifact-glob to specify it."
}

fsbl_handle_prebuilt() {
  local bootloader_url="https://source.mnt.re/reform/${BOOTLOADER_PROJECT}/-/jobs/artifacts/${BOOTLOADER_TAG}/raw/${BOOTLOADER_FILENAME}?job=build"
  local bootloader_path="$DOWNLOADS_DIR/$BOOTLOADER_FILENAME"

  fsbl_download_prebuilt "$bootloader_url" "$bootloader_path"
}

fsbl_handle_source_build() {
  local source_artifact=""
  local output_path="$DOWNLOADS_DIR/$BOOTLOADER_FILENAME"

  fsbl_set_default_repo_url
  fsbl_checkout_source_repo || return 1
  fsbl_apply_xtra_patches || return 1
  fsbl_run_source_build || return 1

  source_artifact="$(fsbl_resolve_source_artifact)" || return 1
  [[ -n "$source_artifact" && -f "$source_artifact" ]] || {
    fsbl_die "Resolved source artifact is missing: $source_artifact"
    return 1
  }
  fsbl_log "Resolved built artifact: $source_artifact"

  install -D -m 0644 "$source_artifact" "$output_path" || {
    fsbl_die "Failed to install built artifact to $output_path"
    return 1
  }
  BOOTLOADER_ARTIFACT_PATH="$output_path"
}

fsbl_compare_source_vs_prebuilt() {
  local built_artifact="$1"
  local prebuilt_url=""
  local prebuilt_artifact=""
  local built_size built_sha1 built_sha256
  local prebuilt_size prebuilt_sha1 prebuilt_sha256
  local first_diff_line first_diff_byte first_diff_start

  fsbl_require_tools curl sha1sum sha256sum stat awk cmp od head

  prebuilt_url="https://source.mnt.re/reform/${BOOTLOADER_PROJECT}/-/jobs/artifacts/${BOOTLOADER_TAG}/raw/${BOOTLOADER_FILENAME}?job=build"
  prebuilt_artifact="${DOWNLOADS_DIR}/${BOOTLOADER_FILENAME}.prebuilt-${BOOTLOADER_TAG}"

  fsbl_log "Diff mode: downloading reference prebuilt artifact..."
  curl -fL --retry 3 --retry-delay 2 -o "$prebuilt_artifact" "$prebuilt_url" || {
    fsbl_log_warn "Unable to download prebuilt reference artifact for diff: $prebuilt_url"
    return 0
  }

  built_size="$(stat -c%s "$built_artifact")"
  built_sha1="$(sha1sum "$built_artifact" | awk '{print $1}')"
  built_sha256="$(sha256sum "$built_artifact" | awk '{print $1}')"

  prebuilt_size="$(stat -c%s "$prebuilt_artifact")"
  prebuilt_sha1="$(sha1sum "$prebuilt_artifact" | awk '{print $1}')"
  prebuilt_sha256="$(sha256sum "$prebuilt_artifact" | awk '{print $1}')"

  echo
  echo "=========================================="
  echo "FSBL Diff Report"
  echo "=========================================="
  echo
  echo "Built artifact:     $built_artifact"
  echo "Prebuilt artifact:  $prebuilt_artifact"
  echo
  echo "Built size:         $built_size"
  echo "Prebuilt size:      $prebuilt_size"
  echo "Built SHA1:         $built_sha1"
  echo "Prebuilt SHA1:      $prebuilt_sha1"
  echo "Built SHA256:       $built_sha256"
  echo "Prebuilt SHA256:    $prebuilt_sha256"
  echo

  if cmp -s "$built_artifact" "$prebuilt_artifact"; then
    echo "Binary compare:     exact match"
    return 0
  fi

  echo "Binary compare:     differ"

  first_diff_line="$(cmp -l "$built_artifact" "$prebuilt_artifact" 2>/dev/null | head -n 1 || true)"
  if [[ -n "$first_diff_line" ]]; then
    first_diff_byte="$(awk '{print $1}' <<< "$first_diff_line")"
    first_diff_start=0
    if (( first_diff_byte > 16 )); then
      first_diff_start=$((first_diff_byte - 16))
    fi

    echo "First differing byte (1-based): $first_diff_byte"
    echo
    echo "Built bytes near first difference (offset=${first_diff_start}, len=64):"
    od -An -tx1 -v -j "$first_diff_start" -N 64 "$built_artifact" || true
    echo
    echo "Prebuilt bytes near first difference (offset=${first_diff_start}, len=64):"
    od -An -tx1 -v -j "$first_diff_start" -N 64 "$prebuilt_artifact" || true
    echo
  fi

  echo "First 20 cmp -l lines (byte, built-octal, prebuilt-octal):"
  cmp -l "$built_artifact" "$prebuilt_artifact" 2>/dev/null | head -n 20 || true
}

# Public function for source-from-image-gen usage.
get_fsbl_artifact() {
  local rc=0

  fsbl_init_defaults
  fsbl_init_logging
  fsbl_enable_command_trace

  fsbl_validate_mode || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    fsbl_validate_required_context || rc=$?
  fi
  if [[ "$rc" -eq 0 ]]; then
    fsbl_resolve_ref || rc=$?
  fi

  if [[ "$rc" -eq 0 ]]; then
    fsbl_log "Resolved bootloader reference (${FSBL_REF_KIND}): ${FSBL_RESOLVED_REF}"
  fi

  if [[ "$rc" -eq 0 ]]; then
    case "${BOOTLOADER_SOURCE_MODE}" in
      prebuilt)
        fsbl_handle_prebuilt || rc=$?
        ;;
      source)
        fsbl_handle_source_build || rc=$?
        ;;
    esac
  fi

  if [[ "$rc" -eq 0 ]]; then
    [[ -n "${BOOTLOADER_ARTIFACT_PATH:-}" ]] || fsbl_die "BOOTLOADER_ARTIFACT_PATH was not set by handler." || rc=$?
  fi
  if [[ "$rc" -eq 0 ]]; then
    [[ -f "${BOOTLOADER_ARTIFACT_PATH}" ]] || fsbl_die "Bootloader artifact does not exist: ${BOOTLOADER_ARTIFACT_PATH}" || rc=$?
  fi
  if [[ "$rc" -eq 0 ]]; then
    fsbl_emit_artifact_metadata "$BOOTLOADER_ARTIFACT_PATH" || rc=$?
  fi
  if [[ "$rc" -eq 0 ]]; then
    fsbl_emit_next_step_commands "$BOOTLOADER_ARTIFACT_PATH" || rc=$?
  fi

  if [[ "$rc" -eq 0 ]]; then
    fsbl_log "Completed at: $(date)"
  fi

  fsbl_disable_command_trace || true
  fsbl_restore_logging || true
  return "$rc"
}

fsbl_parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --sysimage)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--sysimage requires an argument"
        }
        SYSIMAGE="$2"
        shift 2
        ;;
      --mode)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--mode requires an argument"
        }
        BOOTLOADER_SOURCE_MODE="$2"
        shift 2
        ;;
      --ref)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--ref requires an argument"
        }
        UBOOT_REF="$2"
        shift 2
        ;;
      --sha)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--sha requires an argument"
        }
        UBOOT_SHA="$2"
        shift 2
        ;;
      --repo)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--repo requires an argument"
        }
        UBOOT_REPO_URL="$2"
        shift 2
        ;;
      --downloads-dir)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--downloads-dir requires an argument"
        }
        DOWNLOADS_DIR="$2"
        shift 2
        ;;
      --work-dir)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--work-dir requires an argument"
        }
        WORK_DIR="$2"
        shift 2
        ;;
      --build-cmd)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--build-cmd requires an argument"
        }
        UBOOT_BUILD_CMD="$2"
        shift 2
        ;;
      --artifact-path)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--artifact-path requires an argument"
        }
        UBOOT_ARTIFACT_PATH="$2"
        shift 2
        ;;
      --artifact-glob)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--artifact-glob requires an argument"
        }
        UBOOT_ARTIFACT_GLOB="$2"
        shift 2
        ;;
      --cross-compile)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--cross-compile requires an argument"
        }
        UBOOT_CROSS_COMPILE="$2"
        shift 2
        ;;
      --defconfig)
        [[ $# -lt 2 ]] && {
          fsbl_usage
          fsbl_die "--defconfig requires an argument"
        }
        UBOOT_DEFCONFIG="$2"
        shift 2
        ;;
      --diff)
        FSBL_DIFF=1
        shift
        ;;
      --trace)
        FSBL_TRACE=1
        shift
        ;;
      -h | --help)
        fsbl_usage
        FSBL_SHOW_HELP=1
        return 0
        ;;
      *)
        fsbl_usage
        fsbl_die "Unknown argument: $1"
        ;;
    esac
  done
}

fsbl_load_sysimage_config_if_needed() {
  # If caller already provided machine-specific values, no need to resolve again.
  if [[ -n "${BOOTLOADER_PROJECT:-}" && -n "${BOOTLOADER_TAG:-}" && -n "${DTBPATH:-}" ]]; then
    return 0
  fi

  if ! declare -F log >/dev/null 2>&1; then
    log() { fsbl_log "$@"; }
  fi
  if ! declare -F log_warn >/dev/null 2>&1; then
    log_warn() { fsbl_log_warn "$@"; }
  fi
  if ! declare -F die >/dev/null 2>&1; then
    die() { fsbl_die "$@"; }
  fi

  # shellcheck source=/dev/null
  . "$FSBL_SCRIPT_DIR/sysimage-config.sh"
  configure_sysimage
}

fsbl_main() {
  fsbl_parse_args "$@"
  if [[ "$FSBL_SHOW_HELP" -eq 1 ]]; then
    return 0
  fi

  [[ -n "${SYSIMAGE:-}" ]] || {
    fsbl_usage
    fsbl_die "--sysimage is required in standalone mode"
  }

  fsbl_load_sysimage_config_if_needed
  get_fsbl_artifact

  if [[ "$FSBL_DIFF" -eq 1 ]]; then
    if [[ "$BOOTLOADER_SOURCE_MODE" != "source" ]]; then
      fsbl_log_warn "--diff is ignored unless --mode source is selected."
    else
      fsbl_compare_source_vs_prebuilt "$BOOTLOADER_ARTIFACT_PATH"
    fi
  fi

  fsbl_log "Artifact path: ${BOOTLOADER_ARTIFACT_PATH:-unset}"
  fsbl_log "Log file: ${FSBL_LOGFILE}"
  printf '%s\n' "${BOOTLOADER_ARTIFACT_PATH:-}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  fsbl_main "$@"
fi
