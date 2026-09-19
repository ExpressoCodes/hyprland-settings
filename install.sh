#!/usr/bin/env bash
# Install hyprland-settings using nix build
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Building hyprland-settings package..."
nix build --print-out-paths

STORE_PATH="$(readlink -f result)"
BINARY="$STORE_PATH/bin/hyprland-settings"

echo "Installing binary to ~/.local/bin/"
install -Dm755 "$BINARY" ~/.local/bin/hyprland-settings

echo "Installing desktop file to ~/.local/share/applications/"
install -Dm644 data/hyprland-settings.desktop \
    ~/.local/share/applications/hyprland-settings.desktop

# Point Exec= at the installed binary
sed -i "s|Exec=.*|Exec=$HOME/.local/bin/hyprland-settings|" \
    ~/.local/share/applications/hyprland-settings.desktop

echo "Updating desktop database..."
update-desktop-database ~/.local/share/applications/ 2>/dev/null || true

echo ""
echo "Done! Run: hyprland-settings"
echo "Or search for 'Hyprland Settings' in rofi/wofi."
