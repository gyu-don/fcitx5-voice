#!/usr/bin/env python3
"""Pipeline integration test: verifies what the fcitx5 plugin would see.

Exercises the daemon's streaming pipeline (AudioSource → RivaWSClient →
delta/completed callbacks) without D-Bus. Records all events with
timestamps to verify:

  1. Preedit latency:  How fast delta (preedit) arrives after a commit
  2. Double commit:    No duplicate TranscriptionComplete for the same text
  3. Mid-stop:         Clean shutdown when recording stops mid-stream
  4. Signal ordering:  Deltas arrive before their completion

This uses the same WavReplaySource and RivaWSClient as the daemon, so
results reflect actual daemon behavior minus D-Bus transport latency.

Usage:
    python tools/test_pipeline.py                   # Run all scenarios
    python tools/test_pipeline.py --scenario normal  # Run one scenario
    python tools/test_pipeline.py --verbose          # Show all events
    python tools/test_pipeline.py --port 9198        # Custom port

Exit codes:
    0 = all tests passed
    1 = test failure or error
"""

import argparse
import asyncio
import os
import signal
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT))
from daemon.ws_client import RivaWSClient  # noqa: E402
from daemon.recorder import WavReplaySource, CHUNK_BYTES  # noqa: E402

DEFAULT_PORT = 9198

# ANSI colors
_RESET = "\033[0m"
_BOLD = "\033[1m"
_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_DIM = "\033[2m"


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------


@dataclass
class Event:
    """A recorded event from the pipeline (what fcitx5 plugin would see)."""

    time_ms: float  # Milliseconds since pipeline start
    type: str  # "delta", "completed", "error", "commit", "stop"
    text: str = ""

    def __str__(self) -> str:
        if self.text:
            return f"[{self.time_ms:7.1f}ms] {self.type}: {self.text}"
        return f"[{self.time_ms:7.1f}ms] {self.type}"


@dataclass
class PipelineResult:
    """Aggregated result from a single pipeline test run."""

    events: list[Event] = field(default_factory=list)
    start_time: float = 0.0
    error: str | None = None

    @property
    def deltas(self) -> list[Event]:
        return [e for e in self.events if e.type == "delta"]

    @property
    def completions(self) -> list[Event]:
        return [e for e in self.events if e.type == "completed"]

    @property
    def commits(self) -> list[Event]:
        return [e for e in self.events if e.type == "commit"]

    def completion_texts(self) -> list[str]:
        return [e.text for e in self.completions]


# ---------------------------------------------------------------------------
# Pipeline runner — mirrors dbus_service.py._stream() + _send_audio_loop()
# ---------------------------------------------------------------------------


