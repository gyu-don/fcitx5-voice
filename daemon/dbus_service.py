"""D-Bus service for fcitx5-voice daemon with real-time streaming ASR.

Bridges between D-Bus (GLib main loop) and async WebSocket streaming
(asyncio in a separate thread).
"""

import asyncio
import logging
import threading

from gi.repository import GLib
from pydbus import SessionBus
from pydbus.generic import signal

from .recorder import StreamingRecorder
from .ws_client import RivaWSClient

logger = logging.getLogger(__name__)

# D-Bus interface XML definition
DBUS_INTERFACE = """
<node>
  <interface name='org.fcitx.Fcitx5.Voice'>
    <method name='StartRecording'>
    </method>
    <method name='StopRecording'>
    </method>
    <method name='GetStatus'>
      <arg type='s' name='status' direction='out'/>
    </method>
    <signal name='TranscriptionComplete'>
      <arg type='s' name='text'/>
      <arg type='i' name='segment_num'/>
    </signal>
    <signal name='TranscriptionDelta'>
      <arg type='s' name='text'/>
    </signal>
    <signal name='RecordingStarted'>
    </signal>
    <signal name='RecordingStopped'>
    </signal>
    <signal name='Error'>
      <arg type='s' name='message'/>
    </signal>
  </interface>
</node>
"""


