#!/usr/bin/env python3
"""Generate and cache TTS fixture WAV files for ASR testing.

Uses edge-tts (Microsoft Edge TTS) to synthesize Japanese speech,
then layers silence/noise using numpy. Fixtures are cached in
tools/fixtures/ (gitignored) and only regenerated when missing or
when --force is specified.

Requirements:
    pip install edge-tts
    sudo pacman -S ffmpeg   # for MP3→WAV conversion

Usage:
    uv run python tools/generate_fixtures.py          # generate missing
    uv run python tools/generate_fixtures.py --force  # regenerate all
    uv run python tools/generate_fixtures.py --list   # show status
    uv run python tools/generate_fixtures.py short_phrase noisy
"""

import argparse
import asyncio
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

TOOLS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TOOLS_DIR / "fixtures"

SAMPLE_RATE = 16000
TTS_VOICE = "ja-JP-NanamiNeural"


# ---------------------------------------------------------------------------
# Fixture definitions
# ---------------------------------------------------------------------------

@dataclass
class FixtureSpec:
    name: str
    description: str
    texts: list[str]
    pre_silence_s: float = 0.5
    inter_silence_s: float = 0.4
    post_silence_s: float = 1.0
    noise_amplitude: int = 0      # 0 = pure silence; >0 = background noise


FIXTURES: list[FixtureSpec] = [
    FixtureSpec(
        name="short_phrase",
        description="Short single phrase (~4s total)",
        texts=["これはテストです。"],
        pre_silence_s=1.2,  # must cover calibration (10 chunks = 1.0s)
        post_silence_s=1.0,
    ),
    FixtureSpec(
        name="multi_phrase",
        description="2 phrases with pause, for pipeline/E2E tests",
        texts=[
            "音声認識のテストを行います。",
            "認識結果が表示されます。",
        ],
        pre_silence_s=1.2,
        inter_silence_s=0.8,
        post_silence_s=1.0,
    ),
    FixtureSpec(
        name="long_speech",
        description="Long continuous speech (~15s total)",
        texts=[
            "音声認識のテストです。",
            "日本語をリアルタイムでテキストに変換します。",
            "複数の文章で認識精度を検証します。",
        ],
        pre_silence_s=1.2,
        inter_silence_s=0.5,
        post_silence_s=1.5,
    ),
    FixtureSpec(
        name="noisy",
        description="Speech with background noise during silence",
        texts=["ノイズ環境でのテストです。正しく認識できますか。"],
        pre_silence_s=1.2,
        post_silence_s=1.5,
        noise_amplitude=300,
    ),
]


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def _make_silence(duration_s: float, noise_amplitude: int = 0) -> np.ndarray:
    n = int(duration_s * SAMPLE_RATE)
    if noise_amplitude > 0:
        return np.random.randint(
            -noise_amplitude, noise_amplitude + 1, size=n, dtype=np.int16
        )
    return np.zeros(n, dtype=np.int16)


def _trim_silence(samples: np.ndarray, threshold: int = 200) -> np.ndarray:
    """Trim leading and trailing silence from audio samples."""
    abs_samples = np.abs(samples)
    # Find first and last sample above threshold
    above = np.where(abs_samples > threshold)[0]
    if len(above) == 0:
        return samples
    return samples[above[0]:above[-1] + 1]


async def _tts_to_array(text: str) -> np.ndarray:
    """Synthesize text via edge-tts; return 16kHz mono int16 array."""
    try:
        import edge_tts
    except ImportError:
        raise SystemExit("edge-tts not installed. Run: pip install edge-tts")

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        mp3_path = Path(f.name)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = Path(f.name)

    try:
        communicate = edge_tts.Communicate(text, TTS_VOICE)
        await communicate.save(str(mp3_path))

        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", str(mp3_path),
                    "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le",
                    str(wav_path),
                ],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError:
            raise SystemExit("ffmpeg not found. Install: sudo pacman -S ffmpeg")
        except subprocess.CalledProcessError as e:
            raise SystemExit(f"ffmpeg failed: {e.stderr.decode().strip()}")

        with wave.open(str(wav_path), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        return _trim_silence(np.frombuffer(raw, dtype=np.int16).copy())
    finally:
        mp3_path.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)


def _write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes())


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

async def generate_fixture(spec: FixtureSpec, verbose: bool = False) -> None:
    out_path = FIXTURES_DIR / f"{spec.name}.wav"
    parts: list[np.ndarray] = []

    parts.append(_make_silence(spec.pre_silence_s, spec.noise_amplitude))

    for i, text in enumerate(spec.texts):
        if i > 0:
            parts.append(_make_silence(spec.inter_silence_s, spec.noise_amplitude))
        if verbose:
            print(f"    [{i+1}/{len(spec.texts)}] {text!r}")
        parts.append(await _tts_to_array(text))

    parts.append(_make_silence(spec.post_silence_s, spec.noise_amplitude))

    full = np.concatenate(parts)
    _write_wav(out_path, full)

    duration_s = len(full) / SAMPLE_RATE
    size_kb = out_path.stat().st_size / 1024
    print(f"  → {out_path.name}  {duration_s:.1f}s  {size_kb:.0f}KB")


async def main_async(args: argparse.Namespace) -> None:
    FIXTURES_DIR.mkdir(exist_ok=True)

    if args.list:
        print(f"Fixtures dir: {FIXTURES_DIR}")
        for spec in FIXTURES:
            out_path = FIXTURES_DIR / f"{spec.name}.wav"
            if out_path.exists():
                size_kb = out_path.stat().st_size / 1024
                status = f"✓  {size_kb:.0f}KB"
            else:
                status = "✗  (missing)"
            print(f"  {status:12s}  {spec.name:20s}  {spec.description}")
        return

    to_generate = FIXTURES
    if args.name:
        names = set(args.name)
        to_generate = [s for s in FIXTURES if s.name in names]
        unknown = names - {s.name for s in FIXTURES}
        if unknown:
            print(f"Unknown fixture(s): {', '.join(sorted(unknown))}")
            print(f"Available: {', '.join(s.name for s in FIXTURES)}")
            return

    generated = 0
    for spec in to_generate:
        out_path = FIXTURES_DIR / f"{spec.name}.wav"
        if out_path.exists() and not args.force:
            print(f"[skip] {spec.name}  (use --force to regenerate)")
            continue
        print(f"[gen]  {spec.name}: {spec.description}")
        await generate_fixture(spec, verbose=args.verbose)
        generated += 1

    if generated == 0:
        print("All fixtures already exist. Use --force to regenerate.")
    else:
        print(f"\nDone. {generated} fixture(s) written to {FIXTURES_DIR}/")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate TTS fixture WAV files for ASR testing.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--force", "-f", action="store_true", help="Regenerate existing fixtures"
    )
    parser.add_argument(
        "--list", "-l", action="store_true", help="List fixtures and their status"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show each TTS segment"
    )
    parser.add_argument(
        "name", nargs="*", help="Fixture name(s) to generate (default: all)"
    )
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
