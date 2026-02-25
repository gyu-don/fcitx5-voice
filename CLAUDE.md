# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

fcitx5-voice is a voice input plugin for fcitx5 that streams audio to a remote NVIDIA NIM Riva ASR server via WebSocket for real-time GPU-accelerated transcription. It consists of two main components that communicate via D-Bus:

1. **Python Daemon** (`daemon/`) - Background service that captures audio, streams it via WebSocket to NIM Riva, and forwards transcription results via D-Bus signals
2. **C++ Plugin** (`plugin/`) - fcitx5 InputMethodEngine that provides hotkey integration, preedit display (delta), and text injection (completed)

## Architecture

```
User presses Shift+Space
    ↓
fcitx5 VoiceEngine (C++) catches KeyEvent
    ↓
D-Bus method call: StartRecording()
    ↓
Python daemon starts audio capture + WebSocket connection
    ↓
Audio streamed as PCM16 to NIM Riva via WebSocket
    ↓
NIM Riva sends back delta (partial) and completed (final) events
    ↓
D-Bus signals: TranscriptionDelta(text), TranscriptionComplete(text, 0)
    ↓
C++ plugin: delta → setPreedit (replace), completed → commitString
    ↓
On stop: pending preedit committed immediately as final text
```

### Threading Model

The daemon runs two event loops:
- **GLib main loop** (main thread): D-Bus service via pydbus
- **asyncio event loop** (streaming thread): WebSocket + audio coordination

Bridging: asyncio callbacks use `GLib.idle_add()` to emit D-Bus signals from the GLib thread. Stop signaling uses `threading.Event`.

### D-Bus Interface

Service: `org.fcitx.Fcitx5.Voice`
Object: `/org/fcitx/Fcitx5/Voice`

**Methods:**
- `StartRecording()` - Begin audio streaming to ASR server
- `StopRecording()` - End audio streaming (non-blocking; signals stop and returns immediately, streaming thread cleans up asynchronously)
- `GetStatus() -> string` - Returns "idle" or "recording"

**Signals:**
- `TranscriptionDelta(text: string)` - Partial/streaming transcription result (shown as preedit)
- `TranscriptionComplete(text: string, segment_num: int32)` - Final transcription (committed as text). `segment_num` is always 0 in streaming mode (kept for interface compatibility).
- `RecordingStarted()` - Recording began
- `RecordingStopped()` - Recording ended
- `Error(message: string)` - Error occurred

## Build and Development

### Initial Setup

```bash
# Install system dependencies (Arch Linux)
sudo pacman -S fcitx5 extra-cmake-modules dbus python portaudio

# Install Python dependencies
uv sync

# Build and install everything
./scripts/install.sh
```

### Development Workflow

**Python daemon only (faster iteration):**
```bash
# Edit daemon/*.py files
# Run directly with debug logging
uv run fcitx5-voice-daemon --url ws://localhost:9000 --debug

# Or restart systemd service
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

# Monitor signals (see delta and completed events)
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
ls -lh /usr/lib/fcitx5/voice.so
nm -D /usr/lib/fcitx5/voice.so | grep fcitx_addon_factory
```

## Critical Details

### fcitx5 Plugin Loading

**Common issue:** Plugin builds successfully but fcitx5 doesn't load it.

**Root cause:** fcitx5 searches for addons in system paths (e.g., `/usr/lib/fcitx5`), but `make install` with `~/.local` prefix installs to `~/.local/lib/fcitx5`.

**Solution:** Install to system location (requires sudo):
```bash
cd build
cmake .. -DCMAKE_INSTALL_PREFIX=/usr
sudo make install
```

### KDE Wayland Integration

On KDE Wayland, fcitx5 is managed by KWin:
- Don't use `pkill fcitx5` - it will be relaunched by KDE with potential issues
- Use `fcitx5 -r` to restart, or configure via System Settings → Virtual Keyboard
- Set "Fcitx 5" as the virtual keyboard in KDE settings

### D-Bus Signal Reception

**CRITICAL**: D-Bus signals are received via **IOEvent** (file descriptor watching), NOT timer-based polling. The match rule must NOT include `sender=` because D-Bus matches on unique names (`:1.XXX`), not well-known names:
```cpp
"type='signal',interface='org.fcitx.Fcitx5.Voice',path='/org/fcitx/Fcitx5/Voice'"
```

