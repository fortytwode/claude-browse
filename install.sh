#!/bin/bash
# Install claude-session-tools
# Symlinks scripts to ~/.local/bin/ (must be on PATH)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$BIN_DIR"

# Install tools
for tool in claude-browse claude-resume; do
    chmod +x "$SCRIPT_DIR/$tool"
    ln -sf "$SCRIPT_DIR/$tool" "$BIN_DIR/$tool"
    echo "  Installed $tool -> $BIN_DIR/$tool"
done

# Remove deprecated claude-search (replaced by claude-browse)
if [ -f "$BIN_DIR/claude-search" ] && [ ! -L "$BIN_DIR/claude-search" ]; then
    echo "  Note: ~/.local/bin/claude-search still exists (not managed by this repo)"
    echo "  You can remove it manually: rm $BIN_DIR/claude-search"
elif [ -L "$BIN_DIR/claude-search" ]; then
    rm "$BIN_DIR/claude-search"
    echo "  Removed claude-search symlink (replaced by claude-browse)"
fi

# Check dependencies
if ! command -v fzf &> /dev/null; then
    echo ""
    echo "  WARNING: fzf is not installed. claude-browse requires it."
    echo "  Install with: brew install fzf"
fi

echo ""
echo "Done. Available commands:"
echo "  claude-browse    Interactive session browser (type to filter, Enter to resume)"
echo "  claude-resume    Quick resume by keyword (claude-resume gravy pass)"
