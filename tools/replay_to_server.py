#!/usr/bin/env python3
"""WAV file replay tool for Riva ASR server testing.

Reads one or more WAV files and streams their audio to a NIM Riva ASR server
(real or mock) using the same WebSocket protocol as the fcitx5-voice daemon.
Transcription results are displayed in real-time with color and timestamps.

Usage:
    python replay_to_server.py speech.wav
    python replay_to_server.py file1.wav file2.wav --url ws://localhost:9000
    python replay_to_server.py speech.wav --commit-interval 10
    python replay_to_server.py speech.wav --chunk-delay 0 --no-color
"""

import argparse
import asyncio
import json
import logging
import struct
import sys
import time
import wave
from pathlib import Path

# ---------------------------------------------------------------------------
# Import daemon.ws_client from the project root
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon.ws_client import RivaWSClient, DEFAULT_URL, DEFAULT_MODEL, DEFAULT_LANGUAGE  # noqa: E402

# ---------------------------------------------------------------------------
# Audio constants (mirror daemon/recorder.py)
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000       # Hz
CHUNK_SIZE = 1600         # samples per 100 ms chunk
CHUNK_BYTES = CHUNK_SIZE * 2  # 2 bytes per int16 sample = 3200 bytes

# Silence detection constants (mirror daemon/dbus_service.py _send_audio_loop)
CALIBRATION_CHUNKS = 10       # 1 s calibration period
NOISE_MULTIPLIER = 3.0        # threshold = noise_floor * multiplier
MIN_THRESHOLD = 300           # absolute minimum threshold
SILENCE_COMMIT_CHUNKS = 2     # 200 ms silence -> commit
FLUSH_INTERVAL_CHUNKS = 10    # flush every 1 s during silence
MAX_FLUSHES = 3               # up to 3 flush commits

# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_BOLD = "\033[1m"
_CYAN = "\033[96m"
_YELLOW = "\033[93m"
_GREEN = "\033[92m"
_BLUE = "\033[94m"
_RED = "\033[91m"
_DIM = "\033[2m"


