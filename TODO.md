# TODO

## Decisions needed

### Repository structure
- **Current state**: GPU streaming ASR is on a feature branch (`claude/gpu-realtime-transcription-HB9TC`), local Whisper version is on `main`
- **Options**:
  1. **Merge to main** - Replace local Whisper entirely with GPU streaming. Simple, but loses the offline/local capability
  2. **Separate branches** - Keep `main` as local Whisper, streaming on a long-lived branch. Gets messy over time
  3. **Separate repos** - Fork into `fcitx5-voice` (local) and `fcitx5-voice-gpu` (streaming). Clean separation but maintenance burden
  4. **Feature flags / config** - Single codebase that supports both backends via daemon CLI args. Most flexible but more complex
- **Recommendation**: Option 4 (feature flags) if both modes are desired long-term, otherwise option 1 (merge) since GPU streaming is strictly better when a GPU server is available

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
- [ ] Handle WebSocket reconnection - if the server goes down mid-session, the daemon should recover gracefully

### Medium priority
- [ ] WebSocket compression (`permessage-deflate`) - test if NIM Riva supports it, add `--compression` flag
- [ ] Configuration file support - instead of only CLI args, support a config file (e.g., `~/.config/fcitx5-voice/config.toml`). Systemd service URL should be templated at that point.
- [ ] Make the hotkey configurable (currently hardcoded as Shift+Space in C++)
- [ ] Audio device selection - allow specifying which microphone to use

### Low priority
- [ ] Investigate if daemon is necessary at all - could the C++ plugin do WebSocket directly? (libwebsockets exists but adds complexity)
- [ ] Support multiple ASR backends (NIM Riva, OpenAI Whisper API, Google Speech-to-Text) via a backend interface
- [ ] Noise cancellation preprocessing before sending audio
- [ ] Visual indicator in system tray (not just fcitx5 panel) for recording state
- [ ] Auto-start SSH tunnel / Tailscale connection when daemon starts
- [ ] Punctuation handling - some models output space-separated CJK; current `replace(" ", "")` is a hack

## Known issues
- The `segment_num` parameter in `TranscriptionComplete` is always 0 in streaming mode. It's kept for backward compatibility with the C++ plugin but could be cleaned up.
- Japanese text cleaning (`replace(" ", "")`) is a blunt instrument - it works for Japanese but would break languages that use spaces. Need per-language handling.
