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
from .ws_client import RivaWSClient, DEFAULT_COMMIT_INTERVAL

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
        commit_interval: int = DEFAULT_COMMIT_INTERVAL,
    ):
        logger.info("Initializing voice daemon service (streaming mode)")
        self.ws_url = ws_url
        self.model = model
        self.language = language
        self.commit_interval = commit_interval
        self.recording = False
        self._stop_event: threading.Event | None = None
        self._stream_thread: threading.Thread | None = None
        logger.info(
            f"Config: url={ws_url}, model={model}, "
            f"language={language}, commit_interval={commit_interval}"
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

        logger.info("D-Bus: StartRecording called")
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

        logger.info("D-Bus: StopRecording called")
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
        """Main streaming coroutine: connect, capture, send, receive."""
        recorder = StreamingRecorder()
        client = RivaWSClient(
            url=self.ws_url,
            model=self.model,
            language=self.language,
            on_delta=lambda text: GLib.idle_add(
                self._emit_delta, text
            ),
            on_completed=lambda text: GLib.idle_add(
                self._emit_completed, text
            ),
            on_error=lambda msg: GLib.idle_add(self._emit_error, msg),
        )

        try:
            await client.connect()
            recorder.start()

            send_task = asyncio.create_task(
                self._send_audio_loop(client, recorder)
            )
            recv_task = asyncio.create_task(client.recv_loop())

            # Wait for either task to finish (send_audio_loop exits on stop)
            done, pending = await asyncio.wait(
                [send_task, recv_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Check for exceptions in completed tasks
            for task in done:
                if task.exception():
                    logger.error(
                        f"Task error: {task.exception()}",
                        exc_info=task.exception(),
                    )

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            logger.error(f"Stream setup error: {e}", exc_info=True)
            GLib.idle_add(self._emit_error, str(e))
        finally:
            recorder.stop()
            await client.close()
            logger.info("Streaming session ended")

    async def _send_audio_loop(
        self, client: RivaWSClient, recorder: StreamingRecorder
    ):
        """Read audio chunks and send to WebSocket server."""
        loop = asyncio.get_event_loop()
        chunks_since_commit = 0

        while not self._stop_event.is_set():
            chunk = await loop.run_in_executor(
                None, lambda: recorder.get_chunk(timeout=0.2)
            )
            if chunk:
                await client.send_audio(chunk)
                chunks_since_commit += 1
                if chunks_since_commit >= self.commit_interval:
                    await client.commit()
                    chunks_since_commit = 0

        # Send final commit for any remaining audio
        if chunks_since_commit > 0:
            await client.commit()
            logger.info("Sent final audio commit")

    def _emit_delta(self, text: str) -> bool:
        """Emit TranscriptionDelta signal (called via GLib.idle_add)."""
        logger.debug(f"Delta: {len(text)} chars")
        self.TranscriptionDelta(text)
        return False  # Don't repeat

    def _emit_completed(self, text: str) -> bool:
        """Emit TranscriptionComplete signal (called via GLib.idle_add)."""
        if text:
            logger.info(f"Completed: {len(text)} chars")
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
    ws_url: str, model: str, language: str, commit_interval: int
):
    """Start the D-Bus service and return the service object."""
    bus = SessionBus()
    service = VoiceDaemonService(
        ws_url=ws_url,
        model=model,
        language=language,
        commit_interval=commit_interval,
    )

    logger.info("Publishing D-Bus service: org.fcitx.Fcitx5.Voice")
    bus.publish("org.fcitx.Fcitx5.Voice", service)
    logger.info("D-Bus service published successfully")

    return service
