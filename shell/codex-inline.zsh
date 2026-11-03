# Keep SSH Codex behavior unchanged. Locally, disable the shared daemon for
# interactive sessions so each terminal session is identified independently.
_codex_local_interactive() {
  local arg index=1
  for arg in "$@"; do
    case "$arg" in
      --remote|--remote=*|--remote-auth-token-env|--remote-auth-token-env=*) return 1 ;;
      --no-daemon) return 1 ;;
    esac
  done

  # Skip recognized leading global flags so options such as --yolo do not
  # hide a following resume/fork command. Unknown leading options fail closed.
  while (( index <= $# )); do
    arg="${@[$index]}"
    case "$arg" in
      -c|--config|--model|-m|--profile|-p|--cd|-C|--add-dir|--sandbox|-s|-a|-i|--image|--local-provider|--ask-for-approval|--enable|--disable)
        (( index += 2 ))
        ;;
      --yolo|--full-auto|--dangerously-bypass-approvals-and-sandbox|--approve-for-me|--worktree|--strict-config|--search|--no-alt-screen|--no-fullscreen|--oss)
        (( index += 1 ))
        ;;
      -*) return 1 ;;
      *) break ;;
    esac
  done

  # Recognize the explicit interactive entrypoints, plus a bare invocation or
  # prompt. Treat known command names as subcommands and leave them alone.
  arg="${@[$index]-}"
  case "$arg" in
    resume|fork) return 0 ;;
    exec|e|app-server|exec-server|review|login|logout|mcp|plugin|remote-control|app|completion|update|doctor|sandbox|debug|apply|a|agents|queue|archive|delete|migrate-rollouts|migrate-nfs-db|unarchive|cloud|features|help)
      return 1 ;;
    -*) return 1 ;;
    *) return 0 ;;
  esac
}

codex() {
  if [[ -n "$SSH_CONNECTION" && -t 0 && -t 1 && -z "$CODEX_NO_MOBILE" ]]; then
    command codex -c 'tui.alternate_screen="always"' -c 'tui.fullscreen_transcript=true' -c 'tui.raw_output_mode=false' "$@"
  elif [[ -z "$SSH_CONNECTION" && -t 0 && -t 1 ]] && _codex_local_interactive "$@"; then
    command codex --no-daemon "$@"
  else
    command codex "$@"
  fi
}
