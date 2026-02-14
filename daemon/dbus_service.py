"""D-Bus service for fcitx5-voice daemon."""
import logging

import numpy as np
from pydbus import SessionBus
from pydbus.generic import signal

from .recorder import RealtimeRecorder
from .transcriber import Transcriber

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
    """D-Bus service for voice input daemon."""

    dbus = DBUS_INTERFACE

    def __init__(self):
        """Initialize the D-Bus service."""
        logger.info("Initializing voice daemon service")
        self.transcriber = Transcriber()
        self.recorder = RealtimeRecorder(on_segment=self.on_audio_segment)
        self.recording = False
        logger.info("Voice daemon service initialized")

    def StartRecording(self):
        """Start recording audio (D-Bus method)."""
        if not self.recording:
            logger.info("D-Bus: StartRecording called")
            try:
                self.recorder.start()
                self.recording = True
                self.RecordingStarted()
                logger.info("Recording started successfully")
            except Exception as e:
                error_msg = f"Failed to start recording: {e}"
                logger.error(error_msg)
                self.Error(error_msg)
        else:
            logger.warning("Recording already in progress")

    def StopRecording(self):
        """Stop recording audio (D-Bus method)."""
        if self.recording:
            logger.info("D-Bus: StopRecording called")
            try:
                self.recorder.stop()
                self.recording = False
                self.RecordingStopped()
                logger.info("Recording stopped successfully")
            except Exception as e:
                error_msg = f"Failed to stop recording: {e}"
                logger.error(error_msg)
                self.Error(error_msg)
        else:
            logger.warning("Recording not in progress")

    def GetStatus(self) -> str:
        """Get current recording status (D-Bus method)."""
        status = "recording" if self.recording else "idle"
        logger.debug(f"D-Bus: GetStatus called, returning '{status}'")
        return status

    # D-Bus signals
    TranscriptionComplete = signal()
    RecordingStarted = signal()
    RecordingStopped = signal()
    Error = signal()

    def on_audio_segment(self, audio_data: np.ndarray, segment_num: int):
        """Callback from recorder when audio segment is ready."""
        logger.info(f"Processing audio segment #{segment_num}")
        try:
            text = self.transcriber.transcribe(audio_data)
            if text:
                logger.info(f"Emitting TranscriptionComplete signal: '{text}'")
                self.TranscriptionComplete(text, segment_num)
            else:
                logger.warning(f"Segment #{segment_num}: Empty transcription")
        except Exception as e:
            error_msg = f"Transcription failed for segment #{segment_num}: {e}"
            logger.error(error_msg)
            self.Error(error_msg)

    def cleanup(self):
        """Clean up resources."""
        logger.info("Cleaning up voice daemon service")
        if self.recording:
            self.recorder.stop()
        self.transcriber.cleanup()


def start_dbus_service():
    """Start the D-Bus service and return the service object."""
    bus = SessionBus()
    service = VoiceDaemonService()

    logger.info("Publishing D-Bus service: org.fcitx.Fcitx5.Voice")
    bus.publish("org.fcitx.Fcitx5.Voice", service)
    logger.info("D-Bus service published successfully")

    return service
