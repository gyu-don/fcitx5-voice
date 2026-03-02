"""WebSocket client for real-time ASR (NIM Riva or vLLM).

NIM Riva protocol:
  1. Connect to /v1/realtime?intent=transcription
  2. Receive conversation.created
  3. Send transcription_session.update with config
  4. Receive transcription_session.updated
  5. Stream audio as base64 PCM16 via input_audio_buffer.append
  6. Periodically send input_audio_buffer.commit
  7. Receive delta (partial) and completed (final) transcription events

vLLM (OpenAI-compatible) protocol:
  1. Connect to /v1/realtime
  2. Receive session.created
  3. Send session.update with model
  4. Stream audio as base64 PCM16 via input_audio_buffer.append
  5. Periodically send input_audio_buffer.commit
  6. Receive transcription.delta (partial) and transcription.done (final)
"""

import asyncio
import base64
import json
import logging
import threading
import uuid
from typing import Callable

import websockets

logger = logging.getLogger(__name__)

DEFAULT_URL = "ws://localhost:9000"
DEFAULT_MODEL = "parakeet-rnnt-1.1b-unified-ml-cs-universal-multi-asr-streaming"
DEFAULT_LANGUAGE = "ja-JP"
DEFAULT_COMMIT_INTERVAL = 10  # Commit every N chunks (N * 100ms)
DEFAULT_BACKEND = "nim"  # "nim" or "vllm"


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
    """Async WebSocket client for real-time ASR (NIM Riva or vLLM)."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        language: str = DEFAULT_LANGUAGE,
        compression: str | None = "deflate",
        backend: str = DEFAULT_BACKEND,
        on_delta: Callable[[str], None] | None = None,
        on_completed: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self.url = url
        self.model = model
        self.language = language
        self.compression = compression
        self.backend = backend
        self.on_delta = on_delta
        self.on_completed = on_completed
        self.on_error = on_error
        self._ws: websockets.ClientConnection | None = None
        # vLLM: gate commits so we wait for transcription.done before next commit
        self._commit_ready = asyncio.Event()
        self._commit_ready.set()
        # vLLM: accumulate incremental deltas (NIM sends full partial text)
        self._accumulated_text = ""

    async def connect(self) -> None:
        """Connect to ASR server and configure transcription session."""
        base = self.url.rstrip("/")
        if self.backend == "vllm":
            ws_url = f"{base}/v1/realtime"
        else:
            ws_url = f"{base}/v1/realtime?intent=transcription"
        logger.debug(f"Connecting to {ws_url} (backend={self.backend})")

        logger.debug(f"WebSocket compression: {self.compression}")
        self._ws = await websockets.connect(
            ws_url, compression=self.compression, open_timeout=10
        )

        # Wait for server init message
        init_msg = await asyncio.wait_for(self._ws.recv(), timeout=5)
        init = json.loads(init_msg)
        expected = "session.created" if self.backend == "vllm" else "conversation.created"
        if init.get("type") != expected:
            raise RuntimeError(f"Unexpected init message: {init}")
        logger.debug(f"WebSocket: {expected} received")

        if self.backend == "vllm":
            # vLLM: simple session.update with model
            await self._ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "model": self.model,
                        "session": {
                            "input_audio_transcription": {
                                "language": self.language,
                            },
                        },
                    }
                )
            )
        else:
            # NIM Riva: transcription_session.update with full config
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
                            "recognition_config": {
                                "enable_automatic_punctuation": True,
                                "enable_verbatim_transcripts": False,
                            },
                        },
                    }
                )
            )

            # NIM Riva sends a session.updated ack; vLLM does not
            update_msg = await asyncio.wait_for(self._ws.recv(), timeout=5)
            update = json.loads(update_msg)
            if update.get("type") == "error":
                raise RuntimeError(f"Session configuration error: {update}")

        logger.debug(
            f"WebSocket: session configured (model={self.model}, "
            f"language={self.language})"
        )

    def _with_event_id(self, msg: dict) -> dict:
        """Add event_id to message for NIM Riva (vLLM ignores/warns on it)."""
        if self.backend == "nim":
            msg["event_id"] = _event_id()
        return msg

    async def send_audio(self, audio_bytes: bytes) -> None:
        """Send a PCM16 audio chunk to the server."""
        if not self._ws:
            return
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        await self._ws.send(
            json.dumps(
                self._with_event_id({
                    "type": "input_audio_buffer.append",
                    "audio": audio_b64,
                })
            )
        )

    async def commit(
        self, final: bool = False, stop_event: threading.Event | None = None
    ) -> bool:
        """Commit the current audio buffer for transcription.

        For vLLM: waits until the previous generation completes before
        sending the next commit (vLLM ignores commits during generation).
        Checks stop_event periodically to allow early abort.

        Args:
            final: If True, signal no more audio will follow (vLLM).
            stop_event: Threading event to check for abort (non-final only).

        Returns:
            True if commit was sent, False if aborted.
        """
        if not self._ws:
            return False
        # vLLM: wait for previous transcription.done before committing
        if self.backend == "vllm":
            timeout = 30 if final else 10
            while not self._commit_ready.is_set():
                try:
                    await asyncio.wait_for(self._commit_ready.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    timeout -= 0.5
                    if timeout <= 0:
                        logger.warning("Timed out waiting for previous generation")
                        break
                    if stop_event and stop_event.is_set() and not final:
                        logger.debug("Commit aborted: stop requested")
                        return False
            self._commit_ready.clear()
        msg: dict = self._with_event_id({"type": "input_audio_buffer.commit"})
        if final:
            msg["final"] = True
        await self._ws.send(json.dumps(msg))
        return True

    async def recv_loop(self) -> None:
        """Receive and dispatch transcription events from the server."""
        if not self._ws:
            return

        async for msg in self._ws:
            event = json.loads(msg)
            ev_type = event.get("type", "")

            # NIM Riva: conversation.item.input_audio_transcription.delta
            # vLLM:     transcription.delta
            if ev_type in (
                "conversation.item.input_audio_transcription.delta",
                "transcription.delta",
            ):
                delta = _clean_text(event.get("delta", ""), self.language)
                if delta:
                    if self.backend == "vllm":
                        # vLLM sends incremental deltas; accumulate for preedit
                        self._accumulated_text += delta
                        if self.on_delta:
                            self.on_delta(self._accumulated_text)
                    else:
                        # NIM sends full partial text each time
                        if self.on_delta:
                            self.on_delta(delta)

            # NIM Riva: conversation.item.input_audio_transcription.completed (transcript)
            # vLLM:     transcription.done (text)
            elif ev_type in (
                "conversation.item.input_audio_transcription.completed",
                "transcription.done",
            ):
                transcript = _clean_text(
                    event.get("transcript") or event.get("text", ""), self.language
                )
                if self.on_completed:
                    self.on_completed(transcript)
                # vLLM: reset accumulated text, signal next commit can proceed
                self._accumulated_text = ""
                self._commit_ready.set()

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
            logger.debug("WebSocket connection closed")
