#!/usr/bin/env python3
"""Mock NIM Riva ASR WebSocket server for testing fcitx5-voice without a real ASR server.

Implements the NIM Riva realtime transcription protocol:
  1. Client connects to /v1/realtime?intent=transcription
  2. Server sends conversation.created
  3. Client sends transcription_session.update
  4. Server sends transcription_session.updated
  5. Client streams audio via input_audio_buffer.append
  6. Client sends input_audio_buffer.commit
  7. Server responds with delta events then a completed event

Usage:
    python tools/mock_riva_server.py
    python tools/mock_riva_server.py --port 9100 --debug
    python tools/mock_riva_server.py --scenario my_scenario.json --delay 0.05

Custom scenario JSON format:
    [
        ["これ", "これは", "これはテスト", "これはテストです"],
        ["音声", "音声認識", "音声認識のテスト"]
    ]
    Each entry is a list of strings. All but the last are sent as delta events;
    the last is sent as a completed event.

Test with the daemon:
    uv run fcitx5-voice-daemon --url ws://localhost:9100 --debug
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime
from typing import Any

import websockets

# websockets 14.x+ uses ServerConnection; older versions used WebSocketServerProtocol.
# Import whichever is available for use in type hints.
try:
    from websockets.asyncio.server import ServerConnection as _WSConnection
except ImportError:
    from websockets.server import WebSocketServerProtocol as _WSConnection  # type: ignore[assignment,no-redef]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audio format constants (must match daemon/recorder.py)
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000    # Hz
SAMPLE_WIDTH = 2       # bytes per sample (int16)
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH  # 32000 bytes/sec for 16kHz mono 16-bit

# ---------------------------------------------------------------------------
# Default scenario
# ---------------------------------------------------------------------------

DEFAULT_RESPONSES: list[list[str]] = [
    ["これ", "これは", "これはテスト", "これはテストです"],
    ["音声", "音声認識", "音声認識の", "音声認識のテスト中"],
    ["デバッグ", "デバッグモード"],
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    """Return a short HH:MM:SS.mmm timestamp for log lines."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _conv_id() -> str:
    return f"conv_{uuid.uuid4().hex[:12]}"


def _session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


def _audio_duration(byte_count: int) -> float:
    """Convert raw PCM16 byte count to duration in seconds."""
    return byte_count / BYTES_PER_SECOND


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------

