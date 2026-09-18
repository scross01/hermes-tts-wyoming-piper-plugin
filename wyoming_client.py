"""
Wyoming Protocol client for connecting to Piper TTS services.

Uses the official wyoming package for protocol handling.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from typing import Any, Dict, Iterator, List, Optional, Tuple

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.event import Event
from wyoming.tts import Synthesize, SynthesizeVoice

logger = logging.getLogger("hermes-wyoming-piper")


class WyomingError(Exception):
    """Base error for Wyoming protocol operations."""
    pass


class WyomingConnectionError(WyomingError):
    """Connection to Wyoming server failed."""
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
        self._client: Optional[AsyncTcpClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._voices: Optional[List[WyomingVoice]] = None
        self._info: Optional[Dict[str, Any]] = None
        self._audio_format: Optional[Tuple[int, int, int]] = None  # (rate, width, channels)

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create event loop."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def connect(self) -> None:
        """Establish TCP connection and query server capabilities."""
        if self._client is not None:
            return

        loop = self._get_loop()

        async def _connect():
            client = AsyncTcpClient(
                self.host,
                self.port,
                connect_timeout=self.timeout,
                read_timeout=self.timeout,
            )
            await client.connect()

            # Send describe to get server capabilities
            await client.write_event(Event(type="describe"))
            info_event = await client.read_event()
            if info_event is None or info_event.type != "info":
                raise WyomingConnectionError(
                    f"Expected 'info' event, got: {info_event.type if info_event else 'None'}"
                )

            return client, info_event

        try:
            self._client, info_event = loop.run_until_complete(_connect())
            self._info = info_event.data

            # Parse voices from info event
            self._voices = self._parse_voices_from_info(self._info)

            logger.info("Connected to Wyoming server at %s:%d", self.host, self.port)
            logger.info(
                "Server has %d voices: %s",
                len(self._voices),
                [v.name for v in self._voices[:5]],
            )
        except Exception as e:
            self._client = None
            if isinstance(e, WyomingConnectionError):
                raise
            raise WyomingConnectionError(
                f"Failed to connect to {self.host}:{self.port}: {e}"
            ) from e

    def _parse_voices_from_info(self, info: Dict[str, Any]) -> List[WyomingVoice]:
        """Parse voice list from server info event."""
        voices = []
        tts_list = info.get("tts", [])

        for tts_provider in tts_list:
            for voice_info in tts_provider.get("voices", []):
                name = voice_info.get("name", "")
                languages = voice_info.get("languages", [])
                voices.append(WyomingVoice(name=name, languages=languages))

        return voices

    def disconnect(self) -> None:
        """Close the TCP connection."""
        if self._client is not None:
            loop = self._get_loop()

            async def _disconnect():
                if self._client:
                    await self._client.disconnect()

            try:
                loop.run_until_complete(_disconnect())
            except Exception:
                pass
            self._client = None
            self._voices = None
            self._info = None
            self._audio_format = None
            logger.info("Disconnected from Wyoming server")

    def describe(self) -> List[WyomingVoice]:
        """Get available voices (cached from connect)."""
        if self._voices is None:
            self.connect()
        return self._voices or []

    @property
    def audio_format(self) -> Optional[Tuple[int, int, int]]:
        """Last known audio format (rate, width, channels) from synthesis."""
        return self._audio_format

    def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
    ) -> bytes:
        """Synthesize text to WAV audio bytes."""
        self.connect()
        loop = self._get_loop()

        async def _synthesize():
            assert self._client is not None

            synthesize_voice = None
            if voice:
                synthesize_voice = SynthesizeVoice(name=voice)

            synthesize = Synthesize(text=text, voice=synthesize_voice)
            await self._client.write_event(synthesize.event())

            audio_chunks: List[bytes] = []
            sample_rate = 22050
            sample_width = 2
            channels = 1

            while True:
                event = await self._client.read_event()
                if event is None:
                    raise WyomingServerError("Connection closed during synthesis")

                if event.type == "audio-start":
                    sample_rate = event.data.get("rate", 22050)
                    sample_width = event.data.get("width", 2)
                    channels = event.data.get("channels", 1)

                elif event.type == "audio-chunk":
                    chunk = AudioChunk.from_event(event)
                    audio_chunks.append(chunk.audio)

                elif event.type == "audio-stop":
                    break

                elif event.type == "synthesize-stopped":
                    break

                elif event.type == "error":
                    error_msg = event.data.get("text", "Unknown error")
                    raise WyomingServerError(f"Synthesis error: {error_msg}")

            self._audio_format = (sample_rate, sample_width, channels)

            raw_audio = b"".join(audio_chunks)

            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(sample_width)
                wf.setframerate(sample_rate)
                wf.writeframes(raw_audio)

            return wav_buffer.getvalue()

        try:
            return loop.run_until_complete(_synthesize())
        except Exception as e:
            if isinstance(e, WyomingServerError):
                raise
            raise WyomingServerError(f"Synthesis failed: {e}") from e

    def synthesize_stream(
        self,
        text: str,
        voice: Optional[str] = None,
    ) -> Iterator[Tuple[bytes, Tuple[int, int, int]]]:
        """Synthesize text and yield (pcm_bytes, (rate, width, channels)) tuples.

        Yields raw PCM chunks as they arrive from the server, plus the audio format
        on the first yield. The caller can pipe these directly to ffmpeg.

        Each call creates its own event loop + client connection in a worker thread
        to avoid "Future attached to a different loop" errors from the wyoming library.
        """
        from queue import Queue
        from threading import Thread

        q: "Queue[Optional[Tuple[bytes, Tuple[int, int, int]]]]" = Queue()

        _host, _port, _timeout = self.host, self.port, self.timeout

        def _consume_on_new_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def _run():
                    client = AsyncTcpClient(
                        _host, _port,
                        connect_timeout=_timeout,
                        read_timeout=_timeout,
                    )
                    await client.connect()
                    try:
                        synthesize_voice = None
                        if voice:
                            synthesize_voice = SynthesizeVoice(name=voice)

                        syn = Synthesize(text=text, voice=synthesize_voice)
                        await client.write_event(syn.event())

                        sample_rate = 22050
                        sample_width = 2
                        channels = 1
                        format_sent = False

                        while True:
                            event = await client.read_event()
                            if event is None:
                                raise WyomingServerError("Connection closed during synthesis")

                            if event.type == "audio-start":
                                sample_rate = event.data.get("rate", 22050)
                                sample_width = event.data.get("width", 2)
                                channels = event.data.get("channels", 1)

                            elif event.type == "audio-chunk":
                                chunk = AudioChunk.from_event(event)
                                if not format_sent:
                                    q.put((chunk.audio, (sample_rate, sample_width, channels)))
                                    format_sent = True
                                else:
                                    q.put((chunk.audio, None))

                            elif event.type == "audio-stop":
                                break
                            elif event.type == "synthesize-stopped":
                                break
                            elif event.type == "error":
                                error_msg = event.data.get("text", "Unknown error")
                                raise WyomingServerError(f"Synthesis error: {error_msg}")
                    finally:
                        await client.disconnect()

                loop.run_until_complete(_run())
            except Exception as e:
                q.put(e)
            finally:
                q.put(None)
                loop.close()

        thread = Thread(target=_consume_on_new_loop, daemon=True)
        thread.start()

        while True:
            item = q.get()
            if item is None:
                break
            if isinstance(item, Exception):
                if isinstance(item, WyomingServerError):
                    raise item
                raise WyomingServerError(f"Synthesis failed: {item}") from item
            yield item

    def is_connected(self) -> bool:
        """Check if the client is currently connected."""
        return self._client is not None

    def __enter__(self) -> "WyomingPiperClient":
        self.connect()
        return self

    def __exit__(self, *args: Any) -> None:
        self.disconnect()
