# fcitx5-voice

Voice input plugin for fcitx5 using GPU-accelerated real-time ASR via NVIDIA NIM Riva.

## Overview

fcitx5-voice captures microphone audio, streams it over WebSocket to an NVIDIA NIM Riva ASR server running on a GPU machine, and injects the transcribed text into any Linux application via fcitx5.

### Key features

- **Real-time streaming** - Audio is streamed continuously; partial results appear inline as you speak
- **GPU-accelerated** - Leverages NVIDIA NIM Riva on a remote GPU server for fast inference
- **fcitx5 integration** - Works in any text field on Linux (Wayland/X11)
- **Preedit display** - Partial (delta) transcription shown as uncommitted text, like IME composition

### Architecture

```
┌─────────────────────────────────────────┐
│   User Application (any text field)     │
└────────────▲────────────────────────────┘
             │ commitString(text)
┌────────────┴────────────────────────────┐
│          fcitx5 Framework               │
│  ┌──────────────────────────────────┐   │
│  │  Voice Plugin (C++ .so)          │   │
│  │  - Hotkey: Shift+Space           │   │
│  │  - Delta → preedit (inline)      │   │
│  │  - Completed → commitString      │   │
│  └──────────┬───────────────────────┘   │
└─────────────┼───────────────────────────┘
              │ D-Bus IPC
┌─────────────┼───────────────────────────┐
│  Voice Daemon (Python systemd service)  │
│  - Audio capture (sounddevice, PCM16)   │
│  - WebSocket streaming to NIM Riva      │
│  - D-Bus signals: Delta, Completed      │
└─────────────┼───────────────────────────┘
              │ WebSocket (ws://)
┌─────────────┼───────────────────────────┐
│  NIM Riva ASR Server (GPU machine)      │
│  - via SSH port forwarding / Tailscale  │
│  - Model: parakeet-rnnt-1.1b            │
└─────────────────────────────────────────┘
```

## Prerequisites

### Local machine (where you type)

```bash
# Arch Linux
sudo pacman -S fcitx5 fcitx5-qt fcitx5-gtk cmake gcc pkgconf dbus python portaudio

# Ubuntu/Debian
sudo apt install fcitx5 cmake g++ pkg-config libdbus-1-dev python3 libportaudio2
```