async def handle_connection(
    websocket: _WSConnection,
    responses: list[list[str]],
    delay: float,
) -> None:
    """Handle a single WebSocket client connection.

    Args:
        websocket:  The connected WebSocket client.
        responses:  List of response scenarios (cycled when exhausted).
        delay:      Base delay in seconds between delta events and before
                    the first delta after a commit.
    """
    remote = websocket.remote_address
    conn_id = uuid.uuid4().hex[:8]
    log_prefix = f"[conn:{conn_id} {remote[0]}:{remote[1]}]"

    logger.info(f"{log_prefix} Client connected")

    # Per-connection state
    commit_count = 0
    audio_bytes_total = 0     # across entire connection
    audio_bytes_since_commit = 0  # reset on each commit

    # --- Step 1: Send conversation.created ---
    conv_id = _conv_id()
    created_msg = json.dumps({"type": "conversation.created", "conversation": {"id": conv_id}})
    await websocket.send(created_msg)
    logger.debug(f"{log_prefix} Sent conversation.created (id={conv_id})")

    session_configured = False

    try:
        async for raw_msg in websocket:
            ts = _timestamp()

            # Parse incoming message
            try:
                msg: dict[str, Any] = json.loads(raw_msg)
            except json.JSONDecodeError as exc:
                logger.warning(f"{log_prefix} {ts} Received invalid JSON: {exc}")
                continue

            msg_type = msg.get("type", "<unknown>")
            event_id = msg.get("event_id", "")
            logger.debug(
                f"{log_prefix} {ts} Received: type={msg_type}"
                + (f" event_id={event_id}" if event_id else "")
            )

            # --- Step 2: Handle transcription_session.update ---
            if msg_type == "transcription_session.update":
                session = msg.get("session", {})
                audio_fmt = session.get("input_audio_format", "unknown")
                transcription_cfg = session.get("input_audio_transcription", {})
                language = transcription_cfg.get("language", "unknown")
                model = transcription_cfg.get("model", "unknown")
                recognition_cfg = session.get("recognition_config", {})

                logger.info(
                    f"{log_prefix} Session configured: "
                    f"format={audio_fmt}, language={language}, "
                    f"model={model[:40]}{'...' if len(model) > 40 else ''}"
                )

                sess_id = _session_id()
                updated_msg = json.dumps({
                    "type": "transcription_session.updated",
                    "session": {
                        "id": sess_id,
                        "input_audio_format": audio_fmt,
                        "input_audio_transcription": {
                            "language": language,
                            "model": model,
                        },
                        "recognition_config": recognition_cfg,
                    },
                })
                await websocket.send(updated_msg)
                logger.debug(f"{log_prefix} Sent transcription_session.updated (id={sess_id})")
                session_configured = True

            # --- Step 3: Handle audio append ---
            elif msg_type == "input_audio_buffer.append":
                import base64
                audio_b64 = msg.get("audio", "")
                try:
                    audio_raw = base64.b64decode(audio_b64)
                    chunk_bytes = len(audio_raw)
                except Exception as exc:
                    logger.warning(f"{log_prefix} {ts} Failed to decode audio: {exc}")
                    chunk_bytes = 0

                audio_bytes_since_commit += chunk_bytes
                audio_bytes_total += chunk_bytes

                logger.debug(
                    f"{log_prefix} {ts} Audio chunk: "
                    f"{chunk_bytes} bytes "
                    f"({_audio_duration(chunk_bytes):.3f}s), "
                    f"session total: {audio_bytes_since_commit} bytes "
                    f"({_audio_duration(audio_bytes_since_commit):.3f}s)"
                )

            # --- Step 4: Handle commit ---
            elif msg_type == "input_audio_buffer.commit":
                commit_count += 1
                commit_duration = _audio_duration(audio_bytes_since_commit)

                # Pick the response for this commit (cycle through scenarios)
                scenario_index = (commit_count - 1) % len(responses)
                scenario = responses[scenario_index]
                final_text = scenario[-1]
                deltas = scenario[:-1]

                logger.info(
                    f"{log_prefix} {ts} "
                    f"Commit #{commit_count}: received {audio_bytes_since_commit} bytes "
                    f"({commit_duration:.2f}s of audio), "
                    f"responding with: '{final_text}'"
                )

                # Reset per-commit audio counter
                audio_bytes_since_commit = 0

                # Schedule response asynchronously so we don't block the recv loop
                asyncio.ensure_future(
                    _send_transcription_response(
                        websocket=websocket,
                        log_prefix=log_prefix,
                        deltas=deltas,
                        final_text=final_text,
                        delay=delay,
                        commit_num=commit_count,
                    )
                )

            else:
                logger.debug(f"{log_prefix} {ts} Ignored message type: {msg_type}")

    except websockets.exceptions.ConnectionClosedOK:
        logger.info(f"{log_prefix} Client disconnected cleanly")
    except websockets.exceptions.ConnectionClosedError as exc:
        logger.warning(f"{log_prefix} Client disconnected with error: {exc}")
    except Exception as exc:
        logger.error(f"{log_prefix} Unexpected error: {exc}", exc_info=True)
    finally:
        total_duration = _audio_duration(audio_bytes_total)
        logger.info(
            f"{log_prefix} Session summary: "
            f"commits={commit_count}, "
            f"total_audio={audio_bytes_total} bytes ({total_duration:.2f}s)"
        )


