"""
Wyoming Protocol client for connecting to Piper TTS services.

Uses the official wyoming package for protocol handling.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Iterator, List, Self, Tuple

from wyoming.audio import AudioChunk
from wyoming.client import AsyncTcpClient
from wyoming.event import Event
from wyoming.tts import Synthesize, SynthesizeVoice

logger = logging.getLogger("hermes-wyoming-piper")


class WyomingError(Exception):
    """Base error for Wyoming protocol operations."""


class WyomingConnectionError(WyomingError):
    """Connection to Wyoming server failed."""


class WyomingServerError(WyomingError):
    """Server returned an error."""


class WyomingVoice:
    """Represents a voice available on the Piper server."""

    def __init__(self, name: str, languages: List[str] | None = None):
        self.name = name
        self.languages = languages or []

    def __repr__(self) -> str:
        return f"WyomingVoice(name={self.name!r}, languages={self.languages!r})"


class WyomingPiperClient:
    """
    Client for connecting to a Wyoming Protocol Piper TTS server.

    The synchronous methods (connect, disconnect, synthesize) are serialized
    with an internal lock so concurrent calls from different threads cannot
    interleave on the shared connection; synthesize_stream() uses its own
    per-call connection instead.
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
        self._lock = threading.RLock()
        self._client: AsyncTcpClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._voices: List[WyomingVoice] | None = None
        self._info: Dict[str, Any] | None = None
        self._audio_format: Tuple[int, int, int] | None = None  # (rate, width, channels)

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create event loop."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop

    def _detach_client(
        self,
    ) -> Tuple[AsyncTcpClient | None, asyncio.AbstractEventLoop | None]:
        client = self._client
        loop = self._loop
        self._client = None
        self._info = None
        self._voices = None
        self._audio_format = None
        return client, loop

    async def _close_client(self, client: AsyncTcpClient) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=self.timeout)
        except (OSError, RuntimeError, TimeoutError, WyomingError) as error:
            logger.debug("Failed to disconnect Wyoming client: %s", error)

    def _invalidate_client(self) -> None:
        client, loop = self._detach_client()
        if client is None or loop is None or loop.is_closed():
            return
        try:
            loop.run_until_complete(self._close_client(client))
        except (OSError, RuntimeError, WyomingError) as error:
            logger.debug("Failed to invalidate Wyoming client: %s", error)

    def connect(self) -> None:
        """Establish TCP connection and query server capabilities."""
        with self._lock:
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
                try:
                    await client.connect()
                    await client.write_event(Event(type="describe"))
                    info_event = await client.read_event()
                    if info_event is None or info_event.type != "info":
                        raise WyomingConnectionError(
                            f"Expected 'info' event, got: {info_event.type if info_event else 'None'}"
                        )
                    voices = self._parse_voices_from_info(info_event.data)
                    return client, info_event, voices
                except BaseException:
                    await self._close_client(client)
                    raise

            try:
                self._client, info_event, voices = loop.run_until_complete(_connect())
                self._info = info_event.data
                self._voices = voices

                logger.info("Connected to Wyoming server at %s:%d", self.host, self.port)
                logger.info(
                    "Server has %d voices: %s",
                    len(self._voices),
                    [v.name for v in self._voices[:5]],
                )
            except Exception as e:
                self._detach_client()
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
        with self._lock:
            client = self._client
            loop = self._loop
            try:
                if client is not None and loop is not None and not loop.is_closed():
                    loop.run_until_complete(self._close_client(client))
            except (OSError, WyomingError) as error:
                logger.debug("Disconnect failed: %s", error)
            finally:
                self._detach_client()
                try:
                    if loop is not None and not loop.is_closed():
                        loop.close()
                except (OSError, RuntimeError) as error:
                    logger.debug("Event loop cleanup failed: %s", error)
                finally:
                    self._loop = None
            logger.info("Disconnected from Wyoming server")

    def describe(self) -> List[WyomingVoice]:
        """Get available voices (cached from connect)."""
        if self._voices is None:
            self.connect()
        return self._voices or []

    @property
    def audio_format(self) -> Tuple[int, int, int] | None:
        """Last known audio format (rate, width, channels) from synthesis."""
        return self._audio_format

    def synthesize(
        self,
        text: str,
        voice: str | None = None,
    ) -> bytes:
        """Synthesize text to raw PCM audio bytes. Format (rate, width, channels) is available via the audio_format property."""
        with self._lock:
            self.connect()
            client = self._client
            loop = self._get_loop()
            assert client is not None

            async def _synthesize():
                synthesize_voice = None
                if voice:
                    synthesize_voice = SynthesizeVoice(name=voice)

                synthesize = Synthesize(text=text, voice=synthesize_voice)
                await client.write_event(synthesize.event())

                audio_chunks: List[bytes] = []
                sample_rate = 22050
                sample_width = 2
                channels = 1

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
                        audio_chunks.append(chunk.audio)

                    elif event.type == "audio-stop" or event.type == "synthesize-stopped":
                        break

                    elif event.type == "error":
                        error_msg = event.data.get("text", "Unknown error")
                        raise WyomingServerError(f"Synthesis error: {error_msg}")

                self._audio_format = (sample_rate, sample_width, channels)

                return b"".join(audio_chunks)

            try:
                return loop.run_until_complete(_synthesize())
            except Exception as e:
                self._invalidate_client()
                if isinstance(e, WyomingServerError):
                    raise
                raise WyomingServerError(f"Synthesis failed: {e}") from e

    def synthesize_stream(
        self,
        text: str,
        voice: str | None = None,
    ) -> Iterator[Tuple[bytes, Tuple[int, int, int]]]:
        """Synthesize text and yield (pcm_bytes, (rate, width, channels)) tuples.

        Yields raw PCM chunks as they arrive from the server, plus the audio format
        on the first yield. The caller can pipe these directly to ffmpeg.

        Each call creates its own event loop + client connection in a worker thread
        to avoid "Future attached to a different loop" errors from the wyoming library.

        Errors raised on the worker thread — connection failures, timeouts,
        and server errors alike — are re-raised in the consuming thread as
        WyomingServerError.
        """
        from queue import Empty, Queue
        from threading import Thread

        q: Queue[Tuple[bytes, Tuple[int, int, int]] | Exception | None] = Queue()

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

                            elif event.type == "audio-stop" or event.type == "synthesize-stopped":
                                break
                            elif event.type == "error":
                                error_msg = event.data.get("text", "Unknown error")
                                raise WyomingServerError(f"Synthesis error: {error_msg}")
                    finally:
                        try:
                            await asyncio.wait_for(
                                client.disconnect(), timeout=_timeout
                            )
                        except TimeoutError:
                            logger.debug(
                                "synthesize_stream: disconnect timed out after %ss",
                                _timeout,
                            )
                        except Exception as te:  # noqa: BLE001 — teardown noise, logged not raised
                            logger.debug(
                                "synthesize_stream: disconnect failed: %s", te
                            )

                loop.run_until_complete(_run())
            # Forward everything to the consumer (re-raised as WyomingServerError
            # there); asyncio.CancelledError still escapes (BaseException).
            except Exception as e:  # noqa: BLE001
                q.put(e)
            finally:
                q.put(None)
                loop.close()

        thread = Thread(target=_consume_on_new_loop, daemon=True)
        thread.start()

        try:
            while True:
                item = q.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    if isinstance(item, WyomingServerError):
                        raise item
                    raise WyomingServerError(f"Synthesis failed: {item}") from item
                yield item
        finally:
            # Consumer gone (close()/GeneratorExit/exception): drain whatever
            # is already queued so the daemon worker is not blocked on put(),
            # then let its finally (client.disconnect, loop.close, sentinel)
            # run to completion. The worker remains a daemon thread; a full
            # cancellation protocol is documented as a follow-up.
            while not q.empty():
                try:
                    q.get_nowait()
                except Empty:
                    break

    def is_connected(self) -> bool:
        """Check if the client is currently connected."""
        return self._client is not None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.disconnect()
