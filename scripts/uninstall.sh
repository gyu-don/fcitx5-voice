#!/bin/bash
set -e

echo "==> Uninstalling fcitx5-voice plugin"
echo ""

# 1. Stop and disable systemd service
echo "==> Stopping daemon..."
systemctl --user stop fcitx5-voice-daemon.service || true
systemctl --user disable fcitx5-voice-daemon.service || true
rm -f ~/.config/systemd/user/fcitx5-voice-daemon.service
systemctl --user daemon-reload
echo "✓ Daemon stopped and removed"
echo ""

# 2. Remove C++ plugin (requires sudo)
echo "==> Removing C++ plugin..."
sudo rm -f /usr/lib/fcitx5/voice.so
sudo rm -f /usr/share/fcitx5/addon/voice.conf
sudo rm -f /usr/share/fcitx5/inputmethod/voice.conf
echo "✓ Plugin removed from /usr"
echo ""

# 3. Remove daemon binary
echo "==> Removing daemon..."
rm -f ~/.local/bin/fcitx5-voice-daemon
echo "✓ Daemon binary removed"
echo ""

# 4. Remove D-Bus interface
rm -f ~/.local/share/dbus-1/interfaces/org.fcitx.Fcitx5.Voice.xml
echo "✓ D-Bus interface removed"
echo ""

# 5. Restart fcitx5
echo "==> Restarting fcitx5..."
fcitx5 -r || true
echo "✓ fcitx5 restarted"
echo ""

echo "==> Uninstallation complete!"
echo ""
echo "Note: Python packages are still installed in the virtual environment."
echo "To remove the entire project, delete the project directory."
