# Installation Notes for fcitx5-voice

## Current Status

✅ **Completed:**
- Python daemon implementation (D-Bus service, recorder, transcriber)
- C++ fcitx5 plugin source code (engine, D-Bus client, factory)
- CMake build system configuration
- D-Bus interface definition
- Systemd service file
- Installation and uninstallation scripts
- Comprehensive README documentation

✅ **Tested:**
- Python modules import successfully
- Daemon entry point is available
- Dependencies (dbus-1, cmake) are present

❌ **Missing System Dependency:**
- `fcitx5` development package (headers and libraries)

## Next Steps

### 1. Install fcitx5 Development Package

On Arch Linux:
```bash
sudo pacman -S fcitx5 extra-cmake-modules
```

On Ubuntu/Debian:
```bash
sudo apt install fcitx5-core-dev libdbus-1-dev cmake build-essential
```

On Fedora:
```bash
sudo dnf install fcitx5-devel dbus-devel cmake gcc-c++
```

### 2. Run the Installation Script

Once fcitx5 is installed:
```bash
./scripts/install.sh
```

This will:
1. ✓ Install Python daemon (already done)
2. Build and install C++ plugin
3. Set up systemd service
4. Restart fcitx5

### 3. Test the Plugin

After installation:
```bash
# Test D-Bus interface
gdbus call --session \
  --dest org.fcitx.Fcitx5.Voice \
  --object-path /org/fcitx/Fcitx5/Voice \
  --method org.fcitx.Fcitx5.Voice.GetStatus

# Check daemon logs
journalctl --user -u fcitx5-voice-daemon -f

# In any text application: Press Ctrl+Alt+V to start voice input
```

## Alternative: Test Python Daemon Only

You can test the daemon functionality without the C++ plugin:

```bash
# Start daemon manually
uv run fcitx5-voice-daemon --debug

# In another terminal, trigger via D-Bus
gdbus call --session \
  --dest org.fcitx.Fcitx5.Voice \
  --object-path /org/fcitx/Fcitx5/Voice \
  --method org.fcitx.Fcitx5.Voice.StartRecording

# Speak into microphone, then monitor for TranscriptionComplete signal
gdbus monitor --session --dest org.fcitx.Fcitx5.Voice
```

## Files Created

### Python Daemon (daemon/)
- `__init__.py` - Package marker
- `main.py` - Entry point with CLI argument parsing
- `dbus_service.py` - D-Bus interface implementation
- `recorder.py` - Real-time audio recording with silence detection
- `transcriber.py` - Whisper model wrapper

### C++ Plugin (plugin/)
- `voice_engine.h/cpp` - Main fcitx5 plugin (InputMethodEngineV2)
- `dbus_client.h/cpp` - D-Bus communication wrapper
- `voice_engine_factory.cpp` - Plugin registration
- `voice-addon.conf.in` - Addon metadata
- `voice.conf` - Input method configuration
- `CMakeLists.txt` - Build configuration

### Configuration
- `dbus/org.fcitx.Fcitx5.Voice.xml` - D-Bus interface definition
- `systemd/fcitx5-voice-daemon.service` - Systemd unit file
- `CMakeLists.txt` - Root build configuration
- `pyproject.toml` - Updated with dependencies and entry point

### Scripts
- `scripts/install.sh` - Automated installation
- `scripts/uninstall.sh` - Automated uninstallation

### Documentation
- `README.md` - Comprehensive documentation
- `INSTALL_NOTES.md` - This file

## Troubleshooting

If you encounter issues during installation, check:

1. **Python dependencies**: `uv sync` should work without errors
2. **System packages**: All fcitx5 development packages installed
3. **CMake version**: Should be >= 3.22
4. **Compiler**: GCC with C++20 support

For detailed troubleshooting, see README.md.
