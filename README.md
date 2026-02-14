# fcitx5-voice

Voice input plugin for fcitx5 using OpenAI Whisper speech recognition.

## 特徴

### 良い点
- 🏠 **ローカルCPUで動作** - プライバシー保護、API費用なし、オフラインでも使える
- 🌍 **fcitx5統合** - Linuxデスクトップで日本語入力可能な全ての場所で動作
- 🔓 **オープンソース** - 自由にカスタマイズ可能

### 悪い点
- 🐌 **処理が遅い** - CPU推論のため、文字起こしに数秒かかる
- 📉 **精度が低い** - 特に専門用語や固有名詞に弱い

### 向いている用途
- プライバシーが重要なドキュメント作成
- オフライン環境での音声入力
- API費用を払いたくない個人利用

### 向いていない用途
- リアルタイム性が必要な用途（チャット、コーディングなど）
- 高精度が必要な専門文書作成
- 高速な音声入力が必要な場合

より高速・高精度な音声入力が必要な場合は、Google Cloud Speech-to-Text や OpenAI Whisper API などのクラウドサービスの利用を推奨します。

## Features

- 🎤 **Voice-to-text input** - Speak and have your words transcribed automatically
- ⌨️ **Easy hotkey** - Works in any application via Shift+Space
- 🔇 **Automatic silence detection** - Stops recording after ~1 second of silence
- 🧠 **Whisper small model** - Optimized for real-time performance
- 🔄 **Real-time processing indicator** - Shows recording and processing status independently
- 📦 **Simple installation** - Install to system with one script

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
│  │  - Registers hotkey (Shift+Space)│   │
│  │  - Calls D-Bus methods           │   │
│  │  - Shows processing indicator    │   │
│  │  - Injects text to InputContext  │   │
│  └──────────┬───────────────────────┘   │
└─────────────┼───────────────────────────┘
              │ D-Bus IPC
              │ org.fcitx.Fcitx5.Voice
┌─────────────┼───────────────────────────┐
│  Voice Daemon (Python systemd service)  │
│  - D-Bus service interface               │
│  - Whisper model (small size)            │
│  - Audio recording + transcription       │
│  - Emits ProcessingStarted signal        │
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
2. Build and install the C++ fcitx5 plugin to `/usr/lib/fcitx5/` (requires sudo)
3. Install systemd service and start the daemon
4. Restart fcitx5 to load the plugin

### Uninstall

```bash
./scripts/uninstall.sh
```

## Usage

### Basic Voice Input

1. **Start voice input**: Press `Shift+Space` in any text field
2. **Speak**: You'll see "🎤 録音中 (Shift+Space で停止)" notification
3. **Auto-complete**: After ~1 second of silence, transcription will start
4. **Processing**: You'll see "⏳ 処理中..." while Whisper processes your speech
5. **Manual stop**: Press `Shift+Space` again to stop recording immediately

**Note**: You can start a new recording while previous audio is still being processed in the background.

### Tips

- Speak clearly and at a normal pace
- Avoid background noise for better accuracy
- Maximum recording duration: 15 seconds per segment
- The first transcription may be slower (model loading)

## Configuration

### Model Size

Default model is `small` (~500MB memory, optimized for speed). To change:

Edit `daemon/transcriber.py`:
```python
MODEL_SIZE = "small"  # Options: tiny, base, small, medium, large-v3-turbo
```

**Trade-offs**:
- `tiny`: Fastest but very poor accuracy
- `base`: Fast but poor accuracy
- `small`: Good balance (default) ✓
- `medium`: Better accuracy but slower (~3-5 seconds per segment)
- `large-v3-turbo`: Best accuracy but very slow (~10+ seconds per segment)

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
ls -lh /usr/lib/fcitx5/voice.so
qdbus org.fcitx.Fcitx5 /addon org.fcitx.Fcitx.AddonManager1.Addons | grep -i voice
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

The Whisper model stays loaded in memory (~500MB for small model). This is normal and provides fast transcription. To reduce memory:
- Use a smaller model like `tiny` or `base` (edit `daemon/transcriber.py`)
- Restart daemon to unload model: `systemctl --user restart fcitx5-voice-daemon`

Note: The trade-off between memory usage and accuracy is significant. The `small` model is the recommended minimum for acceptable Japanese transcription quality.

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

### Current (small model)
- **Model loading**: ~2-3 seconds on first startup
- **Transcription latency**: ~1-2 seconds for 5-second audio (small model, CPU)
- **Memory usage**: ~500MB (small model loaded in RAM)
- **CPU usage**: Low when idle, high spike during transcription

### Optimization Tips
- Use `beam_size=1` for faster inference (already enabled)
- Enable VAD filtering to skip silent portions (already enabled)
- Use smaller model (tiny/base) for faster processing
- Consider GPU acceleration for 5-10x speedup (not implemented yet)

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

Apache License 2.0

See [LICENSE](LICENSE) file for details.

## Credits

- Based on [fcitx5-mozc](https://github.com/fcitx-contrib/fcitx5-mozc) for plugin architecture
- Uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for speech recognition
- Powered by [OpenAI Whisper](https://github.com/openai/whisper) models

## Known Limitations

### Speed
- **CPU inference is slow**: 1-2 seconds latency per 5-second audio segment
- **No GPU acceleration yet**: Would improve speed 5-10x but not implemented
- **Model loading time**: Takes 2-3 seconds on first use

### Accuracy
- **Weak on specialized terms**: Technical terms, proper nouns often mistranscribed
- **Sensitive to audio quality**: Background noise degrades accuracy significantly
- **No context awareness**: Each segment is transcribed independently

### Workarounds
- Speak clearly and pause between phrases
- Use in quiet environments
- Manually correct errors after insertion
- Consider using larger models for better accuracy (at cost of speed)

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