async def run_pipeline(
    wav_path: str,
    ws_url: str,
    stop_after_ms: float | None = None,
    stop_after_chunks: int | None = None,
    realtime: bool = False,
    model: str = "test-model",
    language: str = "ja-JP",
) -> PipelineResult:
    """Run the streaming pipeline and record all events.

    Uses the same WavReplaySource and RivaWSClient as the daemon.
    The send loop logic mirrors dbus_service.py._send_audio_loop().

    Args:
        wav_path:           Path to WAV file to replay.
        ws_url:             WebSocket URL of the (mock) server.
        stop_after_ms:      Simulate StopRecording after this many ms.
        stop_after_chunks:  Simulate StopRecording after sending N chunks.
        realtime:           Use real-time pacing for WAV replay.
        model:              ASR model name.
        language:           Language code.

    Returns:
        PipelineResult with all recorded events.
    """
    import threading

    result = PipelineResult()
    result.start_time = time.monotonic()
    stop_event = threading.Event()

    def elapsed_ms() -> float:
        return (time.monotonic() - result.start_time) * 1000

    def record(event_type: str, text: str = "") -> None:
        result.events.append(Event(
            time_ms=elapsed_ms(),
            type=event_type,
            text=text,
        ))

    client = RivaWSClient(
        url=ws_url,
        model=model,
        language=language,
        compression="deflate",
        on_delta=lambda text: record("delta", text),
        on_completed=lambda text: record("completed", text),
        on_error=lambda msg: record("error", msg),
    )

    source = WavReplaySource(wav_path, realtime=realtime)
    source.start()

    try:
        await client.connect()
        record("connected")
        source.drain()

        # Schedule mid-recording stop if requested (time-based)
        if stop_after_ms is not None:
            async def _stop_later():
                await asyncio.sleep(stop_after_ms / 1000)
                stop_event.set()
                record("stop")

            stop_task = asyncio.create_task(_stop_later())
        else:
            stop_task = None

        send_task = asyncio.create_task(
            _send_audio_loop(
                client, source, stop_event, record,
                stop_after_chunks=stop_after_chunks,
            )
        )
        recv_task = asyncio.create_task(client.recv_loop())

        done, pending = await asyncio.wait(
            [send_task, recv_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done:
            exc = task.exception()
            if exc:
                raise exc

        # send done → wait briefly for remaining server responses
        if send_task in done and recv_task in pending:
            record("send_done")
            try:
                await asyncio.wait_for(recv_task, timeout=3)
            except (asyncio.TimeoutError, Exception):
                pass
            finally:
                if not recv_task.done():
                    recv_task.cancel()
                    try:
                        await recv_task
                    except asyncio.CancelledError:
                        pass

        # Clean up any remaining tasks
        for task in pending:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if stop_task and not stop_task.done():
            stop_task.cancel()
            try:
                await stop_task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        result.error = str(e)
    finally:
        await client.close()
        source.stop()

    return result


async def _send_audio_loop(
    client, source, stop_event, record, stop_after_chunks=None
):
    """Send audio with silence-based commits (mirrors daemon logic).

    This replicates dbus_service.py VoiceDaemonService._send_audio_loop()
    so we test the same commit timing the daemon uses.

    Args:
        stop_after_chunks: If set, trigger stop_event after sending this
                           many chunks (for deterministic mid-stop testing).
    """
    CALIBRATION_CHUNKS = 10
    NOISE_MULTIPLIER = 3.0
    MIN_THRESHOLD = 300
    SILENCE_COMMIT_CHUNKS = 2
    FLUSH_INTERVAL_CHUNKS = 10
    MAX_FLUSHES = 3

    loop = asyncio.get_event_loop()
    has_speech = False
    silence_chunks = 0
    chunks_since_commit = 0
    flush_count = 0
    silence_after_commit = 0
    total_chunks_sent = 0

    calibration_rms_values: list[float] = []
    silence_threshold = 0.0

    while not stop_event.is_set():
        chunk = await loop.run_in_executor(
            None, lambda: source.get_chunk(timeout=0.2)
        )
        if not chunk:
            if source.exhausted:
                break
            continue

        samples = struct.unpack(f"<{len(chunk) // 2}h", chunk)
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5

        await client.send_audio(chunk)
        chunks_since_commit += 1
        total_chunks_sent += 1

        # Chunk-based stop trigger (deterministic mid-stop)
        if stop_after_chunks is not None and total_chunks_sent >= stop_after_chunks:
            stop_event.set()
            record("stop", f"after {total_chunks_sent} chunks")

        # Calibration phase
        if len(calibration_rms_values) < CALIBRATION_CHUNKS:
            calibration_rms_values.append(rms)
            if len(calibration_rms_values) == CALIBRATION_CHUNKS:
                noise_floor = (
                    sum(calibration_rms_values) / len(calibration_rms_values)
                )
                silence_threshold = max(
                    noise_floor * NOISE_MULTIPLIER, MIN_THRESHOLD
                )
                record("calibrated", f"threshold={silence_threshold:.0f}")
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

        # Commit after speech + silence
        if has_speech and silence_chunks >= SILENCE_COMMIT_CHUNKS:
            await client.commit()
            record("commit", f"chunks={chunks_since_commit}")
            has_speech = False
            chunks_since_commit = 0
            flush_count = 0
            silence_after_commit = 0

        # Flush commits during silence
        if (
            flush_count < MAX_FLUSHES
            and silence_after_commit > 0
            and silence_after_commit % FLUSH_INTERVAL_CHUNKS == 0
        ):
            await client.commit()
            flush_count += 1
            record("flush_commit", f"flush={flush_count}")
            chunks_since_commit = 0

    # Final commit
    if chunks_since_commit > 0:
        await client.commit()
        record("final_commit", f"chunks={chunks_since_commit}")


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------


class TestResult:
    """Result of a single test assertion."""

    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail

    def __str__(self) -> str:
        status = f"{_GREEN}PASS{_RESET}" if self.passed else f"{_RED}FAIL{_RESET}"
        s = f"  {status} {self.name}"
        if self.detail:
            s += f" — {self.detail}"
        return s


async def test_normal_flow(
    wav_path: str, ws_url: str, verbose: bool
) -> list[TestResult]:
    """Test normal recording flow: full WAV → verify events.

    Checks:
      - All expected completion texts received
      - No duplicate completions
      - Deltas precede their completions
      - Measures commit → first delta latency
    """
    results = []

    r = await run_pipeline(wav_path, ws_url)

    if verbose:
        print(f"\n  {_DIM}--- Event log ---{_RESET}")
        for e in r.events:
            print(f"  {_DIM}{e}{_RESET}")
        print()

    if r.error:
        results.append(TestResult("No errors", False, r.error))
        return results

    # Check completions received
    completions = r.completion_texts()
    results.append(TestResult(
        "Completions received",
        len(completions) > 0,
        f"{len(completions)} completion(s)",
    ))

    # Expected texts from default mock scenario
    expected = {"これはテストです", "音声認識のテスト中", "デバッグモード"}
    actual = set(completions)
    results.append(TestResult(
        "Expected texts match",
        expected == actual,
        f"expected={expected}, got={actual}",
    ))

    # No duplicate completions
    has_dups = len(completions) != len(set(completions))
    results.append(TestResult(
        "No duplicate completions",
        not has_dups,
        f"texts={completions}" if has_dups else "",
    ))

    # Latency: commit → first delta (measures server+network response time)
    commit_events = r.commits
    if commit_events:
        first_commit = commit_events[0]
        first_delta_after = next(
            (e for e in r.deltas if e.time_ms > first_commit.time_ms), None
        )
        if first_delta_after:
            latency = first_delta_after.time_ms - first_commit.time_ms
            results.append(TestResult(
                "First delta latency",
                latency < 5000,  # Should be well under 5s
                f"{latency:.1f}ms after first commit",
            ))
        else:
            results.append(TestResult(
                "First delta latency",
                False,
                "No delta received after commit",
            ))

    # Verify delta → completed ordering per commit
    # Each commit should produce deltas THEN a completed
    commit_times = [e.time_ms for e in commit_events]
    ordering_ok = True
    for completion in r.completions:
        # Find deltas with same text prefix that came before this completion
        earlier_deltas = [
            d for d in r.deltas if d.time_ms < completion.time_ms
        ]
        if not earlier_deltas:
            ordering_ok = False
            break
    results.append(TestResult(
        "Deltas precede completions",
        ordering_ok,
        "" if ordering_ok else "Found completion without preceding deltas",
    ))

    # No error events
    results.append(TestResult(
        "No error events",
        len(r.events) == 0 or not any(e.type == "error" for e in r.events),
        f"{len([e for e in r.events if e.type == 'error'])} errors"
        if any(e.type == "error" for e in r.events) else "",
    ))

    return results


async def test_mid_stop(
    wav_path: str, ws_url: str, verbose: bool
) -> list[TestResult]:
    """Test StopRecording mid-stream.

    Simulates the user pressing Shift+Space to stop recording
    while audio is still being streamed. Uses chunk-based stop
    (after 25 chunks = first speech burst) for deterministic behavior.

    Verifies:
      - Pipeline shuts down cleanly (no crash)
      - Final commit is sent for remaining audio
      - Fewer completions than full flow (stopped early)
      - No error events
    """
    results = []

    # Stop after 25 chunks (during first speech burst, before all commits)
    r = await run_pipeline(wav_path, ws_url, stop_after_chunks=25)

    if verbose:
        print(f"\n  {_DIM}--- Event log (mid-stop) ---{_RESET}")
        for e in r.events:
            print(f"  {_DIM}{e}{_RESET}")
        print()

    if r.error:
        results.append(TestResult("Clean shutdown", False, r.error))
        return results

    # Should not crash
    results.append(TestResult("Clean shutdown", True, "No exception"))

    # Stop event should be recorded
    stop_events = [e for e in r.events if e.type == "stop"]
    results.append(TestResult(
        "Stop event recorded",
        len(stop_events) == 1,
        f"{len(stop_events)} stop event(s)",
    ))

    # Should have sent final commit for remaining audio
    all_commits = [
        e for e in r.events
        if e.type in ("commit", "final_commit", "flush_commit")
    ]
    results.append(TestResult(
        "Commits sent",
        len(all_commits) >= 1,
        f"{len(all_commits)} commit(s)",
    ))

    # Fewer completions than full flow (we stopped early)
    results.append(TestResult(
        "Partial completions",
        len(r.completions) < 3,
        f"{len(r.completions)} completion(s) (expected < 3)",
    ))

    # No error events
    errors = [e for e in r.events if e.type == "error"]
    results.append(TestResult(
        "No error events",
        len(errors) == 0,
        f"{len(errors)} errors" if errors else "",
    ))

    return results


async def test_immediate_stop(
    wav_path: str, ws_url: str, verbose: bool
) -> list[TestResult]:
    """Test immediate stop (start → stop with minimal audio).

    Simulates accidental key press: stops after just 5 chunks (500ms of audio,
    still in calibration phase). Verifies:
      - Pipeline handles early termination gracefully
      - No crash, no orphaned tasks
    """
    results = []

    # Stop after 5 chunks (still during calibration, no commits yet)
    r = await run_pipeline(wav_path, ws_url, stop_after_chunks=5)

    if verbose:
        print(f"\n  {_DIM}--- Event log (immediate stop) ---{_RESET}")
        for e in r.events:
            print(f"  {_DIM}{e}{_RESET}")
        print()

    results.append(TestResult(
        "Clean shutdown",
        r.error is None,
        r.error or "No exception",
    ))

    # No speech-based commits (stopped during calibration)
    speech_commits = [e for e in r.events if e.type == "commit"]
    results.append(TestResult(
        "No speech commits (stopped during calibration)",
        len(speech_commits) == 0,
        f"{len(speech_commits)} commit(s)",
    ))

    # Few or no completions expected
    results.append(TestResult(
        "Minimal events",
        len(r.completions) <= 1,
        f"{len(r.completions)} completions, {len(r.deltas)} deltas",
    ))

    return results


async def test_preedit_behavior(
    wav_path: str, ws_url: str, verbose: bool
) -> list[TestResult]:
    """Test preedit (delta) replacement behavior.

    The fcitx5 plugin replaces preedit text on each delta (not append).
    Verifies:
      - Deltas for each commit have increasing length (progressive partial text)
      - Completed text is the final version (not accumulated deltas)
      - Interleaved deltas from concurrent commits are handled correctly

    Note: The mock server processes commits asynchronously, so deltas from
    different commits may interleave. This is realistic — the real server
    can also send interleaved responses.
    """
    results = []

    r = await run_pipeline(wav_path, ws_url)

    if verbose:
        print(f"\n  {_DIM}--- Event log (preedit) ---{_RESET}")
        for e in r.events:
            print(f"  {_DIM}{e}{_RESET}")
        print()

    if r.error:
        results.append(TestResult("No errors", False, r.error))
        return results

    # Group deltas by their completion: match each delta to the completion
    # whose text it is a substring of (prefix matching)
    completion_events = r.completions
    if not completion_events:
        results.append(TestResult(
            "Delta text grows per commit",
            False,
            "No completions to analyze",
        ))
        return results

    # Map each delta to its completion by checking if delta text is
    # a prefix/substring of the completion text
    delta_groups: dict[str, list[Event]] = {c.text: [] for c in completion_events}

    for delta in r.deltas:
        for comp in completion_events:
            # Delta text should be a prefix of or contained in completion text
            if comp.text.startswith(delta.text) or delta.text in comp.text:
                delta_groups[comp.text].append(delta)
                break

    # Check that within each group, delta text length grows
    all_growing = True
    details = []
    for comp_text, deltas in delta_groups.items():
        if len(deltas) >= 2:
            lengths = [len(d.text) for d in deltas]
            is_growing = all(
                lengths[i] <= lengths[i + 1]
                for i in range(len(lengths) - 1)
            )
            if not is_growing:
                all_growing = False
            details.append(f"'{comp_text}': lengths={lengths}")
        elif len(deltas) == 1:
            details.append(f"'{comp_text}': 1 delta")

    results.append(TestResult(
        "Delta text grows per commit",
        all_growing,
        "; ".join(details) if details else "no delta groups",
    ))

    # Verify completed text is not just concatenated deltas
    comp = completion_events[0]
    results.append(TestResult(
        "Completed text is clean",
        len(comp.text) > 0 and "\n" not in comp.text,
        f"'{comp.text}'",
    ))

    # Check interleaving awareness: if deltas from different commits
    # interleave, the plugin's preedit would flicker between them
    delta_texts = [d.text for d in r.deltas]
    interleaved = False
    last_group = None
    for delta in r.deltas:
        # Determine which completion this delta belongs to
        for comp in completion_events:
            if comp.text.startswith(delta.text) or delta.text in comp.text:
                if last_group is not None and last_group != comp.text:
                    interleaved = True
                last_group = comp.text
                break

    results.append(TestResult(
        "Interleaving detected (informational)",
        True,  # Always passes — this is informational
        "deltas from different commits interleave"
        if interleaved
        else "deltas arrive in commit order",
    ))

    return results


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


SCENARIOS = {
    "normal": ("Normal flow (full WAV)", test_normal_flow),
    "mid-stop": ("Mid-recording stop", test_mid_stop),
    "immediate-stop": ("Immediate stop", test_immediate_stop),
    "preedit": ("Preedit replacement behavior", test_preedit_behavior),
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pipeline integration test — verifies fcitx5 plugin behavior.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Port for the mock Riva server.",
    )
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        help="Run only this scenario (default: all).",
    )
    parser.add_argument(
        "--wav",
        metavar="FILE",
        help="Use an existing WAV file instead of generating one.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show all events in the timeline.",
    )
    args = parser.parse_args()

    python = sys.executable
    wav_path = args.wav
    wav_tmpfile = None

    # --- Generate test WAV if needed ---
    if not wav_path:
        wav_tmpfile = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        wav_path = wav_tmpfile.name
        wav_tmpfile.close()

        print(f"Generating test WAV: {wav_path}")
        gen_result = subprocess.run(
            [python, str(TOOLS_DIR / "generate_test_wav.py"), "-o", wav_path],
            capture_output=True,
            text=True,
        )
        if gen_result.returncode != 0:
            print(f"FAILED to generate WAV:\n{gen_result.stderr}", file=sys.stderr)
            return 1

    # --- Start mock server ---
    print(f"Starting mock Riva server on port {args.port}...")
    server_proc = subprocess.Popen(
        [python, str(TOOLS_DIR / "mock_riva_server.py"), "--port", str(args.port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1.0)
    if server_proc.poll() is not None:
        _, stderr = server_proc.communicate()
        print(f"FAILED to start mock server:\n{stderr.decode()}", file=sys.stderr)
        return 1

    ws_url = f"ws://localhost:{args.port}"

    # --- Run scenarios ---
    scenarios_to_run = (
        {args.scenario: SCENARIOS[args.scenario]}
        if args.scenario
        else SCENARIOS
    )

    all_results: list[TestResult] = []
    try:
        for key, (label, test_fn) in scenarios_to_run.items():
            print(f"\n{_BOLD}=== {label} ==={_RESET}")
            try:
                results = asyncio.run(test_fn(wav_path, ws_url, args.verbose))
            except Exception as e:
                results = [TestResult(f"Scenario '{key}'", False, str(e))]

            for r in results:
                print(str(r))
            all_results.extend(results)
    finally:
        # --- Stop server ---
        server_proc.send_signal(signal.SIGTERM)
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait()

        # Clean up temp WAV
        if wav_tmpfile:
            os.unlink(wav_path)

    # --- Summary ---
    passed = sum(1 for r in all_results if r.passed)
    failed = sum(1 for r in all_results if not r.passed)
    print(f"\n{_BOLD}=== Summary ==={_RESET}")
    print(f"  {_GREEN}{passed} passed{_RESET}, {_RED if failed else _DIM}{failed} failed{_RESET}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
