"""Audio sources for real-time ASR.

Provides a common AudioSource protocol and two implementations:
- MicSource: captures live audio via sounddevice (PortAudio)
- WavReplaySource: reads audio from a WAV file

Both produce PCM16 (int16, 16kHz, mono) chunks via the same interface.
All processing (silence detection, commit logic) belongs downstream.
"""

import logging
import queue
import threading
import time
import wave
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Audio format constants (shared across the project)
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_DURATION_MS = 100
CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_DURATION_MS / 1000)  # 1600 samples
CHUNK_BYTES = CHUNK_SIZE * 2  # 3200 bytes (int16 = 2 bytes per sample)


@runtime_checkable
class AudioSource(Protocol):
    """Protocol for audio chunk producers.

    Implementations must provide PCM16 chunks (CHUNK_BYTES bytes each)
    via a blocking get_chunk() call. The source signals end-of-input
    via the exhausted property.
    """

    def start(self) -> None: ...
    def get_chunk(self, timeout: float = 0.2) -> bytes | None: ...
    def stop(self) -> None: ...
    def drain(self) -> None: ...

    @property
    def exhausted(self) -> bool: ...


class MicSource:
    """Live microphone input via sounddevice (PortAudio).

    Never exhausted — runs until explicitly stopped.
    """

    def __init__(self):
        import numpy as np
        import sounddevice as sd
        self._np = np
        self._sd = sd
        self._audio_queue: queue.Queue[bytes] = queue.Queue()
        self._stream: sd.InputStream | None = None

    @property
    def exhausted(self) -> bool:
        return False

    def start(self) -> None:
        if self._stream is not None:
            logger.warning("Recording already started")
            return

        logger.info(
            f"Starting mic source: {SAMPLE_RATE}Hz, "
            f"{CHANNELS}ch, {CHUNK_DURATION_MS}ms chunks"
        )
        self._stream = self._sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
            callback=self._audio_callback,
        )
        self._stream.start()
        logger.info("Mic source started")

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            logger.warning(f"Audio stream status: {status}")
        self._audio_queue.put(indata.copy().tobytes())

    def get_chunk(self, timeout: float = 0.2) -> bytes | None:
        try:
            return self._audio_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> None:
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
        if self._stream is None:
            return

        logger.info("Stopping mic source")
        self._stream.stop()
        self._stream.close()
        self._stream = None

        drained = 0
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        if drained:
            logger.debug(f"Drained {drained} remaining chunks")

        logger.info("Mic source stopped")


class WavReplaySource:
    """Reads audio from a WAV file, producing chunks at real-time pace.

    Validates format on start(). Becomes exhausted when all chunks
    have been consumed.

    Args:
        wav_path: Path to the WAV file (must be 16-bit PCM, mono, 16kHz).
        realtime: If True, feed chunks at real-time pace (100ms intervals).
                  If False, feed as fast as the consumer can read.
    """

    def __init__(self, wav_path: str, realtime: bool = True):
        self._wav_path = wav_path
        self._realtime = realtime
        self._audio_queue: queue.Queue[bytes] = queue.Queue()
        self._exhausted = False
        self._feed_thread: threading.Thread | None = None

    @property
    def exhausted(self) -> bool:
        return self._exhausted and self._audio_queue.empty()

    def start(self) -> None:
        logger.info(f"Starting WAV replay: {self._wav_path}")
        self._exhausted = False
        self._feed_thread = threading.Thread(
            target=self._feed_chunks, daemon=True
        )
        self._feed_thread.start()

    def _feed_chunks(self) -> None:
        try:
            with wave.open(self._wav_path, "rb") as wf:
                if wf.getsampwidth() != 2:
                    logger.error(
                        f"WAV must be 16-bit PCM, got {wf.getsampwidth()*8}-bit"
                    )
                    self._exhausted = True
                    return
                if wf.getnchannels() != 1:
                    logger.error(
                        f"WAV must be mono, got {wf.getnchannels()} channels"
                    )
                    self._exhausted = True
                    return
                if wf.getframerate() != SAMPLE_RATE:
                    logger.warning(
                        f"WAV is {wf.getframerate()}Hz, expected {SAMPLE_RATE}Hz"
                    )

                n_frames = wf.getnframes()
                duration = n_frames / wf.getframerate()
                logger.info(
                    f"WAV loaded: {duration:.1f}s, "
                    f"{n_frames} frames, "
                    f"{'real-time' if self._realtime else 'fast'} mode"
                )

                while True:
                    raw = wf.readframes(CHUNK_SIZE)
                    if not raw:
                        break
                    if len(raw) < CHUNK_BYTES:
                        raw += b"\x00" * (CHUNK_BYTES - len(raw))
                    self._audio_queue.put(raw)
                    if self._realtime:
                        time.sleep(CHUNK_DURATION_MS / 1000)

        except Exception as e:
            logger.error(f"WAV replay error: {e}")
        finally:
            self._exhausted = True
            logger.info("WAV replay finished")

    def get_chunk(self, timeout: float = 0.2) -> bytes | None:
        try:
            return self._audio_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> None:
        pass  # No stale data concern for file replay

    def stop(self) -> None:
        self._exhausted = True
        logger.info("WAV replay stopped")


# Backwards compatibility alias
StreamingRecorder = MicSource
