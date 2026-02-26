"""WebSocket client for NVIDIA NIM Riva real-time ASR.

Implements the NIM Riva transcription WebSocket protocol:
  1. Connect to /v1/realtime?intent=transcription
  2. Receive conversation.created
  3. Send transcription_session.update with config
  4. Receive transcription_session.updated
  5. Stream audio as base64 PCM16 via input_audio_buffer.append
  6. Periodically send input_audio_buffer.commit
  7. Receive delta (partial) and completed (final) transcription events
"""

import asyncio
import base64
import json
import logging
import uuid
from typing import Callable

import websockets

logger = logging.getLogger(__name__)

DEFAULT_URL = "ws://localhost:9000"
DEFAULT_MODEL = "parakeet-rnnt-1.1b-unified-ml-cs-universal-multi-asr-streaming"
DEFAULT_LANGUAGE = "ja-JP"
DEFAULT_COMMIT_INTERVAL = 10  # Commit every N chunks (N * 100ms)


def _event_id() -> str:
    return f"event_{uuid.uuid4()}"


def _clean_text(text: str, language: str) -> str:
    """Clean transcription text based on language.

    For Japanese, the model outputs space-separated characters which need
    to be joined.
    """
    if language.startswith("ja"):
        return text.replace(" ", "")
    return text.strip()


class RivaWSClient:
    """Async WebSocket client for NIM Riva real-time transcription."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        language: str = DEFAULT_LANGUAGE,
        compression: str | None = "deflate",
        on_delta: Callable[[str], None] | None = None,
        on_completed: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self.url = url
        self.model = model
        self.language = language
        self.compression = compression
        self.on_delta = on_delta
        self.on_completed = on_completed
        self.on_error = on_error
        self._ws: websockets.ClientConnection | None = None

    async def connect(self) -> None:
        """Connect to NIM Riva and configure transcription session."""
        ws_url = f"{self.url.rstrip('/')}/v1/realtime?intent=transcription"
        logger.info(f"Connecting to {ws_url}")

        logger.debug(f"WebSocket compression: {self.compression}")
        self._ws = await websockets.connect(
            ws_url, compression=self.compression, open_timeout=10
        )

        # Wait for conversation.created
        init_msg = await asyncio.wait_for(self._ws.recv(), timeout=5)
        init = json.loads(init_msg)
        if init.get("type") != "conversation.created":
            raise RuntimeError(f"Unexpected init message: {init}")
        logger.info("WebSocket: conversation.created received")

        # Configure session
        await self._ws.send(
            json.dumps(
                {
                    "event_id": _event_id(),
                    "type": "transcription_session.update",
                    "session": {
                        "input_audio_format": "pcm16",
                        "input_audio_transcription": {
                            "language": self.language,
                            "model": self.model,
                        },
                    },
                }
            )
        )

        # Wait for session.updated
        update_msg = await asyncio.wait_for(self._ws.recv(), timeout=5)
        update = json.loads(update_msg)
        if update.get("type") == "error":
            raise RuntimeError(f"Session configuration error: {update}")
        logger.info(
            f"WebSocket: session configured (model={self.model}, "
            f"language={self.language})"
        )

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Send a PCM16 audio chunk to the server."""
        if not self._ws:
            return
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        await self._ws.send(
            json.dumps(
                {
                    "event_id": _event_id(),
                    "type": "input_audio_buffer.append",
                    "audio": audio_b64,
                }
            )
        )

    async def commit(self) -> None:
        """Commit the current audio buffer for transcription."""
        if not self._ws:
            return
        await self._ws.send(
            json.dumps(
                {
                    "event_id": _event_id(),
                    "type": "input_audio_buffer.commit",
                }
            )
        )

    async def recv_loop(self) -> None:
        """Receive and dispatch transcription events from the server."""
        if not self._ws:
            return

        async for msg in self._ws:
            event = json.loads(msg)
            ev_type = event.get("type", "")

            if ev_type == "conversation.item.input_audio_transcription.delta":
                delta = _clean_text(event.get("delta", ""), self.language)
                if delta:
                    if self.on_delta:
                        self.on_delta(delta)

            elif ev_type == "conversation.item.input_audio_transcription.completed":
                transcript = _clean_text(
                    event.get("transcript", ""), self.language
                )
                if self.on_completed:
                    self.on_completed(transcript)

            elif ev_type == "error":
                error_msg = json.dumps(event, ensure_ascii=False)
                logger.error(f"Server error: {error_msg}")
                if self.on_error:
                    self.on_error(error_msg)

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception as e:
                logger.debug(f"WebSocket close error (ignored): {e}")
            self._ws = None
            logger.info("WebSocket connection closed")
