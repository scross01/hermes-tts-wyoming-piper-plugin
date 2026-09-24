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
import threading
import time
import uuid
import wave
from queue import Empty, Full, Queue
from threading import Event, Thread
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
_FFMPEG_SHUTDOWN_TIMEOUT = 10
_FFMPEG_STREAM_PROCESS_TIMEOUT = 30
_FFMPEG_STREAM_QUEUE_MAX_SIZE = 8
_FFMPEG_STREAM_QUEUE_TIMEOUT = 0.1
_FFMPEG_STREAM_READ_SIZE = 4096
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
        self._client_lock = threading.Lock()
        self._voices: List[Dict[str, Any]] | None = None

    @property
    def name(self) -> str:
        return self._name

    def _get_client(self):
        with self._client_lock:
            if self._client is not None:
                return self._client

            from .wyoming_client import WyomingPiperClient

            self._client = WyomingPiperClient(
                host=self._host,
                port=self._port,
                timeout=self._timeout,
            )
            return self._client

    @staticmethod
    def _first_stream_audio(
        stream_iter: Iterator[Any],
    ) -> tuple[bytes, tuple[int, int, int] | None]:
        for pcm_bytes, audio_format in stream_iter:
            if pcm_bytes:
                return pcm_bytes, audio_format
        raise WyomingServerError("Server returned no audio")

    @staticmethod
    def _close_source_iterator(stream_iter: Any) -> None:
        close_stream = getattr(stream_iter, "close", None)
        if not callable(close_stream):
            return
        try:
            close_stream()
        except (OSError, RuntimeError, ValueError, WyomingError) as error:
            _debug(f"source iterator close error: {error}")

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
        result = client.synthesize_result(text, voice=voice_name)
        if not result.audio:
            raise WyomingServerError("Server returned no audio")
        elapsed = time.monotonic() - t0

        _debug(
            f"[{request_id}] received {len(result.audio)} bytes in {elapsed:.2f}s, "
            f"voice={voice_name}"
        )

        rate, width, channels = result.audio_format
        pcm_bytes = result.audio

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

    @staticmethod
    def _put_with_cancellation(
        queue: Queue[Any],
        item: Any,
        cancelled: Event,
        timeout: float | None = None,
    ) -> bool:
        wait_timeout = (
            _FFMPEG_STREAM_PROCESS_TIMEOUT if timeout is None else timeout
        )
        deadline = time.monotonic() + wait_timeout
        while not cancelled.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                queue.put(
                    item,
                    timeout=min(_FFMPEG_STREAM_QUEUE_TIMEOUT, remaining),
                )
            except Full:
                continue
            return not cancelled.is_set()
        return False

    def _start_output_reader(
        self, stdout: Any, cancelled: Event
    ) -> tuple[Queue[bytes | Exception | None], Thread]:
        """Read incrementally with read1, falling back to a size-bounded read."""
        output_queue: Queue[bytes | Exception | None] = Queue(
            maxsize=_FFMPEG_STREAM_QUEUE_MAX_SIZE
        )
        read = getattr(stdout, "read1", None)
        if not callable(read):
            read = stdout.read

        def _read_output():
            try:
                while not cancelled.is_set():
                    chunk = read(_FFMPEG_STREAM_READ_SIZE)
                    if not chunk:
                        break
                    if not self._put_with_cancellation(output_queue, chunk, cancelled):
                        return
            except (OSError, ValueError) as error:
                self._put_with_cancellation(output_queue, error, cancelled)
            finally:
                self._put_with_cancellation(output_queue, None, cancelled)

        thread = Thread(
            target=_read_output,
            name="ffmpeg-output-reader",
            daemon=True,
        )
        thread.start()
        return output_queue, thread

    @staticmethod
    def _next_output(
        output_queue: Queue[bytes | Exception | None], timeout: float
    ) -> bytes | None:
        try:
            item = output_queue.get(timeout=timeout)
        except Empty as error:
            raise TimeoutError("ffmpeg output read timed out") from error
        if isinstance(item, Exception):
            raise item
        return item

    def _build_ffmpeg_cmd(self, ffmpeg: str, rate: int, width: int, channels: int,
                          target_ext: str, out_path: str) -> List[str]:
        """Build the ffmpeg command list for raw PCM input to target_ext.

        - target_ext "ogg"/"opus" → libopus, 48k, vbr on
        - target_ext "mp3" → libmp3lame
        - target_ext "flac" → flac
        - target_ext "wav"/"pcm" → no extra codec args (PCM WAV is default)
        """
        sample_formats = {1: "u8", 2: "s16le", 4: "s32le"}
        try:
            sample_format = sample_formats[width]
        except KeyError as error:
            raise ValueError(
                f"Unsupported PCM sample width: {width} bytes"
            ) from error

        cmd = [
            ffmpeg, "-y",
            "-f", sample_format,
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
        if out_path == "pipe:1" and target_ext in ("ogg", "opus"):
            cmd.extend(["-f", "ogg"])
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

        cmd = self._build_ffmpeg_cmd(
            ffmpeg, rate, width, channels, target_ext, out_path
        )

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
        try:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError(
                    f"ffmpeg not found; cannot produce '{target_ext}' output. "
                    "Install ffmpeg or request format='wav'."
                )

            out_path = output_path if output_path.endswith(f".{target_ext}") else \
                       output_path.rsplit(".", 1)[0] + f".{target_ext}"
            cmd = self._build_ffmpeg_cmd(
                ffmpeg, rate, width, channels, target_ext, out_path
            )
        except (OSError, RuntimeError, ValueError, WyomingError):
            self._close_source_iterator(pcm_iter)
            raise

        consumed: List[bytes] = []
        source_errors: List[Exception] = []
        writer_errors: List[Exception] = []
        cancelled = Event()
        proc = None
        writer_thread = None
        stderr_file = None
        result = None
        pipe_error = None
        stderr_text = ""

        try:
            stderr_file = tempfile.TemporaryFile()  # noqa: SIM115
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=stderr_file,
            )
            if proc.stdin is None:
                raise RuntimeError("ffmpeg failed to open stdin pipe")

            def _write_source():
                try:
                    for chunk in pcm_iter:
                        if cancelled.is_set():
                            break
                        try:
                            proc.stdin.write(chunk)
                        except (OSError, ValueError) as error:
                            writer_errors.append(error)
                            return
                        consumed.append(chunk)
                except Exception as error:  # noqa: BLE001
                    source_errors.append(error)
                finally:
                    if not cancelled.is_set():
                        try:
                            proc.stdin.flush()
                            proc.stdin.close()
                        except (OSError, ValueError) as error:
                            writer_errors.append(error)

            writer_thread = Thread(
                target=_write_source,
                name="ffmpeg-source-writer",
                daemon=True,
            )
            writer_thread.start()
            try:
                result = proc.wait(timeout=_FFMPEG_STREAM_PROCESS_TIMEOUT)
            except (subprocess.TimeoutExpired, OSError) as error:
                pipe_error = error
        except (FileNotFoundError, OSError) as error:
            pipe_error = error
        finally:
            cancelled.set()
            self._close_source_iterator(pcm_iter)
            if proc is not None:
                self._shutdown_process(proc)
            if writer_thread is not None:
                writer_thread.join(timeout=_FFMPEG_SHUTDOWN_TIMEOUT)
                if writer_thread.is_alive():
                    _debug("ffmpeg source writer did not stop before join timeout")
            if stderr_file is not None:
                try:
                    stderr_file.seek(0)
                    stderr_text = stderr_file.read().decode(
                        "utf-8", errors="replace"
                    )[-500:]
                finally:
                    stderr_file.close()

        if source_errors:
            raise source_errors[0]
        if pipe_error is None and result == 0 and not writer_errors:
            _debug(f"[{request_id}] piped PCM → {target_ext}: {out_path}")
            return out_path
        if pipe_error is None and result == 0 and writer_errors:
            raise RuntimeError(
                f"ffmpeg input writer failed: {writer_errors[0]}"
            ) from writer_errors[0]

        if pipe_error is not None:
            _debug(f"[{request_id}] ffmpeg failed: {pipe_error}")
        else:
            _debug(f"[{request_id}] ffmpeg error: {stderr_text}")

        wav_path = output_path.rsplit(".", 1)[0] + ".wav"
        self._write_wav(b"".join(consumed), rate, width, channels, wav_path)
        logger.warning("ffmpeg failed; wrote fallback WAV: %s", wav_path)
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
        """Close stdin and reap the process with bounded independent cleanup."""
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError) as error:
            _debug(f"ffmpeg stdin cleanup error: {error}")

        try:
            proc.wait(timeout=_FFMPEG_SHUTDOWN_TIMEOUT)
            return
        except subprocess.TimeoutExpired:
            _debug("ffmpeg did not exit within timeout, killing")
        except (OSError, ValueError) as error:
            _debug(f"ffmpeg wait error: {error}")
            try:
                if proc.poll() is not None:
                    return
            except (OSError, ValueError):
                pass

        try:
            proc.kill()
        except (OSError, ValueError) as error:
            _debug(f"ffmpeg kill error: {error}")

        try:
            proc.wait(timeout=_FFMPEG_SHUTDOWN_TIMEOUT)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            _debug(f"ffmpeg final reap error: {error}")

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
        first_chunk, audio_fmt = self._first_stream_audio(stream_iter)
        if audio_fmt is None:
            audio_fmt = (22050, 2, 1)
        rate, width, channels = audio_fmt

        def pcm_iter():
            yield first_chunk
            for pcm_bytes, fmt_info in stream_iter:
                if fmt_info is not None and fmt_info != audio_fmt:
                    raise WyomingServerError("Synthesis stream format changed")
                yield pcm_bytes

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
            first_chunk, fmt_info = self._first_stream_audio(stream_iter)

            if fmt_info is not None:
                rate, width, channels = fmt_info
            else:
                rate, width, channels = 22050, 2, 1

            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError(
                    "ffmpeg not found; cannot produce 'opus' output. "
                    "Install ffmpeg or use mode='pipe' with format='wav'."
                )

            cmd = self._build_ffmpeg_cmd(
                ffmpeg, rate, width, channels, "opus", "pipe:1"
            )
        except (OSError, RuntimeError, ValueError, WyomingError):
            self._close_source_iterator(stream_iter)
            raise

        try:
            stderr_file = tempfile.TemporaryFile()  # noqa: SIM115
        except (OSError, RuntimeError, ValueError):
            self._close_source_iterator(stream_iter)
            raise
        proc = None
        writer_thread = None
        reader_thread = None
        output_queue = None
        writer_cancelled = Event()
        reader_cancelled = Event()
        writer_errors: List[Exception] = []
        completed = False
        stderr_text = ""

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
            )
            if proc.stdin is None or proc.stdout is None:
                raise RuntimeError("ffmpeg failed to open pipes")

            output_queue, reader_thread = self._start_output_reader(
                proc.stdout, reader_cancelled
            )
            write_queue: Queue[bytes | None] = Queue(
                maxsize=_FFMPEG_STREAM_QUEUE_MAX_SIZE
            )

            def _write_to_ffmpeg():
                try:
                    while not writer_cancelled.is_set():
                        try:
                            data = write_queue.get(
                                timeout=_FFMPEG_STREAM_QUEUE_TIMEOUT
                            )
                        except Empty:
                            continue
                        if data is None:
                            proc.stdin.flush()
                            return
                        proc.stdin.write(data)
                        proc.stdin.flush()
                        if writer_cancelled.is_set():
                            return
                except (OSError, ValueError) as error:
                    writer_errors.append(error)
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass

            writer_thread = Thread(
                target=_write_to_ffmpeg,
                name="ffmpeg-input-writer",
                daemon=True,
            )
            writer_thread.start()
            reader_finished = False

            def _drain_input_backpressure():
                nonlocal reader_finished
                if not write_queue.full():
                    return
                try:
                    output = output_queue.get(timeout=_FFMPEG_STREAM_QUEUE_TIMEOUT)
                except Empty:
                    return
                if isinstance(output, Exception):
                    raise output
                if output is None:
                    reader_finished = True
                else:
                    yield output

            if not self._put_with_cancellation(
                write_queue, first_chunk, writer_cancelled
            ):
                raise TimeoutError("ffmpeg input queue timed out")

            for pcm_bytes, fmt_info in stream_iter:
                if fmt_info is not None and fmt_info != (rate, width, channels):
                    raise WyomingServerError("Synthesis stream format changed")
                yield from _drain_input_backpressure()
                if not self._put_with_cancellation(
                    write_queue, pcm_bytes, writer_cancelled
                ):
                    raise TimeoutError("ffmpeg input queue timed out")
                if writer_errors:
                    raise RuntimeError(
                        f"ffmpeg input writer failed: {writer_errors[0]}"
                    ) from writer_errors[0]
                yield from _drain_input_backpressure()

            if not self._put_with_cancellation(
                write_queue, None, writer_cancelled
            ):
                raise TimeoutError("ffmpeg input queue timed out")
            writer_thread.join(timeout=_FFMPEG_SHUTDOWN_TIMEOUT)
            if writer_thread.is_alive():
                raise TimeoutError("ffmpeg input writer did not stop")
            if writer_errors:
                raise RuntimeError(
                    f"ffmpeg input writer failed: {writer_errors[0]}"
                ) from writer_errors[0]

            try:
                proc.stdin.close()
            except (OSError, ValueError) as error:
                raise RuntimeError(
                    f"ffmpeg input close failed: {error}"
                ) from error

            try:
                proc.wait(timeout=_FFMPEG_STREAM_PROCESS_TIMEOUT)
            except subprocess.TimeoutExpired as error:
                raise TimeoutError("ffmpeg process timed out") from error

            final_chunk = None
            drain_deadline = time.monotonic() + _FFMPEG_STREAM_PROCESS_TIMEOUT
            while not reader_finished:
                remaining = drain_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("ffmpeg output drain timed out")
                output = self._next_output(output_queue, remaining)
                if output is None:
                    reader_finished = True
                    break
                if final_chunk is not None:
                    completed = True
                    yield final_chunk
                final_chunk = output

            completed = True
            if final_chunk is not None:
                yield final_chunk
        finally:
            writer_cancelled.set()
            self._close_source_iterator(stream_iter)
            reader_cancelled.set()

            if proc is not None:
                self._shutdown_process(proc)
            if proc is not None and proc.stdout is not None:
                try:
                    proc.stdout.close()
                except (OSError, ValueError):
                    pass
            if writer_thread is not None:
                writer_thread.join(timeout=_FFMPEG_SHUTDOWN_TIMEOUT)
                if writer_thread.is_alive():
                    _debug("ffmpeg input writer did not stop during cleanup")
            if reader_thread is not None:
                reader_thread.join(timeout=_FFMPEG_SHUTDOWN_TIMEOUT)
                if reader_thread.is_alive():
                    _debug("ffmpeg output reader did not stop during cleanup")

            try:
                stderr_file.seek(0)
                stderr_text = stderr_file.read().decode(
                    "utf-8", errors="replace"
                )[-500:]
            finally:
                stderr_file.close()

            if completed and proc is not None and proc.returncode not in (0, None):
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
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.disconnect()
            except WyomingError as e:
                logger.debug("Disconnect failed during release: %s", e)

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
