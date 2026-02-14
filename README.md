# fcitx5-voice

Voice input plugin for fcitx5 using OpenAI Whisper speech recognition.

## Features

- 🎤 **Voice-to-text input** - Speak and have your words transcribed automatically
- ⌨️ **System-wide hotkey** - Works in any application via Ctrl+Alt+V
- 🔇 **Automatic silence detection** - Stops recording after ~1 second of silence
- 🧠 **Whisper medium model** - Good balance of accuracy and performance
- 🔄 **Real-time transcription** - Background processing doesn't block input
- 🏠 **User-local installation** - No sudo required

## Architecture

```
┌─────────────────────────────────────────┐
│   User Application (any text field)     │
└────────────▲────────────────────────────┘
             │ Text injection
             │
┌────────────┴────────────────────────────┐
│          fcitx5 Framework                │
│  ┌──────────────────────────────────┐   │
│  │  Voice Plugin (C++ .so)          │   │
│  │  - Registers hotkey (Ctrl+Alt+V) │   │
│  │  - Calls D-Bus methods           │   │
│  │  - Injects text to InputContext  │   │
│  └──────────┬───────────────────────┘   │
└─────────────┼───────────────────────────┘
              │ D-Bus IPC
              │ org.fcitx.Fcitx5.Voice
┌─────────────┼───────────────────────────┐
│  Voice Daemon (Python systemd service)  │
│  - D-Bus service interface               │
│  - Whisper model (medium size)           │
│  - Audio recording + transcription       │
│  - Emits TranscriptionComplete signal    │
└──────────────────────────────────────────┘
```

## Installation

### Prerequisites

Install system dependencies (Arch Linux):

```bash
sudo pacman -S fcitx5 fcitx5-qt fcitx5-gtk cmake gcc pkgconf dbus python
```

For other distributions, install equivalent packages:
- `fcitx5` - Input method framework
- `cmake` - Build system (>= 3.22)
- `gcc` - C++ compiler with C++20 support
- `pkgconf` - Package config
- `dbus` - Message bus system
- Python 3.13+ with `uv` package manager

### Build and Install

```bash
# Clone or navigate to the repository
cd /home/penguin/prog/fcitx5-voice

# Run the installation script
./scripts/install.sh
```

The script will:
1. Install Python dependencies (pydbus, PyGObject, faster-whisper, etc.)
2. Build and install the C++ fcitx5 plugin to `~/.local/lib/fcitx5/`
3. Install systemd service and start the daemon
4. Restart fcitx5 to load the plugin

### Uninstall

```bash
./scripts/uninstall.sh
```

## Usage

### Basic Voice Input

1. **Start voice input**: Press `Ctrl+Alt+V` in any text field
2. **Speak**: You'll see "🎤 Recording..." notification
3. **Auto-complete**: After ~1 second of silence, text will be transcribed and inserted
4. **Manual stop**: Press `Ctrl+Alt+V` again to stop recording immediately

### Tips

- Speak clearly and at a normal pace
- Avoid background noise for better accuracy
- Maximum recording duration: 15 seconds per segment
- The first transcription may be slower (model loading)

## Configuration

### Model Size

Default model is `medium` (~1.5GB memory). To change:

Edit `daemon/transcriber.py`:
```python
MODEL_SIZE = "small"  # Options: tiny, base, small, medium, large-v3-turbo
```

### Recording Parameters

Edit `daemon/recorder.py`:
```python
SILENCE_THRESHOLD = 0.01   # Lower = more sensitive to silence
SILENCE_DURATION = 1.0     # Seconds of silence before auto-stop
MAX_DURATION = 15.0        # Max recording length per segment
```

After changes, restart the daemon:
```bash
systemctl --user restart fcitx5-voice-daemon
```

## Troubleshooting

### Plugin Not Loading

Check if the plugin is installed and recognized:
```bash
ls -lh ~/.local/lib/fcitx5/voice.so
fcitx5-diagnose | grep -i voice
```

### Daemon Not Running

Check daemon status:
```bash
systemctl --user status fcitx5-voice-daemon
```

View daemon logs:
```bash
journalctl --user -u fcitx5-voice-daemon -f
```

Restart daemon:
```bash
systemctl --user restart fcitx5-voice-daemon
```