## Code Architecture

### Python Daemon (`daemon/`)

**`main.py`**: Entry point, CLI argument parsing (`--url`, `--language`, `--model`, `--commit-interval`, `--debug`), GLib main loop setup

**`dbus_service.py`**: D-Bus service that bridges GLib and asyncio:
- `StartRecording()` → spawns asyncio streaming thread
- Streaming thread: connects WebSocket, starts audio capture, runs send/recv tasks
- `GLib.idle_add()` used to emit D-Bus signals from the asyncio thread
- `threading.Event` for stop signaling between threads

**`recorder.py`**: Streaming audio recorder using sounddevice:
- Captures PCM16 (int16) at 16kHz, 100ms chunks
- Thread-safe queue for audio data
- No silence detection - continuous streaming

**`ws_client.py`**: NIM Riva WebSocket client:
- Implements the NIM Riva realtime transcription protocol
- `connect()` → `conversation.created` → `transcription_session.update`
- `send_audio()` sends base64-encoded PCM16 chunks
- `commit()` triggers server-side processing
- `recv_loop()` dispatches delta and completed events via callbacks
- Japanese text cleaning: `replace(" ", "")` for space-separated CJK output

### C++ Plugin (`plugin/`)

**`voice_engine.cpp`**: InputMethodEngineV2 implementation
- `keyEvent()`: Intercepts `Shift+Space` hotkey
- `onTranscriptionDelta()`: Replaces `preedit_text_` with latest delta (server resends full partial text each time), displays via `setClientPreedit()`
- `onTranscriptionComplete()`: Clears preedit, calls `ic->commitString(text)`
- `stopRecording()`: Commits any pending preedit text immediately as final text, then signals daemon to stop
- `activate()`: Shows timed status notification (3s auto-clear)
- `deactivate()`/`reset()`: Clear preedit state

**`dbus_client.cpp`**: D-Bus wrapper using libdbus-1 (not GDBus)
- Handles signals: `TranscriptionComplete`, `TranscriptionDelta`, `Error`
- IOEvent-based signal reception via file descriptor

**`voice_engine_factory.cpp`**: Plugin registration via `FCITX_ADDON_FACTORY_V2` macro

### Build System

Root `CMakeLists.txt`:
- Finds `Fcitx5Core` package
- C++20 standard
- Includes Fcitx5 compiler settings

`plugin/CMakeLists.txt`:
- Links against `Fcitx5::Core`, `Fcitx5::Utils`, `dbus-1`
- Builds as MODULE (not SHARED - no `lib` prefix)
- Installs to `${CMAKE_INSTALL_LIBDIR}/fcitx5/`

## Configuration

### Daemon CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `--url` | `ws://localhost:9000` | NIM Riva WebSocket URL |
| `--language` | `ja-JP` | Language code |
| `--model` | `parakeet-rnnt-1.1b-...` | ASR model name |
| `--commit-interval` | `10` | Commit every N chunks (N * 100ms) |
| `--debug` | off | Debug logging |

### Change Hotkey

Edit `plugin/voice_engine.cpp`:
```cpp
if (event.key().check(FcitxKey_space, KeyState::Shift)) {
```
Change to different key combination, rebuild plugin.

## Troubleshooting

### WebSocket connection fails
Ensure SSH tunnel or Tailscale is active. Test: `python3 -c "import websockets, asyncio; asyncio.run(websockets.connect('ws://localhost:9000'))"`

### Plugin not recognized by fcitx5
Verify installation path matches fcitx5's search paths. Check `qdbus` addon list.

### No D-Bus connection
Ensure daemon is running: `systemctl --user status fcitx5-voice-daemon`

### Hotkey doesn't work
Verify Voice IM is active (`fcitx5-remote -a`), check for conflicting keybindings.

## File Locations After Install

- Plugin: `/usr/lib/fcitx5/voice.so`
- Addon config: `/usr/share/fcitx5/addon/voice.conf`
- IM config: `/usr/share/fcitx5/inputmethod/voice.conf`
- Daemon binary: `~/.local/bin/fcitx5-voice-daemon`
- Systemd service: `~/.config/systemd/user/fcitx5-voice-daemon.service`
- User profile: `~/.config/fcitx5/profile`
