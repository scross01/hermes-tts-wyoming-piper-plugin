"""
Hermes Wyoming Piper TTS Plugin

Connects to a remote Piper TTS service via Wyoming Protocol (TCP)
and registers as a TTS provider in Hermes.

Config settings (plugins.entries.tts-wyoming-piper.settings):
  host: piper.local          # Piper server hostname
  port: 10200                 # Wyoming Protocol port
  voice: en_US-lessac-medium  # Voice name
  timeout: 10                 # Connection timeout seconds
  mode: pipe                  # pipe (default) or stream
    - pipe: PCM → ffmpeg → Opus directly (1 conversion)
    - stream: streaming delivery via TTSProvider.stream()
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import uuid
import wave
from typing import Any, Dict, Iterator, List

from agent.tts_provider import TTSProvider

from .wyoming_client import WyomingError

logger = logging.getLogger("hermes-wyoming-piper")

# Debug file logging — gated by plugins.entries.tts-wyoming-piper.settings.debug
_DEBUG_LOG = os.path.expanduser("~/.hermes/logs/wyoming-piper-debug.log")
_debug_enabled = False
_FFMPEG_SHUTDOWN_TIMEOUT = 10  # seconds

def _debug(msg: str) -> None:
    """Write to debug log file and logger when debug mode is on."""
    if not _debug_enabled:
        return
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    try:
        with open(_DEBUG_LOG, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.debug("Failed to write debug log: %s", e)
    logger.debug(msg)


class WyomingPiperProvider(TTSProvider):
    """TTS provider that connects to a remote Piper via Wyoming Protocol."""

    _name = "wyoming-piper"

    def __init__(self, host: str = "localhost", port: int = 10200,
                 voice: str = "", timeout: int = 10, mode: str = "pipe"):
        self._host = host
        self._port = port
        self._voice = voice
        self._timeout = timeout
        self._mode = mode  # "pipe" or "stream"
        self._client = None
        self._voices: List[Dict[str, Any]] | None = None

    @property
    def name(self) -> str:
        return self._name

    def _get_client(self):
        if self._client is not None:
            return self._client

        from .wyoming_client import WyomingPiperClient

        self._client = WyomingPiperClient(
            host=self._host,
            port=self._port,
            timeout=self._timeout,
        )
        return self._client

    def list_voices(self) -> List[Dict[str, Any]]:
        if self._voices is not None:
            return self._voices

        try:
            client = self._get_client()
            wyoming_voices = client.describe()
            self._voices = [
                {
                    "id": v.name,
                    "display": v.name,
                    "language": v.languages[0] if v.languages else "en",
                }
                for v in wyoming_voices
            ]
            return self._voices
        except WyomingError as e:
            logger.warning("Failed to list voices: %s", e)
            return []

    def default_voice(self) -> str | None:
        if self._voice:
            return self._voice
        voices = self.list_voices()
        return voices[0]["id"] if voices else None

    # --- Option 1: Pipe PCM → Opus (default) ---

    def synthesize(
        self,
        text: str,
        output_path: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        speed: float | None = None,
        format: str = "mp3",
        **extra: Any,
    ) -> str:
        request_id = str(uuid.uuid4())
        _debug(
            f"[{request_id}] synthesize() mode={self._mode}: text={len(text)} chars, "
            f"voice={voice or self._voice or '(server default)'}, format={format}"
        )

        if self._mode == "stream":
            return self._synthesize_stream_to_file(request_id, text, output_path,
                                                   voice=voice, format=format)

        return self._synthesize_pipe(request_id, text, output_path,
                                     voice=voice, format=format)

    def _synthesize_pipe(self, request_id: str, text: str, output_path: str,
                         voice: str | None = None, format: str = "mp3") -> str:
        """Option 1: Pipe PCM directly to ffmpeg, skip WAV/MP3 intermediaries."""
        client = self._get_client()
        voice_name = voice or self.default_voice()

        # Determine target format - use opus for voice_bubble platforms, mp3 otherwise
        target_ext = self._target_extension(format)

        t0 = time.monotonic()
        wav_bytes = client.synthesize(text, voice=voice_name)
        elapsed = time.monotonic() - t0

        _debug(
            f"[{request_id}] received {len(wav_bytes)} bytes in {elapsed:.2f}s, "
            f"voice={voice_name}"
        )

        # Get audio format from client
        fmt = client.audio_format or (22050, 2, 1)
        rate, width, channels = fmt

        # If target is wav or pcm, write directly
        if target_ext in ("wav", "pcm"):
            wav_path = output_path if output_path.endswith(".wav") else output_path.rsplit(".", 1)[0] + ".wav"
            with open(wav_path, "wb") as f:
                f.write(wav_bytes)
            _debug(f"[{request_id}] wrote WAV: {wav_path}")
            return wav_path

        # Pipe PCM → ffmpeg → target format
        return self._pipe_pcm_to_format(request_id, wav_bytes, rate, width, channels,
                                         output_path, target_ext)

    def _build_ffmpeg_cmd(self, ffmpeg: str, rate: int, channels: int,
                          target_ext: str, out_path: str) -> List[str]:
        """Build the ffmpeg command list for raw PCM input to target_ext.

        - target_ext "ogg"/"opus" → libopus, 48k, vbr on
        - target_ext "mp3" → libmp3lame
        - target_ext "flac" → flac
        - target_ext "wav"/"pcm" → no extra codec args (PCM WAV is default)
        """
        cmd = [
            ffmpeg, "-y",
            "-f", "s16le",
            "-ar", str(rate),
            "-ac", str(channels),
            "-i", "pipe:0",
        ]
        if target_ext in ("ogg", "opus"):
            cmd.extend(["-acodec", "libopus", "-b:a", "48k", "-vbr", "on"])
        elif target_ext == "mp3":
            cmd.extend(["-acodec", "libmp3lame"])
        elif target_ext == "flac":
            cmd.extend(["-acodec", "flac"])
        cmd.append(out_path)
        return cmd

    def _pipe_pcm_to_format(self, request_id: str, pcm_data: bytes,
                            rate: int, width: int, channels: int,
                            output_path: str, target_ext: str) -> str:
        """Pipe raw PCM data through ffmpeg to target format.

        Raises RuntimeError if ffmpeg is not found and the target format
        requires it (everything except wav/pcm).
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError(
                f"ffmpeg not found; cannot produce '{target_ext}' output. "
                "Install ffmpeg or request format='wav'."
            )

        out_path = output_path if output_path.endswith(f".{target_ext}") else \
                   output_path.rsplit(".", 1)[0] + f".{target_ext}"

        cmd = self._build_ffmpeg_cmd(ffmpeg, rate, channels, target_ext, out_path)

        try:
            result = subprocess.run(
                cmd,
                input=pcm_data,
                capture_output=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0:
                _debug(f"[{request_id}] piped PCM → {target_ext}: {out_path}")
                return out_path
            else:
                _debug(f"[{request_id}] ffmpeg error: {result.stderr[:200]}")
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            _debug(f"[{request_id}] ffmpeg failed: {e}")

        # Fallback to WAV
        wav_path = output_path.rsplit(".", 1)[0] + ".wav"
        self._write_fallback_wav(pcm_data, rate, width, channels, wav_path)
        return wav_path

    def _pipe_stream_to_format(self, request_id: str, pcm_iter: Iterator[bytes],
                               rate: int, width: int, channels: int,
                               output_path: str, target_ext: str) -> str:
        """Stream PCM chunks through ffmpeg to target format."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError(
                f"ffmpeg not found; cannot produce '{target_ext}' output. "
                "Install ffmpeg or request format='wav'."
            )

        out_path = output_path if output_path.endswith(f".{target_ext}") else \
                   output_path.rsplit(".", 1)[0] + f".{target_ext}"

        cmd = self._build_ffmpeg_cmd(ffmpeg, rate, channels, target_ext, out_path)

        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if proc.stdin is None:
                raise RuntimeError("ffmpeg failed to open stdin pipe")

            for chunk in pcm_iter:
                proc.stdin.write(chunk)
            proc.stdin.close()

            result = proc.wait(timeout=30)
            if result == 0:
                _debug(f"[{request_id}] piped PCM → {target_ext}: {out_path}")
                return out_path
            else:
                stderr_bytes = proc.stderr.read() if proc.stderr else b""
                stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:200]
                _debug(f"[{request_id}] ffmpeg error: {stderr_text}")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            _debug(f"[{request_id}] ffmpeg failed: {e}")
            if proc is not None:
                if proc.stdin:
                    proc.stdin.close()
                proc.kill()

        wav_path = output_path.rsplit(".", 1)[0] + ".wav"
        self._write_fallback_wav(b"".join(pcm_iter), rate, width, channels, wav_path)
        _debug(f"[{request_id}] ffmpeg failed, wrote fallback WAV")
        return wav_path

    def _write_fallback_wav(self, pcm_data: bytes, rate: int, width: int,
                             channels: int, wav_path: str) -> None:
        """Write raw PCM data as a valid WAV file."""
        with open(wav_path, "wb") as f, wave.open(f, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(width)
            wf.setframerate(rate)
            wf.writeframes(pcm_data)

    def _target_extension(self, format: str) -> str:
        """Determine target file extension from format hint."""
        fmt = format.lower()
        if fmt in ("opus", "ogg"):
            return "ogg"
        elif fmt in ("wav", "pcm"):
            return "wav"
        elif fmt == "flac":
            return "flac"
        return "mp3"

    # --- Option 2: Streaming delivery ---

    def _synthesize_stream_to_file(self, request_id: str, text: str, output_path: str,
                                   voice: str | None = None, format: str = "mp3") -> str:
        """Option 2: Stream PCM chunks through ffmpeg as they arrive."""
        client = self._get_client()
        voice_name = voice or self.default_voice()
        target_ext = self._target_extension(format)

        t0 = time.monotonic()

        stream_iter = client.synthesize_stream(text, voice=voice_name)
        first_chunk, fmt_info = next(stream_iter)
        audio_fmt = fmt_info

        def pcm_iter():
            nonlocal audio_fmt
            yield first_chunk
            for pcm_bytes, fmt_info in stream_iter:
                if fmt_info is not None:
                    audio_fmt = fmt_info
                yield pcm_bytes

        if audio_fmt is None:
            audio_fmt = (22050, 2, 1)
        rate, width, channels = audio_fmt

        if target_ext in ("wav", "pcm"):
            wav_path = output_path if output_path.endswith(".wav") else output_path.rsplit(".", 1)[0] + ".wav"
            raw_pcm = b"".join(pcm_iter())
            with open(wav_path, "wb") as f:
                f.write(raw_pcm)
            elapsed = time.monotonic() - t0
            _debug(
                f"[{request_id}] streamed WAV: {wav_path} in {elapsed:.2f}s, voice={voice_name}"
            )
            return wav_path

        result_path = self._pipe_stream_to_format(request_id, pcm_iter(), rate, width, channels,
                                                   output_path, target_ext)
        elapsed = time.monotonic() - t0
        _debug(
            f"[{request_id}] streamed to {target_ext}: {result_path} in {elapsed:.2f}s, voice={voice_name}"
        )
        return result_path

    def stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        format: str = "opus",
        **extra: Any,
    ) -> Iterator[bytes]:
        """Stream synthesized audio bytes for voice bubble delivery.

        Yields Opus-encoded audio chunks as they arrive from Piper.
        """
        client = self._get_client()
        voice_name = voice or self.default_voice()

        _debug(f"stream() called: text={len(text)} chars, voice={voice_name}")

        # Read first chunk to determine actual audio format before starting ffmpeg
        stream_iter = client.synthesize_stream(text, voice=voice_name)
        try:
            first_chunk, fmt_info = next(stream_iter)
        except StopIteration:
            return

        if fmt_info is not None:
            rate, _, channels = fmt_info
        else:
            rate, _, channels = 22050, 2, 1

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError(
                "ffmpeg not found; cannot produce 'opus' output. "
                "Install ffmpeg or use mode='pipe' with format='wav'."
            )

        cmd = self._build_ffmpeg_cmd(ffmpeg, rate, channels, "opus", "pipe:1")

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        if proc.stdin is None or proc.stdout is None:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            raise RuntimeError("ffmpeg failed to open pipes")

        # Use a writer thread to avoid pipe deadlock between stdin and stdout
        from queue import Queue
        from threading import Thread

        write_queue: Queue[bytes | None] = Queue()
        _stdin = proc.stdin

        def _writer():
            while True:
                data = write_queue.get()
                if data is None:
                    _stdin.flush()
                    break
                _stdin.write(data)
                _stdin.flush()

        writer_thread = Thread(target=_writer, daemon=True)
        writer_thread.start()

        try:
            write_queue.put(first_chunk)

            for pcm_bytes, fmt_info in stream_iter:
                if fmt_info is not None:
                    rate, _, channels = fmt_info
                    _debug(f"stream: format={fmt_info}")
                write_queue.put(pcm_bytes)

                opus_chunk = proc.stdout.read(4096)
                if opus_chunk:
                    yield opus_chunk

            write_queue.put(None)
            writer_thread.join(timeout=5)

            if proc.stdin:
                proc.stdin.close()
            remaining = proc.stdout.read()
            if remaining:
                yield remaining
        finally:
            if proc.stdin:
                proc.stdin.close()
            try:
                proc.wait(timeout=_FFMPEG_SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                _debug("stream(): ffmpeg did not exit within timeout, killing")
                proc.kill()
                proc.wait()

        _debug("stream() completed")

    def warm(self) -> None:
        try:
            self._get_client().connect()
            self.list_voices()
        except WyomingError as e:
            logger.debug("Warm-up failed: %s", e)

    def release(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except WyomingError as e:
                logger.debug("Disconnect failed during release: %s", e)
            self._client = None

    @property
    def voice_compatible(self) -> bool:
        return True


def register(ctx) -> None:
    global _debug_enabled
    _debug_enabled = bool(ctx.get_config("debug", False))
    provider = WyomingPiperProvider(
        host=ctx.get_config("host", "localhost"),
        port=ctx.get_config("port", 10200),
        voice=ctx.get_config("voice", ""),
        timeout=ctx.get_config("timeout", 10),
        mode=ctx.get_config("mode", "pipe"),
    )
    ctx.register_tts_provider(provider)
    _debug(f"Plugin registered: host={provider._host} port={provider._port} "
           f"voice={provider._voice} mode={provider._mode} debug={_debug_enabled}")
