# Native Codex over SSH, with compact tools and a complete scrollable transcript.
# This config override also works for `resume`/`fork` and is harmless for
# non-UI subcommands. Explicit user flags later in argv retain precedence.
# Local terminals and non-interactive invocations keep their normal defaults.
codex() {
  if [[ -n "$SSH_CONNECTION" && -t 0 && -t 1 && -z "$CODEX_NO_MOBILE" ]]; then
    command codex -c 'tui.alternate_screen="always"' -c 'tui.fullscreen_transcript=true' -c 'tui.raw_output_mode=false' "$@"
  else
    command codex "$@"
  fi
}