class VoiceDaemonService:
    """D-Bus service for voice input daemon with real-time streaming."""

    dbus = DBUS_INTERFACE

    def __init__(
        self,
        ws_url: str,
        model: str,
        language: str,
        compression: str | None = "deflate",
    ):
        logger.info("Initializing voice daemon service (streaming mode)")
        self.ws_url = ws_url
        self.model = model
        self.language = language
        self.compression = compression
        self.recording = False
        self._stop_event: threading.Event | None = None
        self._stream_thread: threading.Thread | None = None
        logger.debug(
            f"Config: url={ws_url}, model={model}, "
            f"language={language}, compression={compression}"
        )

    def StartRecording(self):
        """Start streaming audio to ASR server (D-Bus method)."""
        if self.recording:
            logger.warning("Already recording")
            return

        # Wait briefly for previous streaming thread to finish cleanup
        if self._stream_thread and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=2)
            if self._stream_thread.is_alive():
                logger.error("Previous streaming thread still running")
                self.Error("前回の録音セッションがまだ終了していません")
                return

        logger.debug("D-Bus: StartRecording called")
        self.recording = True
        self.RecordingStarted()
        self._start_streaming()

    def StopRecording(self):
        """Stop streaming audio (D-Bus method).

        Signals the streaming thread to stop and returns immediately.
        The thread handles final commit, waits for server responses,
        and cleans up on its own.
        """
        if not self.recording:
            logger.warning("Not recording")
            return

        logger.debug("D-Bus: StopRecording called")
        self.recording = False
        if self._stop_event:
            self._stop_event.set()
        self.RecordingStopped()

    def GetStatus(self) -> str:
        """Get current status (D-Bus method)."""
        status = "recording" if self.recording else "idle"
        logger.debug(f"D-Bus: GetStatus -> {status}")
        return status

    # D-Bus signals
    TranscriptionComplete = signal()
    TranscriptionDelta = signal()
    RecordingStarted = signal()
    RecordingStopped = signal()
    Error = signal()

    def _start_streaming(self):
        """Start the async streaming thread."""
        self._stop_event = threading.Event()
        self._stream_thread = threading.Thread(
            target=self._run_stream_loop, daemon=True
        )
        self._stream_thread.start()

    def _stop_streaming(self):
        """Signal the streaming thread to stop and wait for it."""
        if self._stop_event:
            self._stop_event.set()
        if self._stream_thread:
            self._stream_thread.join(timeout=15)
            if self._stream_thread.is_alive():
                logger.warning("Streaming thread did not stop in time")
            self._stream_thread = None

    def _run_stream_loop(self):
        """Run the asyncio event loop for streaming (in a separate thread)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._stream())
        except Exception as e:
            logger.error(f"Streaming error: {e}", exc_info=True)
            GLib.idle_add(self._emit_error, str(e))
        finally:
            loop.close()

    async def _stream(self):
        """Main streaming coroutine with automatic reconnection.

        The recorder runs continuously outside the reconnection loop.
        On connection failure, stale audio is drained and reconnection
        is attempted with exponential backoff.
        """
        recorder = StreamingRecorder()
        recorder.start()

        try:
            backoff = 1.0
            while not self._stop_event.is_set():
                client = RivaWSClient(
                    url=self.ws_url,
                    model=self.model,
                    language=self.language,
                    compression=self.compression,
                    on_delta=lambda text: GLib.idle_add(
                        self._emit_delta, text
                    ),
                    on_completed=lambda text: GLib.idle_add(
                        self._emit_completed, text
                    ),
                    on_error=lambda msg: GLib.idle_add(
                        self._emit_error, msg
                    ),
                )
                try:
                    await client.connect()
                    backoff = 1.0  # Reset on successful connection
                    recorder.drain()  # Discard stale audio from reconnect gap

                    send_task = asyncio.create_task(
                        self._send_audio_loop(client, recorder)
                    )
                    recv_task = asyncio.create_task(client.recv_loop())

                    done_tasks, pending = await asyncio.wait(
                        [send_task, recv_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # Check for exceptions in completed tasks
                    for task in done_tasks:
                        if task.exception():
                            raise task.exception()

                    # send_task completed (stop requested):
                    # wait briefly for recv_task to get final events
                    if send_task in done_tasks and recv_task in pending:
                        try:
                            await asyncio.wait_for(recv_task, timeout=3)
                        except (asyncio.TimeoutError, Exception):
                            pass
                        finally:
                            if not recv_task.done():
                                recv_task.cancel()
                                try:
                                    await recv_task
                                except asyncio.CancelledError:
                                    pass
                        break

                    # recv_task completed unexpectedly — reconnect
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    raise RuntimeError("Connection lost unexpectedly")

                except Exception as e:
                    await client.close()
                    if self._stop_event.is_set():
                        break
                    logger.warning(
                        f"WebSocket error: {e}. "
                        f"Reconnecting in {backoff:.0f}s..."
                    )
                    GLib.idle_add(
                        self._emit_error,
                        f"接続が切れました。{backoff:.0f}秒後に再接続します...",
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                else:
                    await client.close()
        finally:
            recorder.stop()
            logger.info("Streaming session ended")

    async def _send_audio_loop(
        self, client: RivaWSClient, recorder: StreamingRecorder
    ):
        """Read audio chunks and send to WebSocket server.

        Commits are triggered by silence detection. After speech followed
        by silence, a commit is sent. The silence threshold is auto-calibrated
        from the first second of ambient noise.
        """
        import struct

        CALIBRATION_CHUNKS = 10     # 1s calibration period
        NOISE_MULTIPLIER = 3.0      # threshold = noise_floor * multiplier
        MIN_THRESHOLD = 300         # absolute minimum threshold
        SILENCE_COMMIT_CHUNKS = 2   # 200ms silence → commit
        FLUSH_INTERVAL_CHUNKS = 10  # Flush every 1s during silence
        MAX_FLUSHES = 3             # Up to 3 flush commits

        loop = asyncio.get_event_loop()
        has_speech = False
        silence_chunks = 0
        chunks_since_commit = 0
        flush_count = 0
        silence_after_commit = 0

        # Auto-calibration state
        calibration_rms_values: list[float] = []
        silence_threshold = 0.0  # Will be set after calibration

        while not self._stop_event.is_set():
            chunk = await loop.run_in_executor(
                None, lambda: recorder.get_chunk(timeout=0.2)
            )
            if not chunk:
                continue

            # Compute RMS energy of PCM16 audio
            samples = struct.unpack(f"<{len(chunk) // 2}h", chunk)
            rms = (sum(s * s for s in samples) / len(samples)) ** 0.5

            # Send audio to server during calibration too
            await client.send_audio(chunk)
            chunks_since_commit += 1

            # Calibration phase: collect noise floor samples
            if len(calibration_rms_values) < CALIBRATION_CHUNKS:
                calibration_rms_values.append(rms)
                if len(calibration_rms_values) == CALIBRATION_CHUNKS:
                    noise_floor = (
                        sum(calibration_rms_values)
                        / len(calibration_rms_values)
                    )
                    silence_threshold = max(
                        noise_floor * NOISE_MULTIPLIER, MIN_THRESHOLD
                    )
                    logger.info(
                        f"Noise calibration: floor={noise_floor:.0f} "
                        f"threshold={silence_threshold:.0f}"
                    )
                continue

            is_speech = rms >= silence_threshold
            if is_speech:
                has_speech = True
                silence_chunks = 0
                flush_count = 0
                silence_after_commit = 0
            else:
                silence_chunks += 1
                if flush_count < MAX_FLUSHES:
                    silence_after_commit += 1

            # Log RMS periodically (every 10 chunks = 1s)
            if chunks_since_commit % 10 == 0:
                logger.debug(
                    f"RMS: {rms:.0f} threshold={silence_threshold:.0f} "
                    f"speech={is_speech} has_speech={has_speech} "
                    f"silence={silence_chunks}"
                )

            # Commit after speech followed by silence
            if has_speech and silence_chunks >= SILENCE_COMMIT_CHUNKS:
                await client.commit()
                logger.debug(
                    f"Commit: {chunks_since_commit} chunks (rms={rms:.0f})"
                )
                has_speech = False
                chunks_since_commit = 0
                flush_count = 0
                silence_after_commit = 0

            # Periodic flush commits during silence to finalize text
            if (flush_count < MAX_FLUSHES
                    and silence_after_commit > 0
                    and silence_after_commit % FLUSH_INTERVAL_CHUNKS == 0):
                await client.commit()
                flush_count += 1
                logger.debug(
                    f"Flush {flush_count}/{MAX_FLUSHES}: "
                    f"{chunks_since_commit} chunks"
                )
                chunks_since_commit = 0

        # Send final commit for any remaining audio
        if chunks_since_commit > 0:
            await client.commit()
            logger.debug("Sent final commit")

    def _emit_delta(self, text: str) -> bool:
        """Emit TranscriptionDelta signal (called via GLib.idle_add)."""
        logger.debug(f"Delta: {len(text)} chars")
        self.TranscriptionDelta(text)
        return False  # Don't repeat

    def _emit_completed(self, text: str) -> bool:
        """Emit TranscriptionComplete signal (called via GLib.idle_add)."""
        if text:
            logger.debug(f"Completed: {len(text)} chars")
        self.TranscriptionComplete(text, 0)
        return False  # Don't repeat

    def _emit_error(self, message: str) -> bool:
        """Emit Error signal (called via GLib.idle_add)."""
        logger.error(f"Emitting error signal: {message}")
        self.Error(message)
        return False  # Don't repeat

    def cleanup(self):
        """Clean up resources."""
        logger.info("Cleaning up voice daemon service")
        if self.recording:
            self.recording = False
            self._stop_streaming()


def start_dbus_service(
    ws_url: str,
    model: str,
    language: str,
    compression: str | None = "deflate",
):
    """Start the D-Bus service and return the service object."""
    bus = SessionBus()
    service = VoiceDaemonService(
        ws_url=ws_url,
        model=model,
        language=language,
        compression=compression,
    )

    bus.publish("org.fcitx.Fcitx5.Voice", service)
    logger.info("D-Bus service published: org.fcitx.Fcitx5.Voice")

    return service
