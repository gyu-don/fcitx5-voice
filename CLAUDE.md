# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

fcitx5-voice is a voice input plugin for fcitx5 using OpenAI Whisper speech recognition. It consists of two main components that communicate via D-Bus:

1. **Python Daemon** (`daemon/`) - Background service that handles audio recording and Whisper transcription
2. **C++ Plugin** (`plugin/`) - fcitx5 InputMethodEngine that provides hotkey integration and text injection

## Architecture

```
User presses Ctrl+Alt+V
    ↓
fcitx5 VoiceEngine (C++) catches KeyEvent
    ↓
D-Bus method call: StartRecording()
    ↓
Python daemon starts audio capture
    ↓
Audio processed by Whisper model
    ↓
D-Bus signal: TranscriptionComplete(text)
    ↓
C++ plugin receives signal via callback
    ↓
ic->commitString(text) injects into application
```

### D-Bus Interface

Service: `org.fcitx.Fcitx5.Voice`
Object: `/org/fcitx/Fcitx5/Voice`

**Methods:**
- `StartRecording()` - Begin audio capture
- `StopRecording()` - End audio capture
- `GetStatus() -> string` - Returns "idle" or "recording"

**Signals:**
- `TranscriptionComplete(text: string, segment_num: int32)` - Emitted when transcription finishes
- `RecordingStarted()` - Recording began
- `RecordingStopped()` - Recording ended
- `Error(message: string)` - Error occurred

## Build and Development

### Initial Setup

```bash
# Install system dependencies (Arch Linux)
sudo pacman -S fcitx5 extra-cmake-modules dbus python

# Install Python dependencies
uv sync

# Build and install everything
./scripts/install.sh
```

### Development Workflow

**Python daemon only (faster iteration):**
```bash
# Edit daemon/*.py files
# Restart daemon
systemctl --user restart fcitx5-voice-daemon

# View logs
journalctl --user -u fcitx5-voice-daemon -f
```

**C++ plugin (requires rebuild):**
```bash
cd build
make -j$(nproc)
make install

# Restart fcitx5 via KDE (don't use pkill - KDE manages fcitx5)
# Use: fcitx5 -r  (if not in KDE Wayland)
```

### Testing Components Independently

**Test daemon via D-Bus:**
```bash
# Check status
gdbus call --session --dest org.fcitx.Fcitx5.Voice \
  --object-path /org/fcitx/Fcitx5/Voice \
  --method org.fcitx.Fcitx5.Voice.GetStatus

# Monitor signals
gdbus monitor --session --dest org.fcitx.Fcitx5.Voice

# Trigger recording
gdbus call --session --dest org.fcitx.Fcitx5.Voice \
  --object-path /org/fcitx/Fcitx5/Voice \
  --method org.fcitx.Fcitx5.Voice.StartRecording
```

**Debug C++ plugin:**
```bash
# Check if plugin loaded
qdbus org.fcitx.Fcitx5 /addon org.fcitx.Fcitx.AddonManager1.Addons | grep -i voice

# Check input methods
fcitx5-remote -a | grep -i voice

# Verify plugin file
ls -lh ~/.local/lib/fcitx5/voice.so
nm -D ~/.local/lib/fcitx5/voice.so | grep fcitx_addon_factory
```

## Critical Installation Details

### systemd Service Sandboxing

The daemon runs with `ProtectHome=read-only` but needs write access to HuggingFace cache:

```ini
# In ~/.config/systemd/user/fcitx5-voice-daemon.service
ReadWritePaths=%h/.cache/huggingface
```

Without this, the Whisper model download will fail with "Read-only file system" error.

### fcitx5 Plugin Loading

**Common issue:** Plugin builds successfully but fcitx5 doesn't load it.

**Root cause:** fcitx5 searches for addons in system paths (e.g., `/usr/lib/fcitx5`), but `make install` with `~/.local` prefix installs to `~/.local/lib/fcitx5`.

**Solutions:**
1. **Recommended:** Install to system location (requires sudo):
   ```bash
   cd build
   cmake .. -DCMAKE_INSTALL_PREFIX=/usr
   sudo make install
   ```

2. **User-local:** Ensure fcitx5 searches `~/.local`:
   - Check: `pkg-config --variable=addondir fcitx5`
   - fcitx5 should automatically search XDG paths, but verify the addon appears in:
     ```bash
     qdbus org.fcitx.Fcitx5 /addon org.fcitx.Fcitx.AddonManager1.Addons
     ```

### KDE Wayland Integration

On KDE Wayland, fcitx5 is managed by KWin:
- Don't use `pkill fcitx5` - it will be relaunched by KDE with potential issues
- Use `fcitx5 -r` to restart, or configure via System Settings → Virtual Keyboard
- Set "Fcitx 5" as the virtual keyboard in KDE settings

## Code Architecture

### Python Daemon (`daemon/`)

**`main.py`**: Entry point, signal handling, GLib main loop setup
**`dbus_service.py`**: Publishes D-Bus service, coordinates recorder ↔ transcriber
**`recorder.py`**: Real-time audio capture with silence detection (sounddevice)
**`transcriber.py`**: Whisper model wrapper (faster-whisper), manages temp files

