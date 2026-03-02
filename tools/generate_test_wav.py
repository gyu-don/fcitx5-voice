#!/usr/bin/env python3
"""Generate test WAV files for testing voice recognition pipelines.

This script uses stdlib only (wave, struct, math) - no external dependencies.

Audio format matches daemon/recorder.py exactly:
  - SAMPLE_RATE = 16000 Hz   (recorder.py: SAMPLE_RATE = 16000)
  - CHANNELS    = 1 (mono)   (recorder.py: CHANNELS = 1)
  - dtype       = int16      (recorder.py: dtype="int16")
  - CHUNK_SIZE  = 1600       (recorder.py: CHUNK_SIZE = int(16000 * 100 / 1000))

"Speech" segments use a 440 Hz sine wave at amplitude ~8000 (RMS ~5657).
"Silence" segments use very low random noise at amplitude ~50 (RMS ~30).

The NIM Riva ASR server receives continuous raw PCM16 regardless of signal
level - there is no client-side silence detection in the current recorder.
These values are chosen to produce clearly audible vs. clearly quiet segments
that would be easy to validate in any downstream silence-gating middleware.
"""

import argparse
import math
import os
import random
import struct
import wave


# ---------------------------------------------------------------------------
# Constants mirrored from daemon/recorder.py
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000   # Hz - matches recorder.py SAMPLE_RATE
CHANNELS = 1          # mono - matches recorder.py CHANNELS
SAMPLE_WIDTH = 2      # bytes (int16) - matches recorder.py dtype="int16"
CHUNK_SIZE = 1600     # samples per 100 ms chunk - matches recorder.py CHUNK_SIZE

# ---------------------------------------------------------------------------
# Signal level constants
#
# "Speech" amplitude: 8000 out of a possible 32767 (int16 max).
#   RMS for a full-cycle sine = amplitude / sqrt(2) ≈ 8000 / 1.414 ≈ 5657.
#   This is well above any reasonable silence threshold a middleware might
#   apply (e.g. RMS > 300 would detect this as speech).
#
# "Silence" amplitude: ~50 peak (random noise).
#   RMS ≈ 50 / sqrt(3) ≈ 29 for uniform noise.
#   Clearly below any typical silence gate (RMS < 100).
# ---------------------------------------------------------------------------

SPEECH_AMPLITUDE = 8000    # peak amplitude for sine "speech" segments
SPEECH_FREQUENCY = 440     # Hz, standard concert A - easy to verify by ear
SILENCE_AMPLITUDE = 50     # peak amplitude for noise "silence" segments


# ---------------------------------------------------------------------------
# Sample generators
# ---------------------------------------------------------------------------

def generate_sine_samples(num_samples: int, frequency: float,
                           amplitude: int, sample_rate: int) -> list[int]:
    """Generate a sine wave at the given frequency and amplitude.

    Returns a list of int16 PCM samples (already clamped to [-32768, 32767]).

    Args:
        num_samples:  Total number of samples to produce.
        frequency:    Tone frequency in Hz (440 Hz = concert A).
        amplitude:    Peak amplitude in int16 units (max 32767).
        sample_rate:  Samples per second (must match the WAV header).
    """
    samples = []
    for i in range(num_samples):
        # sin() returns [-1.0, 1.0]; multiply by amplitude and round to int.
        value = int(amplitude * math.sin(2.0 * math.pi * frequency * i / sample_rate))
        # Clamp defensively - amplitude=8000 is far from int16 overflow, but
        # explicit clamping makes the contract clear.
        value = max(-32768, min(32767, value))
        samples.append(value)
    return samples


def generate_noise_samples(num_samples: int, amplitude: int) -> list[int]:
    """Generate low-level uniform random noise to simulate silence.

    Uses random.randint for reproducibility without needing numpy.
    The peak amplitude is ``amplitude``, giving an RMS of roughly
    ``amplitude / sqrt(3)`` for uniformly distributed noise.

    Args:
        num_samples:  Total number of samples to produce.
        amplitude:    Peak absolute amplitude in int16 units.
    """
    samples = []
    for _ in range(num_samples):
        value = random.randint(-amplitude, amplitude)
        samples.append(value)
    return samples


# ---------------------------------------------------------------------------
# WAV writing helper
# ---------------------------------------------------------------------------

def write_wav(filepath: str, samples: list[int], sample_rate: int) -> int:
    """Write a list of int16 PCM samples to a WAV file.

    Returns the number of bytes written to disk.

    Args:
        filepath:     Output file path.
        samples:      Flat list of int16 PCM sample values.
        sample_rate:  Sample rate to embed in the WAV header.
    """
    with wave.open(filepath, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)   # 2 bytes = int16
        wf.setframerate(sample_rate)
        # struct.pack with "<h" gives little-endian int16, which is what
        # the wave module expects for PCM16 and what sounddevice produces.
        raw = struct.pack(f"<{len(samples)}h", *samples)
        wf.writeframes(raw)
    return os.path.getsize(filepath)


