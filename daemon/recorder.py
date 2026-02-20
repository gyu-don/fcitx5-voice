"""Audio recording with silence detection."""
import logging
import threading
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

# Recording parameters
SAMPLE_RATE = 16000
SILENCE_THRESHOLD = 0.01  # Amplitude threshold for silence detection
SILENCE_DURATION = 1.0  # Seconds of silence before splitting
MAX_DURATION = 15.0  # Maximum duration per segment in seconds


class RealtimeRecorder:
    """Real-time audio recorder with silence detection."""

    def __init__(self, on_segment: Callable[[np.ndarray, int], None]):
        """
        Initialize recorder.

        Args:
            on_segment: Callback function called when audio segment is ready.
                        Receives (audio_data: np.ndarray, segment_num: int)
        """
        self.on_segment = on_segment
        self.audio_buffer: list[np.ndarray] = []
        self.silence_frames = 0
        self.total_frames = 0
        self.silence_frame_threshold = int(SILENCE_DURATION * SAMPLE_RATE)
        self.max_frames = int(MAX_DURATION * SAMPLE_RATE)
        self.segment_count = 0
        self.lock = threading.Lock()
        self.has_speech = False
        self.current_segment_start = False
        self.stream = None

    def audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Callback for audio stream."""
        if status:
            logger.warning(f"Audio stream status: {status}")

        # Calculate RMS (Root Mean Square) to detect silence
        audio_data = indata.copy().flatten()
        rms = np.sqrt(np.mean(audio_data**2))

        with self.lock:
            is_silence = rms < SILENCE_THRESHOLD

            if is_silence:
                self.silence_frames += frames
            else:
                # Speech detected
                if not self.current_segment_start:
                    # Start of new segment
                    self.current_segment_start = True
                    logger.info(f"Segment #{self.segment_count + 1}: Recording started")

                self.has_speech = True
                self.silence_frames = 0
                self.audio_buffer.append(audio_data)
                self.total_frames += frames

            # Check if we should save the segment
            should_save = False
            reason = ""

            if self.has_speech and self.silence_frames >= self.silence_frame_threshold:
                # Silence detected after speech
                should_save = True
                reason = f"silence detected ({self.silence_frames / SAMPLE_RATE:.1f}s)"
            elif self.total_frames >= self.max_frames:
                # Maximum duration reached
                should_save = True
                reason = f"max duration reached ({self.total_frames / SAMPLE_RATE:.1f}s)"

            if should_save and len(self.audio_buffer) > 0:
                # Process the buffer
                audio_array = np.concatenate(self.audio_buffer)
                self.segment_count += 1
                segment_num = self.segment_count
                duration = len(audio_array) / SAMPLE_RATE

                logger.info(
                    f"Segment #{segment_num}: Recording ended ({reason}) - "
                    f"duration: {duration:.2f}s"
                )

                # Call the callback in a separate thread to avoid blocking recording
                threading.Thread(
                    target=self.on_segment,
                    args=(audio_array, segment_num),
                    daemon=True,
                ).start()

                # Reset buffer
                self.audio_buffer = []
                self.total_frames = 0
                self.has_speech = False
                self.silence_frames = 0
                self.current_segment_start = False

    def start(self) -> None:
        """Start recording."""
        if self.stream is not None:
            logger.warning("Recording already started")
            return

        logger.info("Starting real-time voice input")
        logger.info(
            f"Configuration: silence_threshold={SILENCE_THRESHOLD}, "
            f"silence_duration={SILENCE_DURATION}s, "
            f"max_segment_duration={MAX_DURATION}s"
        )

        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self.audio_callback,
            blocksize=int(SAMPLE_RATE * 0.1),  # 100ms blocks
        )
        self.stream.start()
        logger.info("Recording started successfully")

    def stop(self) -> None:
        """Stop recording and process any remaining audio."""
        if self.stream is None:
            logger.warning("Recording not started")
            return

        logger.info("Stopping voice input")
        self.stream.stop()
        self.stream.close()
        self.stream = None

        # Save any remaining audio in buffer
        with self.lock:
            if len(self.audio_buffer) > 0:
                logger.info("Saving remaining audio buffer...")
                audio_array = np.concatenate(self.audio_buffer)
                self.segment_count += 1
                segment_num = self.segment_count

                # Call in separate thread to avoid blocking the D-Bus main loop
                threading.Thread(
                    target=self.on_segment,
                    args=(audio_array, segment_num),
                    daemon=True,
                ).start()

                # Reset buffer
                self.audio_buffer = []
                self.total_frames = 0
                self.has_speech = False
                self.silence_frames = 0

        logger.info("Recording stopped")
