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
    - pipe: PCM → ffmpeg → target format in one pass
    - stream: streaming delivery via TTSProvider.stream()
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
from typing import Any, Dict, Iterator, List

from agent.tts_provider import TTSProvider

try:
    # Hermes runtime loads this file as the ``tts_wyoming_piper`` package, so a
    # package-relative import is correct.
    from .wyoming_client import WyomingError, WyomingServerError
except ImportError:
    # When this file is imported as a standalone module (the repository root
    # doubles as a package, so pytest's package setup imports ``__init__.py``
    # directly without package context), relative imports have no parent
    # package. Fall back to the absolute path, which works once the repository
    # root is on ``sys.path``.
    from wyoming_client import WyomingError, WyomingServerError

logger = logging.getLogger("hermes-wyoming-piper")

# Debug file logging — gated by plugins.entries.tts-wyoming-piper.settings.debug
_DEBUG_LOG = os.path.expanduser("~/.hermes/logs/wyoming-piper-debug.log")
_debug_enabled = False
_FFMPEG_SHUTDOWN_TIMEOUT = 10  # seconds
_VALID_MODES = ("pipe", "stream")
_VALID_OUTPUT_FORMATS = ("mp3", "ogg", "opus", "wav", "flac", "pcm")

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
                 voice: str = "", timeout: int = 10, mode: str = "pipe",
                 output_format: str = "mp3", voice_compatible: bool = False):
        self._host = host
        self._port = port
        self._voice = voice
        self._timeout = timeout
        normalized_mode = (mode or "pipe").strip().lower()
        if normalized_mode not in _VALID_MODES:
            logger.warning(
                "tts-wyoming-piper: unknown mode %r; falling back to 'pipe'", mode
            )
            normalized_mode = "pipe"
        self._mode = normalized_mode

        normalized_format = (output_format or "mp3").strip().lower()
        if normalized_format not in _VALID_OUTPUT_FORMATS:
            logger.warning(
                "tts-wyoming-piper: unknown output_format %r; falling back to 'mp3'",
                output_format,
            )
            normalized_format = "mp3"
        self._output_format = normalized_format
        self._voice_compatible = voice_compatible
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
        format: str | None = None,
        **extra: Any,
    ) -> str:
        request_id = str(uuid.uuid4())
        if format is None:
            format = self._output_format or "mp3"
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
        """Pipe raw PCM from the client through ffmpeg to the target format; wav/pcm written directly."""
        client = self._get_client()
        voice_name = voice or self.default_voice()

        # Determine target format - use opus for voice_bubble platforms, mp3 otherwise
        target_ext = self._target_extension(format)

        t0 = time.monotonic()
        pcm_bytes = client.synthesize(text, voice=voice_name)
        elapsed = time.monotonic() - t0

        _debug(
            f"[{request_id}] received {len(pcm_bytes)} bytes in {elapsed:.2f}s, "
            f"voice={voice_name}"
        )

        # Get audio format from client
        fmt = client.audio_format or (22050, 2, 1)
        rate, width, channels = fmt

        # If target is wav or pcm, write directly
        if target_ext in ("wav", "pcm"):
            wav_path = output_path if output_path.endswith(".wav") else output_path.rsplit(".", 1)[0] + ".wav"
            if target_ext == "pcm":
                with open(wav_path, "wb") as f:
                    f.write(pcm_bytes)
            else:
                self._write_wav(pcm_bytes, rate, width, channels, wav_path)
            _debug(f"[{request_id}] wrote {target_ext}: {wav_path}")
            return wav_path

        # Pipe PCM → ffmpeg → target format
        return self._pipe_pcm_to_format(request_id, pcm_bytes, rate, width, channels,
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
        self._write_wav(pcm_data, rate, width, channels, wav_path)
        logger.warning("ffmpeg failed; wrote fallback WAV: %s", wav_path)
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

        consumed: List[bytes] = []
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
                consumed.append(chunk)
            proc.stdin.close()

            result = proc.wait(timeout=30)
            if result == 0:
                _debug(f"[{request_id}] piped PCM → {target_ext}: {out_path}")
                return out_path
            stderr_bytes = proc.stderr.read() if proc.stderr else b""
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:200]
            _debug(f"[{request_id}] ffmpeg error: {stderr_text}")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            _debug(f"[{request_id}] ffmpeg failed: {e}")
        finally:
            if proc is not None:
                self._shutdown_process(proc)

        wav_path = output_path.rsplit(".", 1)[0] + ".wav"
        self._write_wav(b"".join(consumed), rate, width, channels, wav_path)
        logger.warning("ffmpeg failed; wrote fallback WAV: %s", wav_path)
        _debug(f"[{request_id}] ffmpeg failed, wrote fallback WAV")
        return wav_path

    def _write_wav(self, pcm_data: bytes, rate: int, width: int,
                             channels: int, wav_path: str) -> None:
        """Write raw PCM data as a valid WAV file."""
        with open(wav_path, "wb") as f, wave.open(f, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(width)
            wf.setframerate(rate)
            wf.writeframes(pcm_data)

    def _shutdown_process(self, proc: subprocess.Popen) -> None:
        """Close stdin and reap the process with a bounded wait on every exit path.

        Never raises: cleanup must not mask an in-flight exception.
        """
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                proc.wait(timeout=_FFMPEG_SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                _debug("ffmpeg did not exit within timeout, killing")
                proc.kill()
                try:
                    proc.wait(timeout=_FFMPEG_SHUTDOWN_TIMEOUT)
                except (subprocess.TimeoutExpired, OSError):
                    _debug("ffmpeg still alive after kill; giving up on wait()")
        except OSError as e:
            _debug(f"ffmpeg cleanup error: {e}")

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
        try:
            first_chunk, fmt_info = next(stream_iter)
        except StopIteration as e:
            raise WyomingServerError("Server returned no audio") from e
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
            if target_ext == "pcm":
                raw_pcm = b"".join(pcm_iter())
                with open(wav_path, "wb") as f:
                    f.write(raw_pcm)
            else:
                self._write_wav(b"".join(pcm_iter()), rate, width, channels, wav_path)
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

        # Temp file must outlive Popen and is read/closed in the finally below.
        stderr_file = tempfile.TemporaryFile()  # noqa: SIM115
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )

        if proc.stdin is None or proc.stdout is None:
            self._shutdown_process(proc)
            raise RuntimeError("ffmpeg failed to open pipes")

        # Use a writer thread to avoid pipe deadlock between stdin and stdout
        from queue import Empty, Queue
        from threading import Thread

        write_queue: Queue[bytes | None] = Queue()
        _stdin = proc.stdin

        _WRITER_POLL_SECONDS = 0.1
        _WRITER_IDLE_LIMIT_SECONDS = 5.0

        def _writer():
            try:
                idle = 0.0
                while True:
                    try:
                        data = write_queue.get(timeout=_WRITER_POLL_SECONDS)
                    except Empty:
                        idle += _WRITER_POLL_SECONDS
                        if idle >= _WRITER_IDLE_LIMIT_SECONDS:
                            return  # no producer feeding us; exit
                        continue
                    idle = 0.0
                    if data is None:
                        _stdin.flush()
                        return
                    _stdin.write(data)
                    _stdin.flush()
            except (OSError, ValueError):
                _debug("stream(): writer thread failed writing to ffmpeg")

        writer_thread = Thread(target=_writer, daemon=True)
        writer_thread.start()

        completed = False
        try:
            write_queue.put(first_chunk)

            for pcm_bytes, fmt_info in stream_iter:
                if fmt_info is not None:
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
            # ffmpeg has exited by the time the drain returns: record its
            # status BEFORE suspending on the final yield, so a consumer that
            # never resumes us still gets the failure surfaced in the finally.
            completed = True
            if remaining:
                yield remaining
        finally:
            # Signal the writer if it is still running (abnormal exit path).
            write_queue.put(None)
            if writer_thread.is_alive():
                writer_thread.join(timeout=5)
            self._shutdown_process(proc)
            stderr_file.seek(0)
            stderr_text = stderr_file.read().decode("utf-8", errors="replace")[-500:]
            stderr_file.close()
            if completed and proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg stream encoding failed (rc={proc.returncode}): {stderr_text}"
                )

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
        return self._voice_compatible


def register(ctx) -> None:
    global _debug_enabled
    _debug_enabled = bool(ctx.get_config("debug", False))
    provider = WyomingPiperProvider(
        host=ctx.get_config("host", "localhost"),
        port=ctx.get_config("port", 10200),
        voice=ctx.get_config("voice", ""),
        timeout=ctx.get_config("timeout", 10),
        mode=ctx.get_config("mode", "pipe"),
        output_format=ctx.get_config("output_format", "mp3"),
        voice_compatible=bool(ctx.get_config("voice_compatible", False)),
    )
    ctx.register_tts_provider(provider)
    _debug(f"Plugin registered: host={provider._host} port={provider._port} "
           f"voice={provider._voice} mode={provider._mode} format={provider._output_format} "
           f"voice_compatible={provider._voice_compatible} debug={_debug_enabled}")
