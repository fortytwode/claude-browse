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

for tool in claude-browse claude-resume; do
    chmod +x "$SCRIPT_DIR/$tool"
    ln -sf "$SCRIPT_DIR/$tool" "$BIN_DIR/$tool"
    echo "  Installed $tool -> $BIN_DIR/$tool"
done

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

# PATH sanity check
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo ""
        echo "  NOTE: $BIN_DIR is not on your PATH. Add this to your shell rc:"
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac

echo ""
echo "Done. Available commands:"
echo "  claude-browse    Interactive session browser (type to filter, Enter to resume)"
echo "  claude-resume    Quick resume by keyword (claude-resume <keyword>)"