# ---------------------------------------------------------------------------
# Pattern builders
# ---------------------------------------------------------------------------

def build_speech_silence_pattern(sample_rate: int) -> tuple[list[int], str]:
    """Build the default speech-silence pattern.

    Segments (all durations chosen to exercise the recorder pipeline):
      1.  1.0 s  silence   - calibration period; the daemon's recorder
                             sends the first ~10 chunks (CALIBRATION_CHUNKS=10
                             at 100 ms each = 1 s) before NIM Riva starts
                             meaningful decoding.
      2.  2.0 s  speech    - first utterance (enough for NIM Riva to produce
                             several delta events).
      3.  0.5 s  silence   - short pause between utterances.
      4.  1.0 s  speech    - second utterance.
      5.  1.0 s  silence   - trailing silence; tests that the pipeline
                             flushes/commits after audio stops.

    Returns:
        A (samples, description) tuple.
    """
    segments = [
        # (duration_s, generator_fn)
        (1.0,  lambda n: generate_noise_samples(n, SILENCE_AMPLITUDE)),   # calibration silence
        (2.0,  lambda n: generate_sine_samples(n, SPEECH_FREQUENCY, SPEECH_AMPLITUDE, sample_rate)),
        (0.5,  lambda n: generate_noise_samples(n, SILENCE_AMPLITUDE)),   # inter-utterance pause
        (1.0,  lambda n: generate_sine_samples(n, SPEECH_FREQUENCY, SPEECH_AMPLITUDE, sample_rate)),
        (1.0,  lambda n: generate_noise_samples(n, SILENCE_AMPLITUDE)),   # trailing silence
    ]

    all_samples: list[int] = []
    for duration_s, gen in segments:
        n = int(duration_s * sample_rate)
        all_samples.extend(gen(n))

    description = (
        "1s silence (calibration) + 2s speech + 0.5s silence + "
        "1s speech + 1s silence"
    )
    return all_samples, description


def build_continuous_pattern(sample_rate: int,
                              duration_s: float) -> tuple[list[int], str]:
    """Build a continuous all-speech pattern with no silence gaps.

    Useful for testing sustained transcription without silence gating.

    Args:
        sample_rate:  Samples per second.
        duration_s:   Total duration of the generated audio.

    Returns:
        A (samples, description) tuple.
    """
    n = int(duration_s * sample_rate)
    samples = generate_sine_samples(n, SPEECH_FREQUENCY, SPEECH_AMPLITUDE, sample_rate)
    description = f"{duration_s:.1f}s continuous speech (sine wave, no silence)"
    return samples, description


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate test WAV files for the fcitx5-voice pipeline. "
            "Output is PCM16, 16 kHz, mono - matching daemon/recorder.py."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-o", "--output",
        default="test_speech.wav",
        metavar="FILE",
        help="Output WAV filename.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Override pattern with uniform speech of this many seconds. "
            "Implies --pattern continuous."
        ),
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=SAMPLE_RATE,
        metavar="HZ",
        help=(
            "Sample rate in Hz. Default matches recorder.py SAMPLE_RATE=16000. "
            "Change only if testing non-standard pipeline configurations."
        ),
    )
    parser.add_argument(
        "--pattern",
        choices=["speech-silence", "continuous"],
        default="speech-silence",
        help=(
            "'speech-silence': 1s silence + 2s speech + 0.5s silence + "
            "1s speech + 1s silence (default, tests full pipeline). "
            "'continuous': all speech, no silence (tests sustained transcription)."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # --duration implies continuous pattern
    if args.duration is not None:
        pattern = "continuous"
        duration_s = args.duration
    else:
        pattern = args.pattern
        # Default duration for continuous when --pattern continuous is given
        # without --duration (5.5 s matches the speech-silence total)
        duration_s = 5.5

    # Build sample data
    if pattern == "continuous":
        samples, description = build_continuous_pattern(args.sample_rate, duration_s)
    else:
        samples, description = build_speech_silence_pattern(args.sample_rate)

    # Write WAV
    file_size = write_wav(args.output, samples, args.sample_rate)
    total_duration = len(samples) / args.sample_rate

    # Summary
    print(f"Generated: {args.output}")
    print(f"  Duration   : {total_duration:.2f} s")
    print(f"  File size  : {file_size:,} bytes ({file_size / 1024:.1f} KB)")
    print(f"  Format     : PCM16, {args.sample_rate} Hz, mono")
    print(f"  Samples    : {len(samples):,}")
    print(f"  Pattern    : {description}")
    print(f"  Speech RMS : ~{int(SPEECH_AMPLITUDE / math.sqrt(2))} (sine at {SPEECH_FREQUENCY} Hz, amplitude {SPEECH_AMPLITUDE})")
    print(f"  Silence RMS: ~{int(SILENCE_AMPLITUDE / math.sqrt(3))} (random noise, amplitude {SILENCE_AMPLITUDE})")


if __name__ == "__main__":
    main()
