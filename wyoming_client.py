"""
Wyoming Protocol client for connecting to Piper TTS services.

Implements raw TCP framing per the Wyoming spec:
  - 4-byte big-endian length prefix for each event
  - Event types: describe, describe-response, text-to-speak, audio-start, audio-chunk, audio-stop
  - Audio format: WAV PCM 16-bit mono 22050Hz (Piper default)

References:
  - https://github.com/OHF-Voice/wyoming
  - https://github.com/rhasspy/wyoming-piper
"""

from __future__ import annotations

import io
import json
import logging
import socket
import struct
import wave
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes-wyoming-piper")

# Wyoming event type IDs (from the spec)
EVENT_DESCRIBE = 0
EVENT_DESCRIBE_RESPONSE = 1
EVENT_TEXT_TO_SPEAK = 2
EVENT_AUDIO_START = 3
EVENT_AUDIO_CHUNK = 4
EVENT_AUDIO_STOP = 5
EVENT_ERROR = 6

# Event name mapping for logging
EVENT_NAMES = {
    EVENT_DESCRIBE: "describe",
    EVENT_DESCRIBE_RESPONSE: "describe-response",
    EVENT_TEXT_TO_SPEAK: "text-to-speak",
    EVENT_AUDIO_START: "audio-start",
    EVENT_AUDIO_CHUNK: "audio-chunk",
    EVENT_AUDIO_STOP: "audio-stop",
    EVENT_ERROR: "error",
}

# Piper default audio format
DEFAULT_SAMPLE_RATE = 22050
DEFAULT_SAMPLE_WIDTH = 2  # 16-bit
DEFAULT_CHANNELS = 1


class WyomingError(Exception):
    """Base error for Wyoming protocol operations."""
    pass


class WyomingConnectionError(WyomingError):
    """Connection to Wyoming server failed."""
    pass


class WyomingTimeoutError(WyomingError):
    """Operation timed out."""
    pass


class WyomingServerError(WyomingError):
    """Server returned an error."""
    pass


class WyomingVoice:
    """Represents a voice available on the Piper server."""

    def __init__(self, name: str, languages: Optional[List[str]] = None):
        self.name = name
        self.languages = languages or []

    def __repr__(self) -> str:
        return f"WyomingVoice(name={self.name!r}, languages={self.languages!r})"


