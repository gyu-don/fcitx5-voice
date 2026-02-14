import atexit
import logging
import signal
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from scipy.io.wavfile import write

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

model_size = "large-v3-turbo"
sample_rate = 16000
silence_threshold = 0.01  # Amplitude threshold for silence detection
silence_duration = 1.0  # Seconds of silence before splitting
max_duration = 15.0  # Maximum duration per segment in seconds

temp_dir: Path | None = None


def cleanup() -> None:
    """Clean up temporary directory on exit."""
    global temp_dir
    if temp_dir and temp_dir.exists():
        file_count = len(list(temp_dir.glob("*.wav")))
        for file in temp_dir.glob("*.wav"):
            file.unlink()
        temp_dir.rmdir()
        logging.info(f"Cleaned up temporary directory: {temp_dir} ({file_count} files)")


def signal_handler(sig, frame) -> None:
    """Handle interrupt signal."""
    logging.info("Interrupted by user")
    cleanup()
    sys.exit(0)


class RealtimeRecorder:
    """Real-time audio recorder with silence detection."""

    def __init__(self, model: WhisperModel):
        self.model = model
        self.audio_buffer: list[np.ndarray] = []
        self.silence_frames = 0
        self.total_frames = 0
        self.frames_per_second = sample_rate
        self.silence_frame_threshold = int(silence_duration * sample_rate)
        self.max_frames = int(max_duration * sample_rate)
        self.segment_count = 0
        self.lock = threading.Lock()
        self.has_speech = False
        self.current_segment_start = False

    def audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Callback for audio stream."""
        if status:
            logging.warning(f"Audio stream status: {status}")

        # Calculate RMS (Root Mean Square) to detect silence
        audio_data = indata.copy().flatten()
        rms = np.sqrt(np.mean(audio_data**2))

        with self.lock:
            is_silence = rms < silence_threshold

            if is_silence:
                self.silence_frames += frames
            else:
                # Speech detected
                if not self.current_segment_start:
                    # Start of new segment
                    self.current_segment_start = True
                    logging.info(f"Segment #{self.segment_count + 1}: Recording started")

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
                reason = f"silence detected ({self.silence_frames / sample_rate:.1f}s)"
            elif self.total_frames >= self.max_frames:
                # Maximum duration reached
                should_save = True
                reason = f"max duration reached ({self.total_frames / sample_rate:.1f}s)"

            if should_save and len(self.audio_buffer) > 0:
                # Save and transcribe the buffer
                audio_array = np.concatenate(self.audio_buffer)
                self.save_and_transcribe(audio_array, reason)

                # Reset buffer
                self.audio_buffer = []
                self.total_frames = 0
                self.has_speech = False
                self.silence_frames = 0
                self.current_segment_start = False

    def save_and_transcribe(self, audio_data: np.ndarray, reason: str) -> None:
        """Save audio data to file and transcribe it."""
        global temp_dir

        if temp_dir is None:
            temp_dir = Path(tempfile.mkdtemp(prefix="fcitx5_voice_"))
            logging.info(f"Created temporary directory: {temp_dir}")

        self.segment_count += 1
        segment_num = self.segment_count
        audio_file = temp_dir / f"segment_{segment_num:04d}.wav"

        # Convert to int16
        audio_int16 = (audio_data * 32767).astype(np.int16)
        write(audio_file, sample_rate, audio_int16)

        duration = len(audio_data) / sample_rate
        logging.info(
            f"Segment #{segment_num}: Recording ended ({reason}) - "
            f"duration: {duration:.2f}s, file: {audio_file.name}"
        )

        # Transcribe in a separate thread to avoid blocking recording
        threading.Thread(
            target=self.transcribe_audio,
            args=(audio_file, segment_num),
            daemon=True,
        ).start()

    def transcribe_audio(self, audio_file: Path, segment_num: int) -> None:
        """Transcribe audio file using faster-whisper."""
        try:
            logging.info(f"Segment #{segment_num}: Transcription started")
            segments, info = self.model.transcribe(str(audio_file), beam_size=2)

            # Collect all transcription text
            transcription_parts = []
            for segment in segments:
                transcription_parts.append(segment.text.strip())

            transcription_text = " ".join(transcription_parts)

            logging.info(
                f"Segment #{segment_num}: Transcription completed - "
                f"language: {info.language} ({info.language_probability:.2f})"
            )
            if transcription_text:
                logging.info(f"Segment #{segment_num}: Text: {transcription_text}")
            else:
                logging.warning(f"Segment #{segment_num}: No transcription result")

        except Exception as e:
            logging.error(f"Segment #{segment_num}: Transcription error: {e}")

    def start_recording(self) -> None:
        """Start real-time recording with silence detection."""
        logging.info("Starting real-time voice input (Ctrl+C to stop)")
        logging.info(f"Configuration: silence_threshold={silence_threshold}, "
                    f"silence_duration={silence_duration}s, "
                    f"max_segment_duration={max_duration}s")
        logging.info("Speak into your microphone...")

        with sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            callback=self.audio_callback,
            blocksize=int(sample_rate * 0.1),  # 100ms blocks
        ):
            # Keep the stream open until interrupted
            try:
                while True:
                    sd.sleep(1000)
            except KeyboardInterrupt:
                # Save any remaining audio in buffer
                with self.lock:
                    if len(self.audio_buffer) > 0:
                        logging.info("Saving remaining audio buffer...")
                        audio_array = np.concatenate(self.audio_buffer)
                        self.save_and_transcribe(audio_array, "interrupted")


def record_audio(duration: float) -> Path:
    """Record audio from microphone and save to temporary file."""
    global temp_dir

    if temp_dir is None:
        temp_dir = Path(tempfile.mkdtemp(prefix="fcitx5_voice_"))
        print(f"Created temporary directory: {temp_dir}")

    print(f"Recording for {duration} seconds...")
    recording = sd.rec(
        int(duration * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
    )
    sd.wait()
    print("Recording finished")

    audio_file = temp_dir / f"recording_{len(list(temp_dir.glob('*.wav')))}.wav"
    write(audio_file, sample_rate, recording)
    print(f"Saved to: {audio_file}")

    return audio_file


def main() -> None:
    global temp_dir

    # Register cleanup handlers
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, signal_handler)

    # Initialize model
    logging.info(f"Loading Whisper model: {model_size}")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    logging.info("Model loaded successfully")

    # Start real-time recording with silence detection
    recorder = RealtimeRecorder(model)
    try:
        recorder.start_recording()
    except KeyboardInterrupt:
        logging.info("Stopping voice input...")
    finally:
        cleanup()
        sys.exit(0)


if __name__ == "__main__":
    main()
