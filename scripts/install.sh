#!/bin/bash
set -e

echo "==> Installing fcitx5-voice plugin"
echo ""

# 1. Install Python daemon and dependencies
echo "==> Installing Python daemon..."
uv sync
echo "✓ Python dependencies installed"

# Create symlink for daemon binary
mkdir -p ~/.local/bin
DAEMON_PATH=$(uv run which fcitx5-voice-daemon 2>/dev/null || echo "")
if [ -n "$DAEMON_PATH" ]; then
    ln -sf "$DAEMON_PATH" ~/.local/bin/fcitx5-voice-daemon
    echo "✓ Daemon binary linked to ~/.local/bin/fcitx5-voice-daemon"
else
    echo "⚠ Warning: Could not find fcitx5-voice-daemon binary"
    echo "  Will try to install package in editable mode..."
    uv pip install -e .
    DAEMON_PATH=$(uv run which fcitx5-voice-daemon)
    ln -sf "$DAEMON_PATH" ~/.local/bin/fcitx5-voice-daemon
    echo "✓ Daemon binary linked"
fi
echo ""

# 2. Build and install C++ plugin
echo "==> Building C++ plugin..."
mkdir -p build
cd build
cmake .. -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
echo "✓ C++ plugin built"

echo "==> Installing C++ plugin (requires sudo)..."
sudo make install
cd ..
echo "✓ C++ plugin installed to /usr"
echo ""

# 3. Install systemd service
echo "==> Installing systemd service..."
mkdir -p ~/.config/systemd/user/
cp systemd/fcitx5-voice-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable fcitx5-voice-daemon.service
echo "✓ Systemd service installed and enabled"

echo "==> Starting daemon..."
systemctl --user start fcitx5-voice-daemon.service
sleep 2  # Wait for daemon to start
echo "✓ Daemon started"
echo ""

# 4. Install D-Bus interface (for reference)
echo "==> Installing D-Bus interface definition..."
mkdir -p ~/.local/share/dbus-1/interfaces/
cp dbus/org.fcitx.Fcitx5.Voice.xml ~/.local/share/dbus-1/interfaces/
echo "✓ D-Bus interface installed"
echo ""

# 5. Restart fcitx5
echo "==> Restarting fcitx5..."
fcitx5 -r
sleep 2
echo "✓ fcitx5 restarted"
echo ""

echo "==> Installation complete!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  fcitx5-voice is now installed and running!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Usage:"
echo "  • Press Ctrl+Alt+V in any text field to start voice input"
echo "  • Speak into your microphone"
echo "  • Stop speaking (silence will auto-detect and transcribe)"
echo "  • Text will appear at your cursor position"
echo ""
echo "Troubleshooting:"
echo "  • Check daemon status:"
echo "      systemctl --user status fcitx5-voice-daemon"
echo ""
echo "  • View daemon logs:"
echo "      journalctl --user -u fcitx5-voice-daemon -f"
echo ""
echo "  • Test D-Bus connection:"
echo "      gdbus call --session --dest org.fcitx.Fcitx5.Voice \\"
echo "        --object-path /org/fcitx/Fcitx5/Voice \\"
echo "        --method org.fcitx.Fcitx5.Voice.GetStatus"
echo ""
echo "  • Check if plugin is loaded:"
echo "      fcitx5-diagnose | grep -i voice"
echo ""