async def _send_transcription_response(
    websocket: _WSConnection,
    log_prefix: str,
    deltas: list[str],
    final_text: str,
    delay: float,
    commit_num: int,
) -> None:
    """Send delta events followed by a completed event for one commit.

    Args:
        websocket:   The client WebSocket to send to.
        log_prefix:  Logging prefix string for this connection.
        deltas:      List of partial transcription strings to send as delta events.
        final_text:  The final transcription text to send as completed event.
        delay:       Base delay in seconds between events.
        commit_num:  The commit sequence number (for logging).
    """
    try:
        # Initial pause before first delta (simulates ASR processing latency)
        await asyncio.sleep(delay * 2)

        # Send delta events
        for i, delta_text in enumerate(deltas):
            delta_msg = json.dumps({
                "type": "conversation.item.input_audio_transcription.delta",
                "delta": delta_text,
            })
            await websocket.send(delta_msg)
            ts = _timestamp()
            logger.debug(
                f"{log_prefix} {ts} Sent delta #{i + 1}/{len(deltas)} "
                f"for commit #{commit_num}: '{delta_text}'"
            )
            await asyncio.sleep(delay)

        # Send completed event
        completed_msg = json.dumps({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": final_text,
        })
        await websocket.send(completed_msg)
        ts = _timestamp()
        logger.info(
            f"{log_prefix} {ts} Sent completed for commit #{commit_num}: '{final_text}'"
        )

    except websockets.exceptions.ConnectionClosed:
        logger.debug(
            f"{log_prefix} Connection closed while sending response for commit #{commit_num}"
        )
    except Exception as exc:
        logger.error(
            f"{log_prefix} Error sending response for commit #{commit_num}: {exc}",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Scenario loading
# ---------------------------------------------------------------------------

def load_scenario(path: str) -> list[list[str]]:
    """Load a custom scenario from a JSON file.

    The JSON file should be a list of lists of strings, e.g.:
        [
            ["これ", "これは", "これはテストです"],
            ["hello", "hello world"]
        ]

    Each inner list must have at least one entry. All entries but the last
    are sent as delta events; the last is sent as the completed transcript.

    Args:
        path:  Path to the JSON file.

    Returns:
        Parsed scenario list.

    Raises:
        SystemExit: If the file cannot be read or parsed.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.error(f"Scenario file not found: {path}")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        logger.error(f"Invalid JSON in scenario file {path}: {exc}")
        sys.exit(1)

    if not isinstance(data, list) or not all(
        isinstance(entry, list) and len(entry) >= 1 and
        all(isinstance(s, str) for s in entry)
        for entry in data
    ):
        logger.error(
            f"Scenario file {path} must be a JSON array of non-empty string arrays. "
            "Example: [[\"これ\", \"これはテストです\"], [\"hello\", \"hello world\"]]"
        )
        sys.exit(1)

    logger.info(f"Loaded {len(data)} scenario(s) from {path}")
    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Mock NIM Riva ASR WebSocket server for testing fcitx5-voice "
            "without a real ASR server. Implements the NIM Riva realtime "
            "transcription WebSocket protocol."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="Bind address for the server.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9100,
        help=(
            "Port to listen on. Default is 9100 (different from real Riva's "
            "9000 to avoid conflicts)."
        ),
    )
    parser.add_argument(
        "--scenario",
        metavar="FILE",
        default=None,
        help=(
            "Path to a JSON file with custom response scenarios. "
            "Format: [[\"delta1\", \"delta2\", \"final\"], ...]. "
            "If not provided, uses built-in Japanese test responses."
        ),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        metavar="SECONDS",
        help=(
            "Base delay in seconds between transcription events. "
            "The initial pause before the first delta is 2x this value."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging (includes audio chunk details, all sent/received messages).",
    )
    parser.add_argument(
        "--language",
        default="ja-JP",
        help="Language code for logging context (informational only).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_server(host: str, port: int, responses: list[list[str]], delay: float) -> None:
    """Start the mock Riva WebSocket server and run until cancelled.

    Args:
        host:       Bind address.
        port:       Port number.
        responses:  List of response scenarios.
        delay:      Base delay between events in seconds.
    """
    async def handler(websocket: _WSConnection) -> None:
        await handle_connection(websocket, responses, delay)

    logger.info(f"Mock Riva ASR server starting on ws://{host}:{port}")
    logger.info(f"  Path: /v1/realtime?intent=transcription")
    logger.info(f"  Scenarios: {len(responses)} response(s) loaded (cycling)")
    logger.info(f"  Delay: {delay}s between events ({delay * 2}s initial pause)")
    logger.info("Press Ctrl+C to stop.")

    # Log scenario summary
    for i, scenario in enumerate(responses):
        final = scenario[-1]
        n_deltas = len(scenario) - 1
        logger.info(f"  Scenario {i + 1}: {n_deltas} delta(s) -> '{final}'")

    async with websockets.serve(
        handler,
        host,
        port,
        compression="deflate",
    ):
        logger.info(f"Server ready at ws://{host}:{port}")
        await asyncio.Future()  # run forever until cancelled


def main() -> None:
    args = parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # Reduce websockets library noise unless in debug mode
    if not args.debug:
        logging.getLogger("websockets").setLevel(logging.WARNING)

    # Load scenario
    if args.scenario:
        responses = load_scenario(args.scenario)
    else:
        responses = DEFAULT_RESPONSES
        logger.info(
            f"Using default Japanese scenario ({len(DEFAULT_RESPONSES)} responses). "
            "Pass --scenario FILE to use custom responses."
        )

    logger.info(f"Language context: {args.language}")

    # Run server
    try:
        asyncio.run(run_server(args.host, args.port, responses, args.delay))
    except KeyboardInterrupt:
        logger.info("Server stopped by user (Ctrl+C).")


if __name__ == "__main__":
    main()
