#!/usr/bin/env python3
"""End-to-end test for the fcitx5-voice pipeline.

Two modes:

  mock (default)
    Starts a local mock Riva server, sends a sine-wave WAV, and verifies
    the transcription responses. No real server or microphone needed.

  live (--live)
    Starts the daemon with --replay-wav against a real Riva server, monitors
    D-Bus signals, and verifies completions against expected substrings.
    The daemon emits RecordingStopped automatically when the WAV ends.

Usage:
    # Mock mode – fully offline
    python tools/run_e2e.py

    # Live mode – real server, default fixture (short_phrase)
    python tools/run_e2e.py --live --url ws://spark-fd28.local:9000

    # Live mode – specific fixture, expected substring
    python tools/run_e2e.py --live --url ws://spark-fd28.local:9000 \\
        --fixture long_speech --expect "音声認識"

    # Live mode – custom WAV file
    python tools/run_e2e.py --live --url ws://spark-fd28.local:9000 \\
        --wav path/to/custom.wav

Exit codes:  0 = pass,  1 = fail
"""

import argparse
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parent
FIXTURES_DIR = TOOLS_DIR / "fixtures"

DBUS_DEST = "org.fcitx.Fcitx5.Voice"
DBUS_PATH = "/org/fcitx/Fcitx5/Voice"
DBUS_IFACE = "org.fcitx.Fcitx5.Voice"

DEFAULT_MOCK_PORT = 9199
DEFAULT_FIXTURE = "multi_phrase"
DEFAULT_LIVE_TIMEOUT = 40  # seconds

# Mock mode: expected completions (order-independent, checked as a set)
MOCK_EXPECTED = {
    "これはテストです",
    "デバッグモード",
    "音声認識のテスト中",
}


# ---------------------------------------------------------------------------
# Mock mode (existing behavior)
# ---------------------------------------------------------------------------

