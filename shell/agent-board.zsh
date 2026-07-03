# Source this from ~/.zshrc to get frictionless detach/reattach + a board glance.
#
# work <name>  -- attach to tmux session <name>, creating it if it doesn't exist.
#                 Detach with Ctrl-b then d; the session keeps running.
# aj           -- print the agent-board (all active Claude Code sessions + resume cmds).
#
# NOTE: zsh has a builtin `jobs` (shell job control) -- we deliberately do NOT
# shadow it. Use `aj` (agent-jobs) instead. If you want to override the
# builtin anyway, alias it yourself: alias jobs=aj

work() {
    if [ -z "$1" ]; then
        echo "usage: work <session-name>" >&2
        return 1
    fi
    tmux new-session -A -s "$1"
}

aj() {
    agent-board board
}
