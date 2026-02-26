"""Streaming audio recorder for real-time ASR.

Captures PCM16 audio via sounddevice and provides chunks via a thread-safe queue.
No silence detection - audio is streamed continuously to the ASR server.
"""

import logging
import queue
import threading

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_DURATION_MS = 100
CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_DURATION_MS / 1000)  # 1600 samples per chunk


class StreamingRecorder:
    """Streams PCM16 audio chunks via a thread-safe queue."""

    def __init__(self):
        self._audio_queue: queue.Queue[bytes] = queue.Queue()
        self._stream: sd.InputStream | None = None

    def start(self) -> None:
        """Start audio capture."""
        if self._stream is not None:
            logger.warning("Recording already started")
            return

        logger.info(
            f"Starting streaming recorder: {SAMPLE_RATE}Hz, "
            f"{CHANNELS}ch, {CHUNK_DURATION_MS}ms chunks"
        )
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
            callback=self._audio_callback,
        )
        self._stream.start()
        logger.info("Streaming recorder started")

    def _audio_callback(
        self, indata: np.ndarray, frames: int, time_info, status
    ) -> None:
        """sounddevice callback - runs in audio thread."""
        if status:
            logger.warning(f"Audio stream status: {status}")
        self._audio_queue.put(indata.copy().tobytes())

    def get_chunk(self, timeout: float = 0.2) -> bytes | None:
        """Get next audio chunk (blocking with timeout).

        Returns None on timeout (no audio available).
        """
        try:
            return self._audio_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> None:
        """Discard all queued audio chunks.

        Used during WebSocket reconnection to drop stale audio data
        that accumulated while disconnected.
        """
        drained = 0
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        if drained:
            logger.debug(f"Drained {drained} stale chunks from queue")

    def stop(self) -> None:
        """Stop audio capture and drain the queue."""
        if self._stream is None:
            return

        logger.info("Stopping streaming recorder")
        self._stream.stop()
        self._stream.close()
        self._stream = None

        # Drain remaining chunks
        drained = 0
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        if drained:
            logger.debug(f"Drained {drained} remaining chunks")

        logger.info("Streaming recorder stopped")
