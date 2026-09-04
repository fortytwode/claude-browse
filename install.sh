#!/bin/bash
# Install claude-browse from a git clone.
#
# Symlinks the scripts into ~/.local/bin (must be on PATH).
#
# If you'd rather install via pip: `pip install claude-browse`. This script is
# for users who git-clone the repo and want the tools on their PATH without
# touching pip.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$BIN_DIR"

for tool in claude-browse codex-browse gemini-browse copilot-browse cursor-browse codex-mobile-json codexmobile agent-board; do
    chmod +x "$SCRIPT_DIR/$tool"
    ln -sf "$SCRIPT_DIR/$tool" "$BIN_DIR/$tool"
    echo "  Installed $tool -> $BIN_DIR/$tool"
done

if [ -L "$BIN_DIR/claude-resume" ]; then
    rm "$BIN_DIR/claude-resume"
    echo "  Removed deprecated claude-resume symlink"
elif [ -f "$BIN_DIR/claude-resume" ]; then
    echo "  Note: $BIN_DIR/claude-resume exists and isn't managed by this repo"
    echo "        Remove manually if you want it gone: rm $BIN_DIR/claude-resume"
fi

# Remove deprecated claude-search symlink (pre-v0.1 naming)
if [ -L "$BIN_DIR/claude-search" ]; then
    rm "$BIN_DIR/claude-search"
    echo "  Removed legacy claude-search symlink"
elif [ -f "$BIN_DIR/claude-search" ]; then
    echo "  Note: $BIN_DIR/claude-search exists and isn't managed by this repo"
    echo "        Remove manually if you want it gone: rm $BIN_DIR/claude-search"
fi

# fzf dependency check — give OS-appropriate install instructions
if ! command -v fzf >/dev/null 2>&1; then
    echo ""
    echo "  WARNING: fzf is not installed. claude-browse requires it."

    os="$(uname -s)"
    case "$os" in
        Darwin)
            echo "  Install with:  brew install fzf"
            ;;
        Linux)
            # Prefer the package manager the system actually uses
            if command -v apt >/dev/null 2>&1; then
                echo "  Install with:  sudo apt install fzf"
            elif command -v dnf >/dev/null 2>&1; then
                echo "  Install with:  sudo dnf install fzf"
            elif command -v pacman >/dev/null 2>&1; then
                echo "  Install with:  sudo pacman -S fzf"
            elif command -v zypper >/dev/null 2>&1; then
                echo "  Install with:  sudo zypper install fzf"
            elif command -v apk >/dev/null 2>&1; then
                echo "  Install with:  sudo apk add fzf"
            else
                echo "  Install via your package manager, or from source:"
                echo "    https://github.com/junegunn/fzf#installation"
            fi
            ;;
        *)
            echo "  See https://github.com/junegunn/fzf#installation"
            ;;
    esac
fi

# tmux dependency check (agent-board's work/aj shell aliases)
if ! command -v tmux >/dev/null 2>&1; then
    echo ""
    echo "  WARNING: tmux is not installed. agent-board's \`work\` (attach-or-create)"
    echo "  alias needs it; the board CLI (\`aj\`) works without it."
    os="$(uname -s)"
    case "$os" in
        Darwin) echo "  Install with:  brew install tmux" ;;
        Linux)
            if command -v apt >/dev/null 2>&1; then
                echo "  Install with:  sudo apt install tmux"
            elif command -v dnf >/dev/null 2>&1; then
                echo "  Install with:  sudo dnf install tmux"
            else
                echo "  Install via your package manager, or see https://github.com/tmux/tmux"
            fi
            ;;
        *) echo "  See https://github.com/tmux/tmux" ;;
    esac
fi

# PATH sanity check
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo ""
        echo "  NOTE: $BIN_DIR is not on your PATH. Add this to your shell rc:"
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

# Wire agent-board hooks + statusLine into ~/.claude/settings.json (idempotent, backs up first)
echo ""
echo "Agent Board setup:"
python3 "$SCRIPT_DIR/scripts/install_agent_board.py"

echo ""
echo "  To enable \`work <name>\` (tmux attach-or-create) and \`aj\` (board glance),"
echo "  add this line to your shell rc (~/.zshrc):"
echo "    source \"$SCRIPT_DIR/shell/agent-board.zsh\""

if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    echo ""
    echo "  --- agent-board sync check (Firestore + Slack connectivity) ---"
    "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/agent-board" sync check || true
else
    echo ""
    echo "  NOTE: no .venv found, so cross-laptop sync (Firestore/Slack) isn't set up yet."
    echo "  The local loop (statusline, notifications, jobs board) works without it."
    echo "  To enable sync:"
    echo "    python3 -m venv \"$SCRIPT_DIR/.venv\""
    echo "    \"$SCRIPT_DIR/.venv/bin/pip\" install -e \"$SCRIPT_DIR[board-sync]\""
    echo "  Agent Board automatically uses it on the next hook event; no hook rewiring"
    echo "  or installer re-run is needed."
fi

echo ""
echo "Done. Available commands:"
echo "  claude-browse    Interactive browser that opens everything in Claude by default"
echo "  codex-browse     Interactive browser that opens everything in CodeX by default"
echo "  gemini-browse    Interactive browser that opens everything in Gemini by default"
echo "  copilot-browse   Interactive browser that opens everything in Copilot by default"
echo "  cursor-browse    Interactive browser that opens everything in Cursor by default"
echo "  codex-mobile-json  Experimental mobile-safe Codex JSON transcript runner"
echo "  codexmobile      Short alias for codex-mobile-json"
echo "  agent-board      Session status board (hook/statusline/board/sync subcommands)"
echo "  aj               (after sourcing shell/agent-board.zsh) glance at all active sessions"
echo "  work <name>      (after sourcing shell/agent-board.zsh) tmux attach-or-create"
