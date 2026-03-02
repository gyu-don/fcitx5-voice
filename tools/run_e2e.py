#!/usr/bin/env python3
"""One-command end-to-end test for the voice recognition debug tools.

Starts a mock Riva server, generates a test WAV, replays it, verifies
the transcription results, and reports pass/fail.

Usage:
    python tools/run_e2e.py                # Run with defaults
    python tools/run_e2e.py --verbose       # Show all chunk sends
    python tools/run_e2e.py --keep-wav      # Don't delete the generated WAV
    python tools/run_e2e.py --scenario s.json  # Use custom mock scenario

Exit codes:
    0 = all assertions passed
    1 = assertion failure or error
"""

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parent

# Default port for mock server (avoid conflict with real Riva on 9000)
DEFAULT_PORT = 9199

# Default mock scenario completed texts in arrival order.
# Commit 3 ("デバッグモード", 1 delta) completes before commit 2
# ("音声認識のテスト中", 3 deltas) because it has fewer deltas and
# the mock server processes responses asynchronously with delays.
DEFAULT_EXPECTED = [
    "これはテストです",
    "デバッグモード",
    "音声認識のテスト中",
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run end-to-end test: mock server + WAV replay + assertion.",
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
        metavar="FILE",
        help="Custom scenario JSON for the mock server.",
    )
    parser.add_argument(
        "--wav",
        metavar="FILE",
        help="Use an existing WAV file instead of generating one.",
    )
    parser.add_argument(
        "--keep-wav",
        action="store_true",
        help="Keep the generated WAV file after the test.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output (pass -v to replay tool).",
    )
    parser.add_argument(
        "--capture",
        metavar="FILE",
        help="Capture server responses to a scenario JSON file.",
    )
    args = parser.parse_args()

    python = sys.executable
    wav_path = args.wav
    wav_tmpfile = None

    # --- Step 1: Generate test WAV if not provided ---
    if not wav_path:
        wav_tmpfile = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        wav_path = wav_tmpfile.name
        wav_tmpfile.close()

        print(f"[1/4] Generating test WAV: {wav_path}")
        gen_result = subprocess.run(
            [python, str(TOOLS_DIR / "generate_test_wav.py"), "-o", wav_path],
            capture_output=True,
            text=True,
        )
        if gen_result.returncode != 0:
            print(f"FAILED to generate WAV:\n{gen_result.stderr}", file=sys.stderr)
            return 1
        print(f"       {gen_result.stdout.strip().splitlines()[0]}")
    else:
        print(f"[1/4] Using existing WAV: {wav_path}")

    # --- Step 2: Start mock server ---
    print(f"[2/4] Starting mock Riva server on port {args.port}...")
    server_cmd = [
        python,
        str(TOOLS_DIR / "mock_riva_server.py"),
        "--port", str(args.port),
    ]
    if args.scenario:
        server_cmd += ["--scenario", args.scenario]

    server_proc = subprocess.Popen(
        server_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to be ready
    time.sleep(1.0)
    if server_proc.poll() is not None:
        _, stderr = server_proc.communicate()
        print(f"FAILED to start mock server:\n{stderr.decode()}", file=sys.stderr)
        return 1
    print(f"       Server PID: {server_proc.pid}")

    # --- Step 3: Run replay with assertions ---
    print(f"[3/4] Replaying WAV to mock server...")
    replay_cmd = [
        python,
        str(TOOLS_DIR / "replay_to_server.py"),
        wav_path,
        "--url", f"ws://localhost:{args.port}",
        "--chunk-delay", "0.01",
        "--no-color",
    ]

    # Add expected assertions
    for text in DEFAULT_EXPECTED:
        replay_cmd += ["--expect", text]

    if args.verbose:
        replay_cmd.append("--verbose")
    if args.capture:
        replay_cmd += ["--capture", args.capture]

    replay_result = subprocess.run(replay_cmd, text=True)

    # --- Step 4: Stop server and report ---
    print(f"[4/4] Stopping mock server...")
    server_proc.send_signal(signal.SIGTERM)
    try:
        server_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server_proc.kill()
        server_proc.wait()

    # Cleanup temp WAV
    if wav_tmpfile and not args.keep_wav:
        os.unlink(wav_path)

    # Final verdict
    print()
    if replay_result.returncode == 0:
        print("E2E TEST PASSED")
    else:
        print("E2E TEST FAILED")

    return replay_result.returncode


if __name__ == "__main__":
    sys.exit(main())