Key flow in `dbus_service.py`:
1. `StartRecording()` → `recorder.start()` → async audio capture
2. Recorder detects silence → calls `on_audio_segment(audio_data, segment_num)`
3. `transcriber.transcribe(audio_data)` → Whisper inference
4. `TranscriptionComplete` signal emitted with text

### C++ Plugin (`plugin/`)

**`voice_engine.cpp`**: InputMethodEngineV2 implementation
- `keyEvent()`: Intercepts `Ctrl+Alt+V` hotkey
- `onTranscriptionComplete()`: Receives D-Bus signal, calls `ic->commitString(text)`
- `activate()`/`deactivate()`: Lifecycle hooks
- **IOEvent integration**: D-Bus file descriptor is registered with fcitx5's event loop using `addIOEvent()`

**`dbus_client.cpp`**: D-Bus wrapper using libdbus-1 (not GDBus)
- `callMethod()`: Synchronous method calls
- `messageFilter()`: Static callback for signals
- `processEvents()`: Dispatches D-Bus messages when FD becomes readable
- `getFileDescriptor()`: Returns D-Bus connection FD for event loop integration

**CRITICAL**: D-Bus signals are received via **IOEvent** (file descriptor watching), NOT timer-based polling. The match rule must NOT include `sender=` because D-Bus matches on unique names (`:1.XXX`), not well-known names.

**`voice_engine_factory.cpp`**: Plugin registration via `FCITX_ADDON_FACTORY_V2` macro

### Build System

Root `CMakeLists.txt`:
- Finds `Fcitx5Core` package
- Includes Fcitx5 compiler settings
- Adds `plugin/` subdirectory

`plugin/CMakeLists.txt`:
- Links against `Fcitx5::Core`, `Fcitx5::Utils`, `dbus-1`
- Builds as MODULE (not SHARED - no `lib` prefix)
- Installs to `${CMAKE_INSTALL_LIBDIR}/fcitx5/`
- Installs configs to `${CMAKE_INSTALL_DATADIR}/fcitx5/{addon,inputmethod}/`

## Configuration Files

**`plugin/voice.conf`**: Input method metadata
- Appears in fcitx5 IM selector
- Icon: `audio-input-microphone`, Label: 🎤

**`plugin/voice-addon.conf.in`**: Addon metadata
- Category: InputMethod
- Library: `voice` (loads `voice.so`)
- OnDemand: True (loads when IM is activated)

**`systemd/fcitx5-voice-daemon.service`**: User service
- Type=dbus, BusName=org.fcitx.Fcitx5.Voice
- Sandboxed with ProtectHome/ProtectSystem

## Common Modifications

### Change Whisper Model

Edit `daemon/transcriber.py`:
```python
MODEL_SIZE = "small"  # Options: tiny, base, small, medium, large-v3-turbo
```
Restart daemon: `systemctl --user restart fcitx5-voice-daemon`

### Adjust Recording Sensitivity

Edit `daemon/recorder.py`:
```python
SILENCE_THRESHOLD = 0.01   # Lower = more sensitive
SILENCE_DURATION = 1.0     # Seconds before auto-stop
MAX_DURATION = 15.0        # Max recording length
```

### Change Hotkey

Edit `plugin/voice_engine.cpp`:
```cpp
if (event.key().check(FcitxKey_v, KeyState::Ctrl_Alt)) {
```
Change to different key combination, rebuild plugin.

## Troubleshooting

### Daemon fails with "Read-only file system"
Add `ReadWritePaths=%h/.cache/huggingface` to systemd service file.

### Plugin not recognized by fcitx5
Verify installation path matches fcitx5's search paths. Check `qdbus` addon list.

### No D-Bus connection
Ensure daemon is running: `systemctl --user status fcitx5-voice-daemon`

### Hotkey doesn't work
Verify Voice IM is active (`fcitx5-remote -a`), check for conflicting keybindings.

### D-Bus signals not received (TranscriptionComplete doesn't work)
**Root cause**: Improper event loop integration or incorrect match rule.

**Solution**: The plugin MUST use `addIOEvent()` to watch the D-Bus file descriptor, NOT timer-based polling. The match rule format is:
```cpp
"type='signal',interface='org.fcitx.Fcitx5.Voice',path='/org/fcitx/Fcitx5/Voice'"
```
Do NOT include `sender='org.fcitx.Fcitx5.Voice'` because D-Bus match rules match on unique bus names (`:1.495`), not well-known service names.

**Testing**: To isolate fcitx5 integration issues from D-Bus issues, add a test hotkey that directly calls `onTranscriptionComplete()` with test text, bypassing D-Bus.

### High memory usage (~1.5GB)
Normal - Whisper medium model is loaded in RAM. Use smaller model to reduce.

## File Locations After Install

- Plugin: `~/.local/lib/fcitx5/voice.so` (or `/usr/lib/fcitx5/voice.so`)
- Addon config: `~/.local/share/fcitx5/addon/voice.conf`
- IM config: `~/.local/share/fcitx5/inputmethod/voice.conf`
- Daemon binary: `~/.local/bin/fcitx5-voice-daemon`
- Systemd service: `~/.config/systemd/user/fcitx5-voice-daemon.service`
- User profile: `~/.config/fcitx5/profile` (Voice IM registered here)