### No Microphone Input

Test microphone:
```bash
arecord -l  # List audio devices
arecord -d 5 test.wav  # Record 5 seconds
aplay test.wav  # Play back
```

### D-Bus Connection Issues

Test D-Bus connection:
```bash
gdbus call --session \
  --dest org.fcitx.Fcitx5.Voice \
  --object-path /org/fcitx/Fcitx5/Voice \
  --method org.fcitx.Fcitx5.Voice.GetStatus
```

Monitor D-Bus signals:
```bash
gdbus monitor --session --dest org.fcitx.Fcitx5.Voice
```

### Hotkey Not Working

1. Check if fcitx5 is the active input method framework
2. Check for conflicting keybindings in system settings
3. Check fcitx5 logs for errors:
   ```bash
   journalctl --user -u fcitx5 -f
   ```

### High Memory Usage

The Whisper model stays loaded in memory (~1.5GB for medium model). This is normal and provides fast transcription. To reduce memory:
- Use a smaller model (edit `daemon/transcriber.py`)
- Restart daemon periodically: `systemctl --user restart fcitx5-voice-daemon`

## Development

### Project Structure

```
fcitx5-voice/
├── daemon/              # Python voice daemon
│   ├── __init__.py
│   ├── main.py          # Entry point
│   ├── dbus_service.py  # D-Bus interface
│   ├── recorder.py      # Audio recording
│   └── transcriber.py   # Whisper transcription
├── plugin/              # C++ fcitx5 plugin
│   ├── CMakeLists.txt
│   ├── voice_engine.h/cpp       # Main plugin
│   ├── dbus_client.h/cpp        # D-Bus communication
│   ├── voice_engine_factory.cpp # Plugin registration
│   └── *.conf           # Configuration files
├── dbus/                # D-Bus interface definition
│   └── org.fcitx.Fcitx5.Voice.xml
├── systemd/             # Systemd service file
│   └── fcitx5-voice-daemon.service
└── scripts/             # Installation scripts
    ├── install.sh
    └── uninstall.sh
```

### Testing Daemon Standalone

Run daemon in foreground with debug logging:
```bash
uv run python -m daemon.main --debug
```

In another terminal, trigger recording via D-Bus:
```bash
gdbus call --session \
  --dest org.fcitx.Fcitx5.Voice \
  --object-path /org/fcitx/Fcitx5/Voice \
  --method org.fcitx.Fcitx5.Voice.StartRecording
```

### Rebuilding Plugin

After modifying C++ code:
```bash
cd build
make -j$(nproc)
make install
fcitx5 -r  # Restart fcitx5
```

### Standalone Voice Input (without fcitx5)

The original standalone voice input tool is preserved:
```bash
uv run python standalone.py
```

## Performance

- **Model loading**: ~3-5 seconds on first startup
- **Transcription latency**: ~1-3 seconds for 5-second audio (medium model, CPU)
- **Memory usage**: ~1.5GB (medium model loaded in RAM)
- **CPU usage**: Low when idle, high spike during transcription

## Dependencies

### Python
- `faster-whisper` - Whisper inference engine
- `sounddevice` - Audio recording
- `scipy` - Audio file I/O
- `numpy` - Array processing
- `pydbus` - D-Bus Python bindings
- `PyGObject` - GLib/GObject bindings

### System
- `fcitx5` (>= 5.1.0) - Input method framework
- `libdbus-1` - D-Bus C library
- `cmake` (>= 3.22) - Build system
- GCC with C++20 support

## License

[Add your license here]

## Credits

- Based on [fcitx5-mozc](https://github.com/fcitx-contrib/fcitx5-mozc) for plugin architecture
- Uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for speech recognition
- Powered by [OpenAI Whisper](https://github.com/openai/whisper) models

## Contributing

Contributions are welcome! Please feel free to submit issues or pull requests.

### TODO

- [ ] Add configuration UI for fcitx5-config
- [ ] Support multiple languages with auto-detection
- [ ] GPU acceleration support (CUDA/ROCm)
- [ ] Noise cancellation preprocessing
- [ ] Punctuation auto-insertion
- [ ] Voice command mode (editing commands)
- [ ] Reduce model loading time (lazy loading)