Python 3.13+ with [uv](https://docs.astral.sh/uv/) package manager.

### Remote GPU machine

NVIDIA NIM Riva ASR server running and accessible via:
- SSH port forwarding: `ssh -L 9000:localhost:9000 gpu-server`
- Tailscale: direct IP access
- Any network path that exposes the WebSocket endpoint

## Installation

```bash
# Install Python dependencies
uv sync

# Build and install everything
./scripts/install.sh
```

The install script will:
1. Install Python dependencies
2. Build and install the C++ fcitx5 plugin (requires sudo)
3. Install systemd service and start the daemon
4. Restart fcitx5

### Configure server URL

Edit the systemd service to point to your NIM Riva server:

```bash
# Edit the service file
systemctl --user edit fcitx5-voice-daemon

# Override ExecStart with your server URL:
# [Service]
# ExecStart=
# ExecStart=%h/.local/bin/fcitx5-voice-daemon --url ws://your-gpu-server:9000 --language ja-JP
```

Or run the daemon manually:

```bash
uv run fcitx5-voice-daemon --url ws://localhost:9000 --language ja-JP --debug
```

### Uninstall

```bash
./scripts/uninstall.sh
```

## Usage

1. **Start voice input**: Press `Shift+Space` in any text field
2. **Speak**: Partial transcription appears inline (preedit) as you talk
3. **Real-time feedback**: Text updates continuously as the server processes audio
4. **Stop**: Press `Shift+Space` again to stop recording

### Tips

- Ensure the NIM Riva server is reachable before starting (e.g., SSH tunnel is up)
- Speak naturally; the streaming model handles continuous speech
- The daemon auto-commits audio buffers every ~1 second for processing

## Configuration

### Daemon CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `--url` | `ws://localhost:9000` | NIM Riva WebSocket URL |
| `--language` | `ja-JP` | Language code |
| `--model` | `parakeet-rnnt-1.1b-...` | ASR model name |
| `--commit-interval` | `10` | Commit every N chunks (N * 100ms) |
| `--debug` | off | Enable debug logging |

### systemd service

The default service file is at `~/.config/systemd/user/fcitx5-voice-daemon.service`.

```bash
# View logs
journalctl --user -u fcitx5-voice-daemon -f

# Restart after config changes
systemctl --user restart fcitx5-voice-daemon
```

## Development

### Project structure

```
fcitx5-voice/
├── daemon/              # Python voice daemon
│   ├── main.py          # Entry point + CLI args
│   ├── dbus_service.py  # D-Bus service + asyncio bridge
│   ├── recorder.py      # Streaming audio capture (sounddevice)
│   └── ws_client.py     # NIM Riva WebSocket client
├── plugin/              # C++ fcitx5 plugin
│   ├── voice_engine.*   # Main plugin (hotkey, preedit, commit)
│   ├── dbus_client.*    # D-Bus signal handling
│   └── *.conf           # fcitx5 configuration
├── dbus/                # D-Bus interface definition
├── systemd/             # Systemd service file
└── scripts/             # Install/uninstall scripts
```

### Testing daemon standalone

```bash
# Run in foreground with debug logging
uv run fcitx5-voice-daemon --url ws://localhost:9000 --debug

# In another terminal, trigger via D-Bus
gdbus call --session \
  --dest org.fcitx.Fcitx5.Voice \
  --object-path /org/fcitx/Fcitx5/Voice \
  --method org.fcitx.Fcitx5.Voice.StartRecording

# Monitor D-Bus signals
gdbus monitor --session --dest org.fcitx.Fcitx5.Voice
```

### Rebuilding C++ plugin

```bash
cd build && make -j$(nproc) && sudo make install
fcitx5 -r  # Restart fcitx5
```

## Troubleshooting

### WebSocket connection fails

```bash
# Test connectivity
python3 -c "import websockets, asyncio; asyncio.run(websockets.connect('ws://localhost:9000'))"

# Check SSH tunnel
ss -tlnp | grep 9000
```

### No audio input

```bash
# List audio devices
python3 -c "import sounddevice; print(sounddevice.query_devices())"

# Test recording
arecord -d 3 test.wav && aplay test.wav
```

### Plugin not loading

```bash
ls -lh /usr/lib/fcitx5/voice.so
qdbus org.fcitx.Fcitx5 /addon org.fcitx.Fcitx.AddonManager1.Addons | grep voice
```

### Daemon not running

```bash
systemctl --user status fcitx5-voice-daemon
journalctl --user -u fcitx5-voice-daemon -f
```

## D-Bus Interface

Service: `org.fcitx.Fcitx5.Voice`

| Type | Name | Args | Description |
|------|------|------|-------------|
| Method | StartRecording | - | Begin audio streaming |
| Method | StopRecording | - | Stop audio streaming |
| Method | GetStatus | -> string | "recording" or "idle" |
| Signal | TranscriptionDelta | text: string | Partial transcription (preedit) |
| Signal | TranscriptionComplete | text: string, segment_num: int | Final transcription (commit) |
| Signal | RecordingStarted | - | Recording began |
| Signal | RecordingStopped | - | Recording ended |
| Signal | Error | message: string | Error occurred |

## Dependencies

### Python
- `websockets` - WebSocket client for NIM Riva
- `sounddevice` - Audio capture (via PortAudio)
- `numpy` - Audio buffer handling
- `pydbus` - D-Bus Python bindings
- `PyGObject` - GLib main loop

### System
- `fcitx5` (>= 5.1.0) - Input method framework
- `libdbus-1` - D-Bus C library
- `cmake` (>= 3.22) - Build system
- GCC with C++20 support
- PortAudio - Audio I/O library

## License

Apache License 2.0
