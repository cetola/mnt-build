# bash completion for mnt-build
_mnt_build_complete() {
  local cur prev words cword
  COMPREPLY=()
  _get_comp_words_by_ref -n : cur prev words cword 2>/dev/null || {
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    words=("${COMP_WORDS[@]}")
    cword="$COMP_CWORD"
  }

  local global_opts="--help --version"
  local subcommands="build clean uboot barebox"
  local sysimages="pocket-reform-system-a311d reform-next-system-rk3588 pocket-reform-system-rk3588 pocket-reform-system-imx8mp pocket-reform-system-rk3588s"

  # Resolve subcommand, if any.
  local subcmd=""
  local i
  for ((i=1; i<${#words[@]}; i++)); do
    case "${words[i]}" in
      build|clean|uboot|barebox)
        subcmd="${words[i]}"
        break
        ;;
    esac
  done

  if [[ -z "$subcmd" ]]; then
    COMPREPLY=( $(compgen -W "$subcommands $global_opts" -- "$cur") )
    return 0
  fi

  case "$subcmd" in
    build)
      local build_opts="--help --kversion --build-dir -j --jobs --localversion-rev --dry-run --olddefconfig --defconfig --post-clean --skip-git-ops --arch --cross-compile --with-headers --kernel-only --dtbs-only --modules-only --kernel --verruckt"
      case "$prev" in
        --build-dir)
          COMPREPLY=( $(compgen -d -- "$cur") )
          return 0
          ;;
        --kversion|--arch|--cross-compile|--localversion-rev|-j|--jobs|--kernel)
          return 0
          ;;
      esac
      COMPREPLY=( $(compgen -W "$build_opts" -- "$cur") )
      ;;
    clean)
      local clean_opts="--help --build-dir --kernel --kversion"
      case "$prev" in
        --build-dir)
          COMPREPLY=( $(compgen -d -- "$cur") )
          return 0
          ;;
        --kernel|--kversion)
          return 0
          ;;
      esac
      COMPREPLY=( $(compgen -W "$clean_opts" -- "$cur") )
      ;;
    uboot)
      local uboot_opts="--help --list --sysimage --dry-run --diff --menuconfig --clean --reset"
      case "$prev" in
        --list)
          COMPREPLY=( $(compgen -W "sysimage" -- "$cur") )
          return 0
          ;;
        --sysimage)
          COMPREPLY=( $(compgen -W "$sysimages" -- "$cur") )
          return 0
          ;;
      esac
      COMPREPLY=( $(compgen -W "$uboot_opts" -- "$cur") )
      ;;
    barebox)
      local barebox_opts="--help --list --sysimage --dry-run --diff --menuconfig --clean"
      case "$prev" in
        --list)
          COMPREPLY=( $(compgen -W "sysimage" -- "$cur") )
          return 0
          ;;
        --sysimage)
          COMPREPLY=( $(compgen -W "$sysimages" -- "$cur") )
          return 0
          ;;
      esac
      COMPREPLY=( $(compgen -W "$barebox_opts" -- "$cur") )
      ;;
  esac

  return 0
}

complete -F _mnt_build_complete mnt-build