def _color(text: str, code: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"{code}{text}{_RESET}"


# ---------------------------------------------------------------------------
# State shared between send and recv tasks
# ---------------------------------------------------------------------------

class ReplayState:
    """Mutable state shared between the send and recv coroutines."""

    def __init__(self, use_color: bool, start_time: float):
        self.use_color = use_color
        self.start_time = start_time

        # Counters
        self.chunks_sent: int = 0
        self.commits_sent: int = 0
        self.completions_received: int = 0
        self.completed_texts: list[str] = []

        # Total audio bytes sent (for duration calculation)
        self.audio_bytes_sent: int = 0

        # Capture: track deltas per commit for --capture output
        self._current_deltas: list[str] = []
        self.captured_scenarios: list[list[str]] = []

        # Flag set by sender when all audio has been sent
        self.send_done: asyncio.Event = asyncio.Event()

    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def log(self, message: str, color_code: str = "") -> None:
        ts = f"[{self.elapsed():5.2f}s]"
        colored_ts = _color(ts, _DIM, self.use_color)
        colored_msg = _color(message, color_code, self.use_color) if color_code else message
        print(f"{colored_ts} {colored_msg}", flush=True)

    def log_info(self, message: str) -> None:
        self.log(message, _CYAN)

    def log_commit(self, message: str) -> None:
        self.log(message, _BLUE)

    def log_delta(self, text: str) -> None:
        label = _color("DELTA:", _YELLOW, self.use_color)
        print(f"{_color(f'[{self.elapsed():5.2f}s]', _DIM, self.use_color)} {label} {text}", flush=True)

    def log_completed(self, text: str) -> None:
        label = _color("COMPLETED:", _BOLD + _GREEN, self.use_color)
        print(f"{_color(f'[{self.elapsed():5.2f}s]', _DIM, self.use_color)} {label} {_color(text, _GREEN, self.use_color)}", flush=True)

    def log_error(self, message: str) -> None:
        label = _color("ERROR:", _RED, self.use_color)
        print(f"{_color(f'[{self.elapsed():5.2f}s]', _DIM, self.use_color)} {label} {_color(message, _RED, self.use_color)}", flush=True)

    def log_chunk(self, chunk_num: int) -> None:
        msg = f"Sending audio: 100ms chunk #{chunk_num}"
        self.log(msg, _DIM)


# ---------------------------------------------------------------------------
# WAV reading
# ---------------------------------------------------------------------------

def open_wav(path: str) -> tuple[wave.Wave_read, float]:
    """Open and validate a WAV file. Returns (wave_reader, duration_seconds).

    Validates: 16-bit PCM, mono, 16000 Hz.
    Prints a warning (but continues) for non-standard sample rates.
    Raises SystemExit for unrecoverable format errors.
    """
    try:
        wf = wave.open(path, "rb")
    except FileNotFoundError:
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        sys.exit(1)
    except wave.Error as e:
        print(f"ERROR: Cannot open WAV file {path}: {e}", file=sys.stderr)
        sys.exit(1)

    n_channels = wf.getnchannels()
    sample_width = wf.getsampwidth()
    frame_rate = wf.getframerate()
    n_frames = wf.getnframes()

    errors = []
    warnings = []

    if sample_width != 2:
        errors.append(
            f"must be 16-bit PCM (sample width=2 bytes), got {sample_width * 8}-bit"
        )
    if n_channels != 1:
        errors.append(f"must be mono (1 channel), got {n_channels} channels")
    if frame_rate != SAMPLE_RATE:
        warnings.append(
            f"sample rate is {frame_rate} Hz, expected {SAMPLE_RATE} Hz. "
            "Chunk timing will be inaccurate."
        )

    if errors:
        print(f"ERROR: Invalid WAV format in {path}:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    for w in warnings:
        print(f"WARNING: {path}: {w}", file=sys.stderr)

    duration = n_frames / frame_rate
    return wf, duration


def read_chunks(wf: wave.Wave_read) -> list[bytes]:
    """Read all frames from an open WAV file as a list of raw PCM16 chunks.

    Each chunk is CHUNK_BYTES bytes (3200 bytes = 1600 samples = 100 ms at 16 kHz).
    The last chunk is zero-padded if the file length is not an exact multiple.
    """
    chunks = []
    while True:
        raw = wf.readframes(CHUNK_SIZE)
        if not raw:
            break
        if len(raw) < CHUNK_BYTES:
            # Zero-pad the final partial chunk
            raw = raw + b"\x00" * (CHUNK_BYTES - len(raw))
        chunks.append(raw)
    wf.close()
    return chunks


# ---------------------------------------------------------------------------
# RMS helper for silence detection
# ---------------------------------------------------------------------------

def rms_of_chunk(chunk: bytes) -> float:
    """Compute the RMS energy of a PCM16 chunk."""
    n = len(chunk) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", chunk)
    return (sum(s * s for s in samples) / n) ** 0.5


# ---------------------------------------------------------------------------
# Send task
# ---------------------------------------------------------------------------

async def send_audio_fixed_interval(
    client: RivaWSClient,
    chunks: list[bytes],
    commit_interval: int,
    chunk_delay: float,
    state: ReplayState,
) -> None:
    """Send audio chunks with fixed commit interval (simple mode)."""
    chunks_since_commit = 0

    for i, chunk in enumerate(chunks):
        chunk_num = i + 1
        state.log_chunk(chunk_num)

        await client.send_audio(chunk)
        state.chunks_sent += 1
        state.audio_bytes_sent += len(chunk)
        chunks_since_commit += 1

        if chunks_since_commit >= commit_interval:
            audio_sent_s = chunks_since_commit * 0.1  # 100ms per chunk
            state.commits_sent += 1
            state.log_commit(
                f"Commit #{state.commits_sent} (sent {audio_sent_s:.1f}s of audio)"
            )
            await client.commit()
            chunks_since_commit = 0

        if chunk_delay > 0:
            await asyncio.sleep(chunk_delay)

    # Final commit for any remaining audio
    if chunks_since_commit > 0:
        audio_sent_s = chunks_since_commit * 0.1
        state.commits_sent += 1
        state.log_commit(
            f"Commit #{state.commits_sent} (sent {audio_sent_s:.1f}s of audio, final)"
        )
        await client.commit()

    state.send_done.set()


async def send_audio_auto_silence(
    client: RivaWSClient,
    chunks: list[bytes],
    chunk_delay: float,
    state: ReplayState,
) -> None:
    """Send audio chunks with auto silence-detection commit logic.

    Mirrors the logic in daemon/dbus_service.py _send_audio_loop().
    """
    calibration_rms_values: list[float] = []
    silence_threshold = 0.0

    has_speech = False
    silence_chunks = 0
    chunks_since_commit = 0
    flush_count = 0
    silence_after_commit = 0

    for i, chunk in enumerate(chunks):
        chunk_num = i + 1
        state.log_chunk(chunk_num)

        rms = rms_of_chunk(chunk)
        await client.send_audio(chunk)
        state.chunks_sent += 1
        state.audio_bytes_sent += len(chunk)
        chunks_since_commit += 1

        # Calibration phase
        if len(calibration_rms_values) < CALIBRATION_CHUNKS:
            calibration_rms_values.append(rms)
            if len(calibration_rms_values) == CALIBRATION_CHUNKS:
                noise_floor = sum(calibration_rms_values) / len(calibration_rms_values)
                silence_threshold = max(noise_floor * NOISE_MULTIPLIER, MIN_THRESHOLD)
                state.log_info(
                    f"Noise calibration: floor={noise_floor:.0f}, "
                    f"threshold={silence_threshold:.0f}"
                )
            if chunk_delay > 0:
                await asyncio.sleep(chunk_delay)
            continue

        is_speech = rms >= silence_threshold
        if is_speech:
            has_speech = True
            silence_chunks = 0
            flush_count = 0
            silence_after_commit = 0
        else:
            silence_chunks += 1
            if flush_count < MAX_FLUSHES:
                silence_after_commit += 1

        # Commit after speech followed by silence
        if has_speech and silence_chunks >= SILENCE_COMMIT_CHUNKS:
            audio_sent_s = chunks_since_commit * 0.1
            state.commits_sent += 1
            state.log_commit(
                f"Commit #{state.commits_sent} (sent {audio_sent_s:.1f}s of audio)"
            )
            await client.commit()
            has_speech = False
            chunks_since_commit = 0
            flush_count = 0
            silence_after_commit = 0
            silence_chunks = 0

        # Periodic flush commits during silence
        if (
            flush_count < MAX_FLUSHES
            and silence_after_commit > 0
            and silence_after_commit % FLUSH_INTERVAL_CHUNKS == 0
        ):
            audio_sent_s = chunks_since_commit * 0.1
            flush_count += 1
            state.commits_sent += 1
            state.log_commit(
                f"Commit #{state.commits_sent} (flush {flush_count}/{MAX_FLUSHES}, "
                f"sent {audio_sent_s:.1f}s of audio)"
            )
            await client.commit()
            chunks_since_commit = 0

        if chunk_delay > 0:
            await asyncio.sleep(chunk_delay)

    # Final commit for any remaining audio
    if chunks_since_commit > 0:
        audio_sent_s = chunks_since_commit * 0.1
        state.commits_sent += 1
        state.log_commit(
            f"Commit #{state.commits_sent} (sent {audio_sent_s:.1f}s of audio, final)"
        )
        await client.commit()

    state.send_done.set()


# ---------------------------------------------------------------------------
# Recv task (wraps client.recv_loop with callback wiring)
# ---------------------------------------------------------------------------

async def recv_events(client: RivaWSClient, state: ReplayState) -> None:
    """Receive transcription events; forwards to state callbacks."""
    await client.recv_loop()


# ---------------------------------------------------------------------------
# Main replay coroutine for a single WAV file
# ---------------------------------------------------------------------------

async def replay_file(
    wav_path: str,
    url: str,
    model: str,
    language: str,
    compression: str | None,
    commit_interval: int,
    chunk_delay: float,
    state: ReplayState,
) -> None:
    """Connect to server, stream WAV audio, display results."""

    # Wire up callbacks
    def on_delta(text: str) -> None:
        state._current_deltas.append(text)
        state.log_delta(text)

    def on_completed(text: str) -> None:
        state.completions_received += 1
        state.completed_texts.append(text)
        # Save scenario: deltas + final completed text
        state.captured_scenarios.append(state._current_deltas + [text])
        state._current_deltas = []
        state.log_completed(text)

    def on_error(message: str) -> None:
        state.log_error(message)

    client = RivaWSClient(
        url=url,
        model=model,
        language=language,
        compression=compression,
        on_delta=on_delta,
        on_completed=on_completed,
        on_error=on_error,
    )

    state.log_info(f"Connecting to {url}")

    try:
        await client.connect()
    except Exception as e:
        state.log_error(f"Failed to connect: {e}")
        raise

    state.log_info(f"Session configured (model={model}, language={language})")

    # Load WAV chunks
    wf, duration = open_wav(wav_path)
    chunks = read_chunks(wf)
    total_audio_s = len(chunks) * 0.1  # each chunk is 100ms
    state.log_info(
        f"Loaded {wav_path}: {duration:.2f}s audio, {len(chunks)} chunks"
    )

    # Build send and recv tasks
    if commit_interval > 0:
        send_coro = send_audio_fixed_interval(
            client, chunks, commit_interval, chunk_delay, state
        )
    else:
        send_coro = send_audio_auto_silence(
            client, chunks, chunk_delay, state
        )

    send_task = asyncio.create_task(send_coro)
    recv_task = asyncio.create_task(recv_events(client, state))

    try:
        # Wait for send to complete first
        done, pending = await asyncio.wait(
            [send_task, recv_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Check for exceptions from completed tasks
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc

        # send_task done: wait up to 3s for remaining server responses
        if send_task in done and recv_task in pending:
            state.log_info("All audio sent; waiting up to 3s for server responses...")
            try:
                await asyncio.wait_for(recv_task, timeout=3.0)
            except asyncio.TimeoutError:
                state.log_info("Timeout waiting for final server responses")
            except Exception:
                pass
            finally:
                if not recv_task.done():
                    recv_task.cancel()
                    try:
                        await recv_task
                    except (asyncio.CancelledError, Exception):
                        pass

        # recv_task ended unexpectedly while we were still sending
        elif recv_task in done and send_task in pending:
            state.log_info("Server closed connection")
            send_task.cancel()
            try:
                await send_task
            except (asyncio.CancelledError, Exception):
                pass

    finally:
        await client.close()

    return total_audio_s


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    """Top-level async entry point. Returns exit code."""
    use_color = not args.no_color
    start_time = time.monotonic()
    state = ReplayState(use_color=use_color, start_time=start_time)

    compression: str | None = "deflate" if args.compression else None

    total_audio_s = 0.0

    try:
        for wav_path in args.wav_files:
            state.log_info(f"--- Replaying: {wav_path} ---")
            file_audio_s = await replay_file(
                wav_path=wav_path,
                url=args.url,
                model=args.model,
                language=args.language,
                compression=compression,
                commit_interval=args.commit_interval,
                chunk_delay=args.chunk_delay,
                state=state,
            )
            total_audio_s += file_audio_s

    except KeyboardInterrupt:
        state.log_info("Interrupted by user (Ctrl+C)")
    except Exception as e:
        state.log_error(f"Fatal error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1

    # Final summary
    elapsed = state.elapsed()
    print("", flush=True)
    summary_header = _color("=== Summary ===", _BOLD, use_color)
    print(summary_header, flush=True)
    print(
        f"  Total audio duration : {total_audio_s:.2f}s "
        f"({state.chunks_sent} chunks sent)",
        flush=True,
    )
    print(f"  Elapsed wall time    : {elapsed:.2f}s", flush=True)
    print(f"  Commits sent         : {state.commits_sent}", flush=True)
    print(f"  Completions received : {state.completions_received}", flush=True)

    if state.completed_texts:
        print("  Completed texts:", flush=True)
        for i, text in enumerate(state.completed_texts, 1):
            print(f"    [{i}] {text}", flush=True)
    else:
        print("  Completed texts      : (none)", flush=True)

    # --capture: write captured scenarios to JSON
    if args.capture and state.captured_scenarios:
        capture_path = Path(args.capture)
        with open(capture_path, "w", encoding="utf-8") as f:
            json.dump(state.captured_scenarios, f, ensure_ascii=False, indent=2)
        print(
            f"  Captured {len(state.captured_scenarios)} scenario(s) "
            f"to {capture_path}",
            flush=True,
        )

    # --expect: compare completed texts against expected values
    if args.expect:
        print("", flush=True)
        expected = args.expect
        actual = state.completed_texts
        all_pass = True

        header = _color("=== Assertions ===", _BOLD, use_color)
        print(header, flush=True)

        for i, exp in enumerate(expected):
            if i < len(actual):
                if actual[i] == exp:
                    status = _color("PASS", _GREEN, use_color)
                    print(f"  {status} [{i+1}] '{exp}'", flush=True)
                else:
                    status = _color("FAIL", _RED, use_color)
                    print(f"  {status} [{i+1}] expected '{exp}', got '{actual[i]}'", flush=True)
                    all_pass = False
            else:
                status = _color("FAIL", _RED, use_color)
                print(f"  {status} [{i+1}] expected '{exp}', but no completion received", flush=True)
                all_pass = False

        if len(actual) > len(expected):
            extras = actual[len(expected):]
            for i, text in enumerate(extras):
                idx = len(expected) + i + 1
                status = _color("EXTRA", _YELLOW, use_color)
                print(f"  {status} [{idx}] unexpected completion: '{text}'", flush=True)

        if all_pass and len(actual) == len(expected):
            print(_color("All assertions passed.", _GREEN, use_color), flush=True)
        else:
            return 1

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay WAV file(s) to a NIM Riva ASR server and display "
            "transcription results. Uses the same WebSocket protocol as "
            "the fcitx5-voice daemon."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "wav_files",
        nargs="+",
        metavar="WAV",
        help="WAV file(s) to replay (16-bit PCM, mono, 16000 Hz).",
    )
    parser.add_argument(
        "--url",
        default="ws://localhost:9100",
        help="WebSocket URL of the Riva ASR server.",
    )
    parser.add_argument(
        "--language",
        default=DEFAULT_LANGUAGE,
        help="Language code for transcription.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="ASR model name.",
    )
    parser.add_argument(
        "--commit-interval",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Commit every N chunks (N * 100ms). "
            "0 = auto mode with silence detection."
        ),
    )
    parser.add_argument(
        "--chunk-delay",
        type=float,
        default=0.1,
        metavar="SECONDS",
        help=(
            "Delay between sending chunks in seconds. "
            "0.1 = real-time (100ms). 0 = as fast as possible."
        ),
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="Disable ANSI color output.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable verbose/debug logging.",
    )
    parser.add_argument(
        "--compression",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable/disable WebSocket per-message deflate compression.",
    )
    parser.add_argument(
        "--expect",
        action="append",
        metavar="TEXT",
        help=(
            "Expected completed text (can be repeated). "
            "Exits with code 1 if actual completions don't match. "
            "Example: --expect 'これはテストです' --expect '音声認識のテスト中'"
        ),
    )
    parser.add_argument(
        "--capture",
        metavar="FILE",
        help=(
            "Save captured delta/completed sequences to a JSON file. "
            "Output is compatible with mock_riva_server.py --scenario."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        exit_code = asyncio.run(run(args))
    except KeyboardInterrupt:
        exit_code = 130  # Standard exit code for Ctrl+C

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
