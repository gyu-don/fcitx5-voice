#!/bin/bash
set -e

# Parse arguments
RIVA_URL="ws://localhost:9000"
LANGUAGE="ja-JP"
LOCAL_INSTALL=false

usage() {
    echo "Usage: $0 [--local] [--url <websocket-url>] [--language <lang-code>]"
    echo ""
    echo "Options:"
    echo "  --local            Install to ~/.local (no sudo required)"
    echo "  --url <url>        NIM Riva WebSocket URL (default: ws://localhost:9000)"
    echo "  --language <code>  Language code (default: ja-JP)"
    echo ""
    echo "Examples:"
    echo "  $0 --local --url ws://my-gpu-server:9000"
    echo "  $0 --url ws://100.64.0.5:9000 --language en-US"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --local)
            LOCAL_INSTALL=true
            shift
            ;;
        --url)
            RIVA_URL="$2"
            shift 2
            ;;
        --language)
            LANGUAGE="$2"
            shift 2
            ;;
        --help|-h)
            usage
            ;;
        *)
            echo "Unknown option: $1"
            usage
            ;;
    esac
done

echo "==> Installing fcitx5-voice plugin"
echo "    URL: $RIVA_URL"
echo "    Language: $LANGUAGE"
if [ "$LOCAL_INSTALL" = true ]; then
    echo "    Mode: local (~/.local, no sudo)"
else
    echo "    Mode: system (/usr, requires sudo)"
fi
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

if [ "$LOCAL_INSTALL" = true ]; then
    cmake .. -DCMAKE_INSTALL_PREFIX="$HOME/.local" -DCMAKE_BUILD_TYPE=Release
    make -j$(nproc)
    echo "✓ C++ plugin built"

    echo "==> Installing C++ plugin to ~/.local..."
    make install
    cd ..
    echo "✓ C++ plugin installed to ~/.local"
else
    cmake .. -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release
    make -j$(nproc)
    echo "✓ C++ plugin built"

    echo "==> Installing C++ plugin (requires sudo)..."
    sudo make install
    cd ..
    echo "✓ C++ plugin installed to /usr"
fi
echo ""

# 3. Install systemd service (with configured URL and language)
echo "==> Installing systemd service..."
mkdir -p ~/.config/systemd/user/
sed -e "s|--url ws://localhost:9000|--url $RIVA_URL|" \
    -e "s|--language ja-JP|--language $LANGUAGE|" \
    systemd/fcitx5-voice-daemon.service > ~/.config/systemd/user/fcitx5-voice-daemon.service
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
fcitx5 -rd
sleep 2
echo "✓ fcitx5 restarted"
echo ""

echo "==> Installation complete!"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  fcitx5-voice is now installed and running!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
if [ "$LOCAL_INSTALL" = true ]; then
    echo "NOTE: Set FCITX_ADDON_DIRS so fcitx5 can find the plugin."
    echo "      Add to ~/.config/environment.d/fcitx5-voice.conf:"
    echo ""
    echo "        FCITX_ADDON_DIRS=/usr/lib/fcitx5:\$HOME/.local/lib/fcitx5"
    echo ""
    echo "      Then log out and log back in."
    echo ""
fi
echo "Usage:"
echo "  • Switch to Voice input method in fcitx5"
echo "  • Press Shift+Space to start recording"
echo "  • Speak into your microphone (partial text shown as preedit)"
echo "  • Press Shift+Space again to stop and commit text"
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