def run_mock_mode(args: argparse.Namespace) -> int:
    python = sys.executable
    wav_path = args.wav

    if not wav_path:
        wav_path = str(FIXTURES_DIR / "multi_phrase.wav")
        if not Path(wav_path).exists():
            print(f"[1/4] Generating fixture: short_phrase")
            r = subprocess.run(
                [python, str(TOOLS_DIR / "generate_fixtures.py"), "multi_phrase"],
                capture_output=True, text=True,
            )
            if r.returncode != 0 or not Path(wav_path).exists():
                print(f"FAILED to generate fixture:\n{r.stderr}", file=sys.stderr)
                return 1
        print(f"[1/4] Using fixture: {wav_path}")
    else:
        print(f"[1/4] Using WAV: {wav_path}")

    port = args.port or DEFAULT_MOCK_PORT
    print(f"[2/4] Starting mock Riva server on port {port}...")
    server_cmd = [python, str(TOOLS_DIR / "mock_riva_server.py"), "--port", str(port)]
    if args.scenario:
        server_cmd += ["--scenario", args.scenario]
    server_proc = subprocess.Popen(server_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(1.0)
    if server_proc.poll() is not None:
        _, err = server_proc.communicate()
        print(f"FAILED to start mock server:\n{err.decode()}", file=sys.stderr)
        return 1
    print(f"       Server PID: {server_proc.pid}")

    print(f"[3/4] Replaying WAV to mock server...")
    capture_file = args.capture or tempfile.NamedTemporaryFile(
        suffix=".json", delete=False
    ).name
    replay_cmd = [
        python, str(TOOLS_DIR / "replay_to_server.py"),
        wav_path,
        "--url", f"ws://localhost:{port}",
        "--chunk-delay", "0.01",
        "--commit-interval", "10",
        "--no-color",
        "--capture", capture_file,
    ]
    if args.verbose:
        replay_cmd.append("--verbose")
    replay_result = subprocess.run(replay_cmd, text=True)

    print(f"[4/4] Stopping mock server...")
    server_proc.send_signal(signal.SIGTERM)
    try:
        server_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server_proc.kill()
        server_proc.wait()

    # Set-based assertion: order-independent check of completion texts
    import json as _json
    actual_texts: set[str] = set()
    try:
        with open(capture_file, encoding="utf-8") as f:
            scenarios = _json.load(f)
        # Each scenario is [delta1, delta2, ..., final_text]; last element is the completion
        actual_texts = {s[-1] for s in scenarios if s}
    except (FileNotFoundError, _json.JSONDecodeError, IndexError):
        pass
    finally:
        if not args.capture and os.path.exists(capture_file):
            os.unlink(capture_file)

    print()
    if replay_result.returncode != 0:
        print("E2E TEST FAILED (replay error)")
        return 1

    missing = MOCK_EXPECTED - actual_texts
    extra = actual_texts - MOCK_EXPECTED
    if missing:
        print(f"  FAIL: missing completions: {missing}")
    if extra:
        print(f"  WARN: unexpected completions: {extra}")

    if not missing:
        print("E2E TEST PASSED")
        return 0
    else:
        print("E2E TEST FAILED")
        return 1


# ---------------------------------------------------------------------------
# Live mode (daemon + real server + D-Bus monitoring)
# ---------------------------------------------------------------------------

def _gdbus_call(method: str, timeout: float = 5.0) -> bool:
    r = subprocess.run(
        [
            "gdbus", "call", "--session",
            "--dest", DBUS_DEST,
            "--object-path", DBUS_PATH,
            "--method", f"{DBUS_IFACE}.{method}",
            "--timeout", str(int(timeout)),
        ],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def _wait_for_dbus_service(timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _gdbus_call("GetStatus", timeout=1.0):
            return True
        time.sleep(0.3)
    return False


def _stream_lines(proc: subprocess.Popen, q: "queue.Queue[str | None]") -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        q.put(line)
    q.put(None)


def run_live_mode(args: argparse.Namespace) -> int:
    if not args.url:
        print("ERROR: --url is required for live mode", file=sys.stderr)
        return 1

    # Resolve WAV
    if args.wav:
        wav_path = args.wav
        if not Path(wav_path).exists():
            print(f"ERROR: WAV not found: {wav_path}", file=sys.stderr)
            return 1
        print(f"[1/5] Using WAV: {wav_path}")
    else:
        fixture = args.fixture or DEFAULT_FIXTURE
        fixture_path = FIXTURES_DIR / f"{fixture}.wav"
        if not fixture_path.exists():
            print(f"[1/5] Fixture '{fixture}' not found, generating...")
            r = subprocess.run(
                [sys.executable, str(TOOLS_DIR / "generate_fixtures.py"), fixture],
                text=True,
            )
            if r.returncode != 0 or not fixture_path.exists():
                print(f"ERROR: Failed to generate fixture '{fixture}'", file=sys.stderr)
                return 1
        else:
            print(f"[1/5] Using fixture: {fixture_path.name}")
        wav_path = str(fixture_path)

    # Start daemon
    print(f"[2/5] Starting daemon (url={args.url}, replay={Path(wav_path).name})...")
    daemon_cmd = [
        sys.executable, "-m", "daemon.main",
        "--url", args.url,
        "--replay-wav", wav_path,
    ]
    if args.debug:
        daemon_cmd.append("--debug")
    daemon_proc = subprocess.Popen(
        daemon_cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Wait for D-Bus service
    print(f"[3/5] Waiting for D-Bus service...")
    if not _wait_for_dbus_service(timeout=10.0):
        out, _ = daemon_proc.communicate(timeout=2)
        print(f"ERROR: D-Bus service did not appear (another daemon running?)", file=sys.stderr)
        print(f"Daemon output:\n{out}", file=sys.stderr)
        daemon_proc.terminate()
        daemon_proc.wait()
        return 1
    print(f"       Ready (daemon PID={daemon_proc.pid})")

    # Start monitoring and recording
    print(f"[4/5] Monitoring D-Bus signals...")
    monitor_proc = subprocess.Popen(
        ["gdbus", "monitor", "--session", "--dest", DBUS_DEST],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    time.sleep(0.2)  # Let monitor subscribe before StartRecording

    if not _gdbus_call("StartRecording"):
        print("ERROR: StartRecording failed", file=sys.stderr)
        monitor_proc.terminate()
        daemon_proc.terminate()
        daemon_proc.wait()
        return 1

    line_queue: queue.Queue[str | None] = queue.Queue()
    threading.Thread(
        target=_stream_lines, args=(monitor_proc, line_queue), daemon=True
    ).start()

    completions: list[str] = []
    recording_stopped = False
    deadline = time.time() + (args.timeout or DEFAULT_LIVE_TIMEOUT)
    re_complete = re.compile(r"TranscriptionComplete \('(.+)',\s*\d+\)")

    while time.time() < deadline:
        try:
            line = line_queue.get(timeout=min(deadline - time.time(), 1.0))
        except queue.Empty:
            continue
        if line is None:
            break
        line = line.rstrip()
        if args.verbose:
            print(f"       {line}")
        if "RecordingStopped" in line:
            recording_stopped = True
            break
        m = re_complete.search(line)
        if m:
            completions.append(m.group(1))

    monitor_proc.terminate()
    monitor_proc.wait()

    # Stop daemon
    print(f"[5/5] Stopping daemon...")
    daemon_proc.terminate()
    try:
        daemon_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        daemon_proc.kill()
        daemon_proc.wait()

    # Report
    print()
    print("=== Results ===")
    if not recording_stopped:
        print(f"  WARNING: RecordingStopped not received within {args.timeout or DEFAULT_LIVE_TIMEOUT}s")

    if completions:
        print(f"  Completions ({len(completions)}):")
        for i, text in enumerate(completions, 1):
            print(f"    [{i}] {text}")
    else:
        print("  Completions: (none)")

    failures: list[str] = []
    if not completions:
        failures.append("No TranscriptionComplete events received")
    for expected in (args.expect or []):
        if not any(expected in c for c in completions):
            failures.append(f"Expected substring not found: {expected!r}")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print("E2E TEST FAILED")
        return 1
    print("E2E TEST PASSED")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end test for fcitx5-voice pipeline.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Live mode: real Riva server + daemon instead of mock server.",
    )
    parser.add_argument("--url", metavar="URL", help="Riva WebSocket URL (live mode).")
    parser.add_argument(
        "--fixture", metavar="NAME", default=DEFAULT_FIXTURE,
        help=f"Fixture from tools/fixtures/ (live mode, default: {DEFAULT_FIXTURE}).",
    )
    parser.add_argument(
        "--expect", metavar="TEXT", action="append",
        help="Substring expected in at least one completion (live mode). Repeatable.",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_LIVE_TIMEOUT,
        help="Seconds to wait for RecordingStopped (live mode).",
    )
    parser.add_argument("--debug", action="store_true", help="Enable daemon debug logging.")
    parser.add_argument("--wav", metavar="FILE", help="Use a specific WAV file.")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--port", type=int, help=f"Mock server port (mock mode).")
    parser.add_argument("--scenario", metavar="FILE", help="Mock scenario JSON (mock mode).")
    parser.add_argument("--capture", metavar="FILE", help="Capture responses to JSON (mock mode).")

    args = parser.parse_args()
    return run_live_mode(args) if args.live else run_mock_mode(args)


if __name__ == "__main__":
    sys.exit(main())