class WyomingPiperClient:
    """
    Client for connecting to a Wyoming Protocol Piper TTS server.

    Usage:
        client = WyomingPiperClient("raspberrypi08.home.lan", 10200)
        client.connect()
        voices = client.describe()
        audio = client.synthesize("Hello world", voice=voices[0].name)
        client.disconnect()
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 10200,
        timeout: float = 10.0,
    ):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._socket: Optional[socket.socket] = None
        self._voices: Optional[List[WyomingVoice]] = None

    def connect(self) -> None:
        """Establish TCP connection to the Wyoming server."""
        if self._socket is not None:
            return

        try:
            self._socket = socket.create_connection(
                (self.host, self.port),
                timeout=self.timeout,
            )
            logger.info("Connected to Wyoming server at %s:%d", self.host, self.port)
        except (socket.error, OSError) as e:
            self._socket = None
            raise WyomingConnectionError(
                f"Failed to connect to {self.host}:{self.port}: {e}"
            ) from e

    def disconnect(self) -> None:
        """Close the TCP connection."""
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
            logger.info("Disconnected from Wyoming server")

    def _send_event(self, event_type: int, payload: bytes = b"") -> None:
        """Send a Wyoming event with length-prefixed framing."""
        if self._socket is None:
            raise WyomingConnectionError("Not connected")

        # Frame: [4 bytes event type][4 bytes payload length][payload]
        header = struct.pack(">II", event_type, len(payload))
        try:
            self._socket.sendall(header + payload)
        except (socket.error, OSError) as e:
            self._socket = None
            raise WyomingConnectionError(f"Failed to send event: {e}") from e

    def _recv_event(self) -> Tuple[int, bytes]:
        """Receive a Wyoming event, returning (event_type, payload)."""
        if self._socket is None:
            raise WyomingConnectionError("Not connected")

        # Read header (8 bytes: event type + payload length)
        header = self._recv_exact(8)
        event_type, payload_len = struct.unpack(">II", header)

        # Read payload
        payload = self._recv_exact(payload_len) if payload_len > 0 else b""

        return event_type, payload

    def _recv_exact(self, n: int) -> bytes:
        """Receive exactly n bytes from the socket."""
        if self._socket is None:
            raise WyomingConnectionError("Not connected")

        data = bytearray()
        while len(data) < n:
            try:
                chunk = self._socket.recv(n - len(data))
            except socket.timeout:
                raise WyomingTimeoutError("Receive timed out")
            except (socket.error, OSError) as e:
                self._socket = None
                raise WyomingConnectionError(f"Receive failed: {e}") from e

            if not chunk:
                raise WyomingConnectionError("Connection closed by server")

            data.extend(chunk)

        return bytes(data)

    def describe(self) -> List[WyomingVoice]:
        """
        Query the server for available voices.

        Returns a list of WyomingVoice objects with name and supported languages.
        """
        self.connect()

        # Send Describe event
        self._send_event(EVENT_DESCRIBE)

        # Read response
        event_type, payload = self._recv_event()

        if event_type == EVENT_ERROR:
            error_msg = payload.decode("utf-8", errors="replace")
            raise WyomingServerError(f"Server error: {error_msg}")

        if event_type != EVENT_DESCRIBE_RESPONSE:
            raise WyomingServerError(
                f"Unexpected event type: {EVENT_NAMES.get(event_type, event_type)}"
            )

        # Parse JSON response
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as e:
            raise WyomingServerError(f"Invalid describe response: {e}") from e

        # Extract voices
        voices = []
        for voice_info in data.get("voices", []):
            name = voice_info.get("name", "")
            languages = voice_info.get("languages", [])
            voices.append(WyomingVoice(name=name, languages=languages))

        self._voices = voices
        logger.info("Server has %d voices: %s", len(voices), [v.name for v in voices])
        return voices

    def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
    ) -> bytes:
        """
        Synthesize text to audio.

        Args:
            text: Text to synthesize
            voice: Voice name (uses server default if None)

        Returns:
            WAV audio bytes
        """
        self.connect()

        # Build TextToSpeak event payload
        event_data: Dict[str, Any] = {"text": text}
        if voice:
            event_data["voice"] = {"name": voice}

        payload = json.dumps(event_data).encode("utf-8")
        self._send_event(EVENT_TEXT_TO_SPEAK, payload)

        # Collect audio chunks
        audio_chunks: List[bytes] = []
        sample_rate = DEFAULT_SAMPLE_RATE
        sample_width = DEFAULT_SAMPLE_WIDTH
        channels = DEFAULT_CHANNELS

        while True:
            event_type, chunk_data = self._recv_event()

            if event_type == EVENT_AUDIO_START:
                # Parse audio format from start event
                try:
                    start_info = json.loads(chunk_data)
                    sample_rate = start_info.get("rate", DEFAULT_SAMPLE_RATE)
                    sample_width = start_info.get("width", DEFAULT_SAMPLE_WIDTH)
                    channels = start_info.get("channels", DEFAULT_CHANNELS)
                except (json.JSONDecodeError, KeyError):
                    pass
                logger.debug(
                    "Audio start: rate=%d width=%d channels=%d",
                    sample_rate, sample_width, channels,
                )

            elif event_type == EVENT_AUDIO_CHUNK:
                audio_chunks.append(chunk_data)

            elif event_type == EVENT_AUDIO_STOP:
                logger.debug("Audio stop after %d chunks", len(audio_chunks))
                break

            elif event_type == EVENT_ERROR:
                error_msg = chunk_data.decode("utf-8", errors="replace")
                raise WyomingServerError(f"Synthesis error: {error_msg}")

            else:
                logger.warning(
                    "Unexpected event during synthesis: %s",
                    EVENT_NAMES.get(event_type, event_type),
                )

        # Combine chunks
        raw_audio = b"".join(audio_chunks)

        # Wrap in WAV container
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(raw_audio)

        return wav_buffer.getvalue()

    def is_connected(self) -> bool:
        """Check if the client is currently connected."""
        if self._socket is None:
            return False
        try:
            # Peek at socket to check if it's still alive
            self._socket.settimeout(0)
            data = self._socket.recv(1, socket.MSG_PEEK)
            self._socket.settimeout(self.timeout)
            # If we got data or no error, it's connected (data means server sent something)
            return True
        except (socket.timeout, BlockingIOError):
            # Timeout on peek = still connected, just no data
            self._socket.settimeout(self.timeout)
            return True
        except (socket.error, OSError):
            self._socket = None
            return False

    def __enter__(self) -> "WyomingPiperClient":
        self.connect()
        return self

    def __exit__(self, *args: Any) -> None:
        self.disconnect()
