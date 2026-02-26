# TODO

## Decisions needed

### Daemon architecture
- **Current**: Python daemon with GLib main loop (for D-Bus via pydbus) + asyncio in a separate thread (for WebSocket)
- **Concern**: GLib + asyncio thread bridge is functional but not elegant
- **Alternative**: Switch to fully asyncio-based daemon with `dbus-fast` (asyncio-native D-Bus library, actively maintained, used by Home Assistant). This would eliminate the threading bridge entirely
- **Trade-off**: `dbus-fast` is a new dependency and requires rewriting D-Bus integration; `pydbus` + GLib is proven and already working
- **Decision**: Defer until the current approach proves problematic

### NIM Riva server setup documentation
- How to set up the NIM Riva server on the GPU machine is not documented
- Should we include a docker-compose or deployment guide?
- At minimum, document the expected NIM Riva version and API compatibility

## Next actions

### High priority
- [x] Handle WebSocket reconnection - if the server goes down mid-session, the daemon should recover gracefully
- [x] WebSocket compression (`permessage-deflate`) - test if NIM Riva supports it, add `--compression` flag

### Medium priority
- [ ] Punctuation handling - some models output space-separated CJK; current `replace(" ", "")` is a hack
- [ ] Configuration file support - instead of only CLI args, support a config file (e.g., `~/.config/fcitx5-voice/config.toml`). Systemd service URL should be templated at that point.
- [ ] Make the hotkey configurable (currently hardcoded as Shift+Space in C++)
- [ ] Audio device selection - allow specifying which microphone to use

### Low priority
- [ ] Investigate if daemon is necessary at all - could the C++ plugin do WebSocket directly? (libwebsockets exists but adds complexity)
- [ ] Support multiple ASR backends (NIM Riva, OpenAI Whisper API, Google Speech-to-Text) via a backend interface
- [ ] Noise cancellation preprocessing before sending audio

## Known issues
- The `segment_num` parameter in `TranscriptionComplete` is always 0 in streaming mode. It's kept for backward compatibility with the C++ plugin but could be cleaned up.
- Japanese text cleaning (`replace(" ", "")`) is a blunt instrument - it works for Japanese but would break languages that use spaces. Need per-language handling.
