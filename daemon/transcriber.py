"""Whisper model wrapper for audio transcription."""
import logging
import tempfile
import threading
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel
from scipy.io.wavfile import write

logger = logging.getLogger(__name__)

# Model configuration
MODEL_SIZE = "small"  # Options: tiny (fastest) < base < small < medium
SAMPLE_RATE = 16000


class Transcriber:
    """Whisper model wrapper for transcribing audio."""

    def __init__(self):
        """Initialize transcriber (model is loaded lazily on first use)."""
        self.model = None
        self._model_lock = threading.Lock()
        self.temp_dir = Path(tempfile.mkdtemp(prefix="fcitx5_voice_"))
        logger.info(f"Created temporary directory: {self.temp_dir}")

    def _ensure_model(self):
        """Load Whisper model if not already loaded (thread-safe)."""
        if self.model is None:
            with self._model_lock:
                if self.model is None:
                    logger.info(f"Loading Whisper model: {MODEL_SIZE}")
                    self.model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
                    logger.info("Model loaded successfully")

    def transcribe(self, audio_data: np.ndarray) -> str:
        """
        Transcribe audio data to text.

        Args:
            audio_data: Audio samples as float32 numpy array

        Returns:
            Transcribed text string
        """
        self._ensure_model()

        # Save audio to temporary file
        audio_file = self.temp_dir / f"segment_{id(audio_data)}.wav"
        audio_int16 = (audio_data * 32767).astype(np.int16)
        write(audio_file, SAMPLE_RATE, audio_int16)

        try:
            logger.info(f"Transcription started for {audio_file.name}")
            # Optimized for speed: beam_size=1, VAD filter enabled
            segments, info = self.model.transcribe(
                str(audio_file),
                beam_size=1,  # Faster inference (was 2)
                vad_filter=True,  # Skip silence
                vad_parameters=dict(min_silence_duration_ms=500)
            )

            # Collect all transcription text
            transcription_parts = []
            for segment in segments:
                transcription_parts.append(segment.text.strip())

            transcription_text = " ".join(transcription_parts)

            logger.info(
                f"Transcription completed - "
                f"language: {info.language} ({info.language_probability:.2f})"
            )

            if transcription_text:
                logger.info(f"Text: {transcription_text}")
            else:
                logger.warning("No transcription result")

            return transcription_text

        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return ""
        finally:
            # Clean up temporary file
            try:
                audio_file.unlink()
            except Exception as e:
                logger.warning(f"Failed to delete {audio_file}: {e}")

    def cleanup(self) -> None:
        """Clean up temporary directory."""
        if self.temp_dir and self.temp_dir.exists():
            file_count = len(list(self.temp_dir.glob("*.wav")))
            for file in self.temp_dir.glob("*.wav"):
                file.unlink()
            self.temp_dir.rmdir()
            logger.info(f"Cleaned up temporary directory: {self.temp_dir} ({file_count} files)")
