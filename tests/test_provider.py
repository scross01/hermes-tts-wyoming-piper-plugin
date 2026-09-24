import itertools
import logging
import os
import subprocess
import sys
import tempfile
import threading
import wave
from unittest.mock import MagicMock, call, patch

import pytest

from wyoming_client import (
    SynthesisResult,
    WyomingError,
    WyomingServerError,
    WyomingVoice,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.conftest import _load_wyoming_piper

WyomingPiperProvider = _load_wyoming_piper().WyomingPiperProvider


@pytest.fixture(autouse=True)
def _no_real_ffmpeg(monkeypatch):
    """Keep the unit suite hermetic: resolve ffmpeg via a fake binary path.

    These tests mock subprocess.Popen/run themselves; what they must never do
    is depend on ffmpeg being installed on the machine running the tests
    (the plan-013 doctrine: CI has no ffmpeg). Tests that specifically cover
    the not-found path patch shutil.which with None after this fixture runs,
    and monkeypatch.setattr defers to per-test patches set inside the test
    body, so those keep working.
    """
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)


class TestWyomingPiperProvider:
    def test_name(self):
        p = WyomingPiperProvider()
        assert p.name == "wyoming-piper"

    @pytest.mark.parametrize(
        "fmt,expected",
        [
            ("mp3", "mp3"),
            ("ogg", "ogg"),
            ("opus", "ogg"),
            ("wav", "wav"),
            ("pcm", "wav"),
            ("flac", "flac"),
            ("unknown", "mp3"),
        ],
    )
    def test_target_extension(self, fmt, expected):
        p = WyomingPiperProvider()
        assert p._target_extension(fmt) == expected

    @pytest.mark.parametrize("width,expected_input_format", [
        (1, "u8"),
        (2, "s16le"),
        (4, "s32le"),
    ])
    @pytest.mark.parametrize("target_ext,expected_codec_args", [
        ("ogg", ["-acodec", "libopus", "-b:a", "48k", "-vbr", "on"]),
        ("opus", ["-acodec", "libopus", "-b:a", "48k", "-vbr", "on"]),
        ("mp3", ["-acodec", "libmp3lame"]),
        ("flac", ["-acodec", "flac"]),
        ("wav", []),
        ("pcm", []),
    ])
    def test_build_ffmpeg_cmd(self, width, expected_input_format, target_ext,
                              expected_codec_args):
        p = WyomingPiperProvider()
        cmd = p._build_ffmpeg_cmd(
            "/usr/bin/ffmpeg", 22050, width, 1, target_ext, "/tmp/out"
        )
        assert cmd[0] == "/usr/bin/ffmpeg"
        assert cmd[cmd.index("-f") + 1] == expected_input_format
        for arg in expected_codec_args:
            assert arg in cmd
        assert cmd[-1] == "/tmp/out"

    def test_build_ffmpeg_rejects_unsupported_width(self):
        p = WyomingPiperProvider()
        with pytest.raises(ValueError, match="Unsupported PCM sample width: 3 bytes"):
            p._build_ffmpeg_cmd(
                "/usr/bin/ffmpeg", 22050, 3, 1, "mp3", "/tmp/out"
            )

    def test_build_ffmpeg_cmd_adds_ogg_pipe_muxer(self):
        p = WyomingPiperProvider()
        cmd = p._build_ffmpeg_cmd(
            "/usr/bin/ffmpeg", 22050, 2, 1, "opus", "pipe:1"
        )
        assert cmd.index("-f", cmd.index("pipe:0")) < cmd.index("pipe:1")
        assert cmd[cmd.index("-f", cmd.index("pipe:0")) + 1] == "ogg"

    def test_voice_compatible_default_false(self):
        p = WyomingPiperProvider()
        assert p.voice_compatible is False

    def test_voice_compatible_opt_in(self):
        p = WyomingPiperProvider(voice_compatible=True)
        assert p.voice_compatible is True

    def test_write_wav_roundtrip(self):
        p = WyomingPiperProvider()
        pcm = b"\x00\x00" * 100  # 100 samples of silence
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            p._write_wav(pcm, 22050, 2, 1, path)
            with wave.open(path, "rb") as wf:
                assert wf.getnchannels() == 1
                assert wf.getsampwidth() == 2
                assert wf.getframerate() == 22050
                assert wf.readframes(wf.getnframes()) == pcm
        finally:
            os.unlink(path)

    def test_default_voice_returns_configured_voice(self):
        p = WyomingPiperProvider(voice="en_US-lessac-medium")
        assert p.default_voice() == "en_US-lessac-medium"

    def test_default_voice_returns_first_from_list_voices(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.describe.return_value = [
            WyomingVoice(name="v1", languages=["en"]),
            WyomingVoice(name="v2", languages=["fr"]),
        ]
        with patch.object(p, "_get_client", return_value=mock_client):
            assert p.default_voice() == "v1"

    def test_default_voice_returns_none_when_no_voices(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.describe.return_value = []
        with patch.object(p, "_get_client", return_value=mock_client):
            assert p.default_voice() is None

    def test_list_voices_returns_empty_on_failure(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.describe.side_effect = WyomingError("connection failed")
        with patch.object(p, "_get_client", return_value=mock_client):
            assert p.list_voices() == []

    def test_concurrent_client_init_constructs_one_client(self):
        p = WyomingPiperProvider()
        barrier = threading.Barrier(2)
        result_lock = threading.Lock()
        results = []
        errors = []
        constructed = []

        def construct_client(**kwargs):
            client = MagicMock(**kwargs)
            with result_lock:
                constructed.append(client)
            return client

        def get_client():
            try:
                barrier.wait(timeout=1)
                client = p._get_client()
                with result_lock:
                    results.append(client)
            except Exception as error:  # noqa: BLE001
                with result_lock:
                    errors.append(error)

        threads = [threading.Thread(target=get_client) for _ in range(2)]
        with patch("wyoming_client.WyomingPiperClient", side_effect=construct_client) as constructor:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=1)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert constructor.call_count == 1
        assert len(constructed) == 1
        assert results[0] is results[1]

    def test_synthesize_stream_to_file_pipes_incrementally(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([
            (b"chunk1", (22050, 2, 1)),
            (b"chunk2", None),
            (b"chunk3", None),
        ])

        captured_iterators = []

        def fake_pipe_stream(request_id, pcm_iter, rate, width, channels, output_path, target_ext):
            captured_iterators.append(list(pcm_iter))
            return output_path

        with patch.object(p, "_get_client", return_value=mock_client), \
             patch.object(p, "_pipe_stream_to_format", side_effect=fake_pipe_stream):
            p._synthesize_stream_to_file(1, "hello", "/tmp/out.mp3", format="mp3")

        assert len(captured_iterators) == 1
        assert captured_iterators[0] == [b"chunk1", b"chunk2", b"chunk3"]


class TestRequestId:
    def test_synthesize_uses_uuid_request_id(self):
        fixed_uuid = "12345678-1234-1234-1234-123456789abc"
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_result.return_value = SynthesisResult(
            b"PCM", (22050, 2, 1)
        )
        p._client = mock_client

        with patch("uuid.uuid4", return_value=fixed_uuid), \
             patch("tts_wyoming_piper._debug") as mock_debug:
            p.synthesize("hello", "/tmp/out.mp3")

        first_call_arg = mock_debug.call_args[0][0]
        assert fixed_uuid in first_call_arg


class TestPipeModeRawPcm:
    @staticmethod
    def _provider_with_client(pcm, fmt=(22050, 2, 1)):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_result.return_value = SynthesisResult(pcm, fmt)
        p._client = mock_client
        return p

    def test_pipe_empty_audio_raises_before_output(self, tmp_path):
        p = self._provider_with_client(b"")
        with patch.object(p, "_write_wav") as mock_wav, \
             patch.object(p, "_pipe_pcm_to_format") as mock_pipe, \
             pytest.raises(WyomingServerError, match="Server returned no audio"):
            p._synthesize_pipe(
                "req1", "hello", str(tmp_path / "out.wav"),
                voice="v", format="wav",
            )
        mock_wav.assert_not_called()
        mock_pipe.assert_not_called()

    def test_concurrent_pipe_formats_stay_atomic(self, tmp_path):
        p = WyomingPiperProvider(voice="v")
        mock_client = MagicMock()
        barrier = threading.Barrier(2)
        errors = []

        def synthesize_result(text, voice=None):
            barrier.wait(timeout=1)
            rate = 16000 if text == "low" else 22050
            return SynthesisResult(b"\x00\x00" * 20, (rate, 2, 1))

        mock_client.synthesize_result.side_effect = synthesize_result
        p._client = mock_client

        def synthesize(text, output_name):
            try:
                p._synthesize_pipe(
                    "req", text, str(tmp_path / output_name),
                    voice="v", format="wav",
                )
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [
            threading.Thread(target=synthesize, args=("low", "low.wav")),
            threading.Thread(target=synthesize, args=("high", "high.wav")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        with wave.open(str(tmp_path / "low.wav"), "rb") as wav_file:
            assert wav_file.getframerate() == 16000
        with wave.open(str(tmp_path / "high.wav"), "rb") as wav_file:
            assert wav_file.getframerate() == 22050

    def test_pipe_mp3_feeds_raw_pcm_to_ffmpeg(self, tmp_path):
        p = self._provider_with_client(b"RAWPCM")
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = p._synthesize_pipe("req1", "hello", str(tmp_path / "out.mp3"),
                                     voice="v", format="mp3")
        assert out == str(tmp_path / "out.mp3")
        assert mock_run.call_args.kwargs["input"] == b"RAWPCM"  # regression: was WAV-with-header

    def test_pipe_wav_wraps_pcm_in_wav_header(self, tmp_path):
        p = self._provider_with_client(b"\x00\x00" * 50)
        out = p._synthesize_pipe("req1", "hello", str(tmp_path / "out.wav"),
                                 voice="v", format="wav")
        with wave.open(out, "rb") as wf:
            assert wf.getframerate() == 22050
            assert wf.readframes(wf.getnframes()) == b"\x00\x00" * 50

    def test_pipe_pcm_request_produces_valid_single_wrap_wav(self, tmp_path):
        # format="pcm" maps to target_ext "wav" (see _target_extension), so the
        # output is a valid single-wrap WAV, not raw bytes.
        p = self._provider_with_client(b"\x00\x00" * 50)
        out = p._synthesize_pipe("req1", "hello", str(tmp_path / "out.wav"),
                                 voice="v", format="pcm")
        with wave.open(out, "rb") as wf:
            assert wf.getframerate() == 22050
            assert wf.readframes(wf.getnframes()) == b"\x00\x00" * 50

    def test_stream_wav_path_gets_wav_header(self, tmp_path):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([
            (b"\x00\x00" * 10, (22050, 2, 1)),
            (b"\x00\x00" * 10, None),
        ])
        with patch.object(p, "_get_client", return_value=mock_client):
            out = p._synthesize_stream_to_file("req1", "hello", str(tmp_path / "out.wav"),
                                               voice="v", format="wav")
        with wave.open(out, "rb") as wf:
            assert wf.readframes(wf.getnframes()) == b"\x00\x00" * 20


class TestStreamLifecycle:
    def _streamable_provider(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter(
            [(b"in1", (22050, 2, 1))] + [(b"in2", None), (b"in3", None)]
        )
        p._client = mock_client
        return p

    def _proc_mock(self):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.read1.side_effect = [
            b"out1", b"out2", b"out3", b"", b""
        ]
        proc.wait = MagicMock(return_value=0)
        proc.kill = MagicMock()
        proc.returncode = 0
        return proc

    def test_early_close_signals_writer_and_reaps(self):
        """CORRECTNESS-03: consuming one chunk then close() must signal the
        writer and reap the process."""
        p = self._streamable_provider()
        proc = self._proc_mock()
        with patch("subprocess.Popen", return_value=proc):
            gen = p.stream("hello")
            first = next(gen)
            assert first == b"out1"
            gen.close()
        assert proc.stdin.close.called  # reaped / stdin shut

    def test_writer_thread_terminates_on_early_close(self):
        """The writer must not survive the generator being closed: after close(),
        a bounded join must suffice (no thread left blocked on the queue)."""
        p = self._streamable_provider()
        proc = self._proc_mock()
        with patch("subprocess.Popen", return_value=proc):
            gen = p.stream("hello")
            next(gen)
            gen.close()
        # writer_thread is a local; we assert indirectly: no crash + proc reaped.
        assert proc.stdin.close.called

    def test_stream_slow_source_waits_for_later_chunk(self):
        p = WyomingPiperProvider()
        source_paused = threading.Event()
        release_source = threading.Event()
        results = []
        errors = []
        workers = []
        real_thread = threading.Thread

        def source():
            yield b"in1", (22050, 2, 1)
            source_paused.set()
            release_source.wait(timeout=1)
            yield b"in2", None

        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = source()
        p._client = mock_client
        proc = self._proc_mock()

        def make_thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            workers.append(worker)
            return worker

        def consume():
            try:
                results.extend(p.stream("hello"))
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        consumer = real_thread(target=consume)
        with patch("subprocess.Popen", return_value=proc), \
             patch("tts_wyoming_piper.Thread", side_effect=make_thread):
            consumer.start()
            try:
                assert source_paused.wait(timeout=1)
                writer = next(
                    worker for worker in workers
                    if worker.name == "ffmpeg-input-writer"
                )
                assert writer.is_alive()
                release_source.set()
                consumer.join(timeout=1)
            finally:
                release_source.set()
                consumer.join(timeout=1)
                for worker in workers:
                    worker.join(timeout=1)

        assert not consumer.is_alive()
        assert errors == []
        assert not writer.is_alive()
        assert proc.stdin.write.call_args_list == [call(b"in1"), call(b"in2")]

    def test_stream_full_input_queue_has_bounded_deadline(self):
        p = self._streamable_provider()
        p._client.synthesize_stream.return_value = iter(
            [(b"in1", (22050, 2, 1))]
            + [(f"in{index}", None) for index in range(2, 32)]
        )
        write_started = threading.Event()
        release_write = threading.Event()
        read_started = threading.Event()
        release_read = threading.Event()
        workers = []
        real_thread = threading.Thread
        write_count = 0
        errors = []

        def blocked_write(_chunk):
            nonlocal write_count
            write_count += 1
            if write_count == 2:
                write_started.set()
                release_write.wait(timeout=1)

        def blocked_read(_size):
            read_started.set()
            release_read.wait(timeout=1)
            return b""

        proc = self._proc_mock()
        proc.stdin.write.side_effect = blocked_write
        proc.stdout.read1.side_effect = blocked_read
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10),
            0,
        ]
        proc.kill.side_effect = lambda: (
            release_write.set(),
            release_read.set(),
        )

        def make_thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            workers.append(worker)
            return worker

        def consume():
            try:
                list(p.stream("hello"))
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        consumer = real_thread(target=consume)
        with patch("subprocess.Popen", return_value=proc), \
             patch("tts_wyoming_piper.Thread", side_effect=make_thread), \
             patch("tts_wyoming_piper._FFMPEG_STREAM_PROCESS_TIMEOUT", 0.05), \
             patch("tts_wyoming_piper._FFMPEG_SHUTDOWN_TIMEOUT", 0.05):
            consumer.start()
            try:
                assert write_started.wait(timeout=1)
                assert read_started.wait(timeout=1)
                consumer.join(timeout=0.5)
            finally:
                release_write.set()
                release_read.set()
                consumer.join(timeout=1)
                for worker in workers:
                    worker.join(timeout=1)

        assert not consumer.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], TimeoutError)
        assert "input queue timed out" in str(errors[0])
        proc.kill.assert_called_once()
        assert proc.wait.call_count == 2
        assert all(not worker.is_alive() for worker in workers)

    def test_stream_close_joins_blocked_writer(self):
        p = WyomingPiperProvider()
        write_started = threading.Event()
        release_write = threading.Event()
        workers = []
        real_thread = threading.Thread

        def blocked_write(_chunk):
            write_started.set()
            release_write.wait(timeout=1)

        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter(
            [(b"in1", (22050, 2, 1))]
            + [(f"in{index}", None) for index in range(2, 12)]
        )
        p._client = mock_client
        proc = self._proc_mock()
        proc.stdin.write.side_effect = blocked_write

        def make_thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            workers.append(worker)
            return worker

        errors = []
        generator = p.stream("hello")

        def close_stream():
            try:
                generator.close()
            except Exception as error:  # noqa: BLE001
                errors.append(error)
            else:
                errors.append(None)

        with patch("subprocess.Popen", return_value=proc), \
             patch("tts_wyoming_piper.Thread", side_effect=make_thread):
            try:
                assert next(generator) == b"out1"
                assert write_started.wait(timeout=1)
            finally:
                closer = real_thread(target=close_stream)
                closer.start()
                release_write.set()
                closer.join(timeout=1)
                generator.close()
                for worker in workers:
                    worker.join(timeout=1)

        assert not closer.is_alive()
        assert errors == [None]
        assert proc.stdin.close.called
        assert proc.wait.called
        assert all(not worker.is_alive() for worker in workers)

    def test_stream_to_file_rejects_format_change_before_output(self, tmp_path):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([
            (b"first", (16000, 2, 1)),
            (b"second", (22050, 2, 1)),
        ])
        p._client = mock_client
        with patch.object(p, "_write_wav") as mock_wav, \
             pytest.raises(WyomingServerError, match="stream format changed"):
            p._synthesize_stream_to_file(
                "req", "hello", str(tmp_path / "out.wav"),
                voice="v", format="wav",
            )
        mock_wav.assert_not_called()

    def test_direct_stream_rejects_format_change(self):
        p = self._streamable_provider()
        p._client.synthesize_stream.return_value = iter([
            (b"first", (16000, 2, 1)),
            (b"second", (22050, 2, 1)),
        ])
        proc = self._proc_mock()
        with patch("subprocess.Popen", return_value=proc), \
             pytest.raises(WyomingServerError, match="stream format changed"):
            list(p.stream("hello"))
        assert proc.stdin.close.called

    def test_source_iterator_exception_propagates_from_stream(self):
        """A WyomingServerError from the wyoming client mid-stream must reach
        the consumer while the process is still reaped."""
        p = WyomingPiperProvider()
        mock_client = MagicMock()

        def _raise():
            raise WyomingServerError("Synthesis error: mid")

        stream_iter = itertools.chain(
            iter([(b"in1", (22050, 2, 1)), (b"in2", None)]),
            (_raise() for _ in range(1)),
        )
        mock_client.synthesize_stream.return_value = stream_iter
        p._client = mock_client

        proc = self._proc_mock()
        with patch("subprocess.Popen", return_value=proc), \
             pytest.raises(WyomingServerError, match="Synthesis error: mid"):
            list(p.stream("hello"))
        assert proc.stdin.close.called


class TestConfigValidation:
    def test_unknown_mode_warns_and_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            p = WyomingPiperProvider(mode="straming")
        assert p._mode == "pipe"
        assert "unknown mode 'straming'" in caplog.text

    def test_unknown_output_format_warns_and_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            p = WyomingPiperProvider(output_format="og")
        assert p._output_format == "mp3"
        assert "unknown output_format 'og'" in caplog.text

    def test_valid_values_do_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="hermes-wyoming-piper"):
            p = WyomingPiperProvider(mode="stream", output_format="flac")
        assert p._mode == "stream"
        assert p._output_format == "flac"
        assert "unknown" not in caplog.text

    def test_values_are_case_and_whitespace_insensitive(self, caplog):
        with caplog.at_level(logging.WARNING, logger="hermes-wyoming-piper"):
            p = WyomingPiperProvider(mode="  Pipe ", output_format="MP3")
        assert p._mode == "pipe"
        assert p._output_format == "mp3"
        assert "unknown" not in caplog.text

    def test_empty_strings_fall_back_without_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="hermes-wyoming-piper"):
            p = WyomingPiperProvider(mode="", output_format="")
        assert p._mode == "pipe"
        assert p._output_format == "mp3"
        assert "unknown" not in caplog.text


class TestOutputFormatIsUsed:
    @staticmethod
    def _provider_with_client(configured_format):
        p = WyomingPiperProvider(output_format=configured_format)
        mock_client = MagicMock()
        mock_client.synthesize_result.return_value = SynthesisResult(
            b"RAWPCM", (22050, 2, 1)
        )
        p._client = mock_client
        return p

    def test_configured_format_used_when_caller_omits_format(self, tmp_path):
        """CORRECTNESS-05: output_format=flac must produce flac when the
        caller does not pass format explicitly."""
        p = self._provider_with_client("flac")
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = p.synthesize("hello", str(tmp_path / "out.mp3"))
        assert out == str(tmp_path / "out.flac")

    def test_explicit_caller_format_still_wins(self, tmp_path):
        p = self._provider_with_client("flac")
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = p.synthesize("hello", str(tmp_path / "out.mp3"), format="mp3")
        assert out == str(tmp_path / "out.mp3")

    def test_default_output_format_is_mp3(self, tmp_path):
        p = self._provider_with_client("mp3")
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out = p.synthesize("hello", str(tmp_path / "out.mp3"))
        assert out == str(tmp_path / "out.mp3")

    def test_stream_mode_uses_configured_format_when_omitted(self, tmp_path):
        p = WyomingPiperProvider(mode="stream", output_format="wav")
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([
            (b"\x00\x00" * 10, (22050, 2, 1)),
            (b"\x00\x00" * 10, None),
        ])
        with patch.object(p, "_get_client", return_value=mock_client):
            # synthesize() resolves the omitted format from output_format and
            # dispatches to the stream-to-file path with format="wav".
            out = p.synthesize("hello", str(tmp_path / "out.mp3"))
        with wave.open(out, "rb") as wf:
            assert wf.getframerate() == 22050


class TestStreamHangFix:
    def test_stream_kills_ffmpeg_on_timeout(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([(b"\x00\x00" * 100, (22050, 2, 1))])
        p._client = mock_client

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read1.side_effect = [b"", b""]
        # Plan 014: the wait calls now live in _shutdown_process —
        # wait #1 times out (shutdown timeout), kill fires, wait #2 times
        # out (post-kill), wait #3 succeeds. The kill assertion is the
        # plan-003 contract and stays invariable.
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10),
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10),
            0,
        ]
        mock_proc.kill = MagicMock()
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc), \
             pytest.raises(TimeoutError, match="ffmpeg process timed out"):
            list(p.stream("hello"))

        mock_proc.kill.assert_called_once()


class TestStreamOutputReads:
    def test_stream_uses_incremental_read1(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([
            (b"in1", (22050, 2, 1)),
        ])
        p._client = mock_client
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.read1.side_effect = [b"out1", b""]
        proc.wait.return_value = 0
        proc.returncode = 0

        with patch("subprocess.Popen", return_value=proc):
            chunks = list(p.stream("hello"))

        assert chunks == [b"out1"]
        assert proc.stdout.read1.call_count == 2
        proc.stdout.read.assert_not_called()

    def test_stream_blocked_read_reaches_cleanup(self):
        p = WyomingPiperProvider()
        read_started = threading.Event()
        release_read = threading.Event()
        workers = []
        real_thread = threading.Thread

        def blocked_read(_size):
            read_started.set()
            release_read.wait(timeout=1)
            return b""

        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([
            (b"in1", (22050, 2, 1)),
        ])
        p._client = mock_client
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.read1.side_effect = blocked_read
        proc.wait.side_effect = [
            0,
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10),
            0,
        ]
        proc.returncode = 0
        errors = []

        def make_thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            workers.append(worker)
            return worker

        def consume():
            try:
                list(p.stream("hello"))
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        consumer = real_thread(target=consume)
        with patch("subprocess.Popen", return_value=proc), \
             patch("tts_wyoming_piper.Thread", side_effect=make_thread), \
             patch("tts_wyoming_piper._FFMPEG_STREAM_PROCESS_TIMEOUT", 0.05), \
             patch("tts_wyoming_piper._FFMPEG_SHUTDOWN_TIMEOUT", 0.01):
            consumer.start()
            try:
                assert read_started.wait(timeout=1)
                consumer.join(timeout=0.5)
            finally:
                release_read.set()
                consumer.join(timeout=1)
                for worker in workers:
                    worker.join(timeout=1)

        assert not consumer.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], TimeoutError)
        assert "timed out" in str(errors[0])
        proc.kill.assert_called_once()
        assert all(not worker.is_alive() for worker in workers)


class TestPipeStreamFallback:
    @staticmethod
    def _mock_proc(returncode=1):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        proc.wait = MagicMock(return_value=returncode)
        return proc

    def test_fallback_wav_contains_all_fed_audio(self, tmp_path):
        p = WyomingPiperProvider()
        chunks = [b"aa", b"bb", b"cc"]
        proc = self._mock_proc(returncode=1)
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(p, "_write_wav") as mock_wav:
            out = p._pipe_stream_to_format(
                "req1", iter(chunks), 22050, 2, 1,
                str(tmp_path / "out.mp3"), "mp3",
            )
        assert out == str(tmp_path / "out.wav")
        mock_wav.assert_called_once()
        assert mock_wav.call_args.args[0] == b"aabbcc"  # regression: was b"" (exhausted iterator)

    def test_fallback_on_broken_pipe_contains_partial_audio(self, tmp_path):
        p = WyomingPiperProvider()
        proc = self._mock_proc()
        proc.stdin.write.side_effect = [None, BrokenPipeError("dead"), None]
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(p, "_write_wav") as mock_wav:
            p._pipe_stream_to_format(
                "req1", iter([b"aa", b"bb", b"cc"]), 22050, 2, 1,
                str(tmp_path / "out.mp3"), "mp3",
            )
        assert mock_wav.call_args.args[0] == b"aa"  # best available: what was fed
        # Plan 014: cleanup is _shutdown_process — kill only when the bounded
        # wait times out; here (mock wait succeeds) the process is simply reaped.
        proc.wait.assert_called()

    def test_success_returns_out_path_without_fallback(self, tmp_path):
        p = WyomingPiperProvider()
        proc = self._mock_proc(returncode=0)
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(p, "_write_wav") as mock_wav:
            out = p._pipe_stream_to_format(
                "req1", iter([b"aa"]), 22050, 2, 1,
                str(tmp_path / "out.mp3"), "mp3",
            )
        assert out == str(tmp_path / "out.mp3")
        mock_wav.assert_not_called()

    def test_file_stream_does_not_use_stderr_pipe(self, tmp_path):
        p = WyomingPiperProvider()
        proc = self._mock_proc(returncode=0)
        with patch("subprocess.Popen", return_value=proc) as popen:
            p._pipe_stream_to_format(
                "req1", iter([b"aa"]), 22050, 2, 1,
                str(tmp_path / "out.mp3"), "mp3",
            )
        assert popen.call_args.kwargs["stderr"] != subprocess.PIPE
        assert popen.call_args.kwargs["stdout"] == subprocess.DEVNULL

    def test_file_stream_closes_source_on_failure(self, tmp_path):
        p = WyomingPiperProvider()
        source_closed = threading.Event()
        write_started = threading.Event()
        release_write = threading.Event()

        def source():
            try:
                yield b"aa"
                yield b"bb"
            finally:
                source_closed.set()

        def blocked_write(_chunk):
            write_started.set()
            release_write.wait(timeout=1)

        proc = self._mock_proc(returncode=1)
        proc.stdin.write.side_effect = blocked_write
        proc.stdin.close.side_effect = release_write.set
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(p, "_write_wav") as mock_wav:
            out = p._pipe_stream_to_format(
                "req1", source(), 22050, 2, 1,
                str(tmp_path / "out.mp3"), "mp3",
            )

        assert write_started.wait(timeout=1)
        assert source_closed.is_set()
        assert out == str(tmp_path / "out.wav")
        mock_wav.assert_called_once()
        proc.wait.assert_called()

    def test_writer_failure_is_visible_on_zero_returncode(self, tmp_path):
        p = WyomingPiperProvider()
        proc = self._mock_proc(returncode=0)
        proc.stdin.write.side_effect = BrokenPipeError("dead")
        with patch("subprocess.Popen", return_value=proc), \
             patch.object(p, "_write_wav") as mock_wav, \
             pytest.raises(RuntimeError, match="ffmpeg input writer failed"):
            p._pipe_stream_to_format(
                "req1", iter([b"aa"]), 22050, 2, 1,
                str(tmp_path / "out.mp3"), "mp3",
            )
        mock_wav.assert_not_called()

    def test_source_exception_propagates_and_reaps_process(self, tmp_path):
        """CORRECTNESS-01: WyomingServerError from the iterator must propagate
        (never become a fallback WAV) and ffmpeg must be reaped."""
        p = WyomingPiperProvider()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stderr = MagicMock()

        def boom():
            yield b"aa"
            raise WyomingServerError("Synthesis error: voice not found")

        with patch("subprocess.Popen", return_value=proc), \
             patch.object(p, "_write_wav") as mock_wav, \
             patch.object(p, "_shutdown_process", wraps=p._shutdown_process) as mock_shutdown, \
             pytest.raises(WyomingServerError):
            p._pipe_stream_to_format(
                "req1", boom(), 22050, 2, 1,
                str(tmp_path / "out.mp3"), "mp3",
            )
        mock_wav.assert_not_called()
        mock_shutdown.assert_called_once()

    def test_source_exception_leaves_no_running_process(self, tmp_path):
        """The finally must reap even when the iterator raises before completion."""
        p = WyomingPiperProvider()
        proc = MagicMock()
        proc.stdin = MagicMock()

        def boom():
            yield b"aa"
            raise OSError("disk gone")

        with patch("subprocess.Popen", return_value=proc), \
             patch.object(p, "_shutdown_process") as mock_shutdown, \
             pytest.raises(OSError, match="disk gone"):
            p._pipe_stream_to_format(
                "req1", boom(), 22050, 2, 1,
                str(tmp_path / "out.mp3"), "mp3",
            )
        mock_shutdown.assert_called_once()


class TestProcessShutdown:
    def test_shutdown_broken_stdin_close_still_waits_and_kills(self):
        p = WyomingPiperProvider()
        proc = MagicMock()
        proc.stdin.close.side_effect = BrokenPipeError("closed")
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10),
            0,
        ]

        p._shutdown_process(proc)

        assert proc.wait.call_count == 2
        proc.kill.assert_called_once_with()

    def test_shutdown_kill_error_still_attempts_final_wait(self):
        p = WyomingPiperProvider()
        proc = MagicMock()
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10),
            0,
        ]
        proc.kill.side_effect = OSError("already gone")

        p._shutdown_process(proc)

        assert proc.wait.call_count == 2

    def test_shutdown_treats_polled_exit_as_reaped(self):
        p = WyomingPiperProvider()
        proc = MagicMock()
        proc.wait.side_effect = OSError("wait failed")
        proc.poll.return_value = 0

        p._shutdown_process(proc)

        proc.kill.assert_not_called()


class TestStreamFfmpegFailure:
    def test_stream_raises_on_nonzero_returncode(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([(b"in1", (22050, 2, 1))])
        p._client = mock_client

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read1.side_effect = [b"out1", b""]
        mock_proc.wait = MagicMock()
        mock_proc.returncode = 1

        def fake_popen(cmd, **kwargs):
            kwargs["stderr"].write(b"some ffmpeg failure detail")
            kwargs["stderr"].seek(0)
            return mock_proc

        with patch("subprocess.Popen", side_effect=fake_popen), \
             pytest.raises(RuntimeError, match="rc=1"):
            list(p.stream("hello"))

    def test_stream_completes_on_zero_returncode(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([(b"in1", (22050, 2, 1))])
        p._client = mock_client

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read1.side_effect = [b"out1", b""]
        mock_proc.wait = MagicMock()
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            chunks = list(p.stream("hello"))
        assert chunks == [b"out1"]

    def test_close_after_final_chunk_still_raises_on_ffmpeg_failure(self):
        """CORRECTNESS-06: receiving the final chunk and then closing the
        generator must still surface a failed encode (completed is recorded
        before the final yield)."""
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([(b"in1", (22050, 2, 1))])
        p._client = mock_client

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read1.side_effect = [b"final-out", b""]
        mock_proc.wait = MagicMock()
        mock_proc.returncode = 1

        def fake_popen(cmd, **kwargs):
            kwargs["stderr"].write(b"encode died at the end")
            kwargs["stderr"].seek(0)
            return mock_proc

        with patch("subprocess.Popen", side_effect=fake_popen):
            gen = p.stream("hello")
            chunks = []
            for chunk in gen:            # consumes drain, yields final chunk,
                chunks.append(chunk)     # then suspends before completed...
                break
            assert chunks == [b"final-out"]
            with pytest.raises(RuntimeError, match="rc=1"):
                gen.close()              # finally must now raise rc=1

    def test_close_after_final_chunk_with_zero_rc_is_silent(self):
        """Guard: the happy path with an early close stays silent."""
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([(b"in1", (22050, 2, 1))])
        p._client = mock_client

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read1.side_effect = [b"final-out", b""]
        mock_proc.wait = MagicMock()
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            gen = p.stream("hello")
            next(gen)
            gen.close()  # must not raise

    def test_stream_empty_audio_raises(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([])
        with patch.object(p, "_get_client", return_value=mock_client), \
             patch("subprocess.Popen") as popen, \
             pytest.raises(WyomingServerError, match="Server returned no audio"):
            list(p.stream("hello"))
        popen.assert_not_called()

    def test_stream_to_file_empty_stream_raises(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([])
        with patch.object(p, "_get_client", return_value=mock_client), \
             pytest.raises(WyomingServerError, match="no audio"):
            p._synthesize_stream_to_file("req1", "hello", "/tmp/out.mp3",
                                         voice="v", format="mp3")

    def test_pipe_pcm_to_format_raises_when_ffmpeg_missing(self):
        p = WyomingPiperProvider()
        with patch("shutil.which", return_value=None), \
             pytest.raises(RuntimeError, match="ffmpeg not found"):
            p._pipe_pcm_to_format(1, b"", 22050, 2, 1, "/tmp/out.mp3", "mp3")

    def test_stream_closes_source_when_ffmpeg_missing(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        source_closed = threading.Event()

        def source():
            try:
                yield b"\x00\x00", (22050, 2, 1)
                yield b"\x00\x00", None
            finally:
                source_closed.set()

        mock_client.synthesize_stream.return_value = source()
        with patch.object(p, "_get_client", return_value=mock_client), \
             patch("shutil.which", return_value=None), \
             pytest.raises(RuntimeError, match="ffmpeg not found"):
            list(p.stream("hello"))
        assert source_closed.is_set()

    def test_stream_closes_source_when_command_build_fails(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        source_closed = threading.Event()

        def source():
            try:
                yield b"\x00\x00", (22050, 3, 1)
            finally:
                source_closed.set()

        mock_client.synthesize_stream.return_value = source()
        with patch.object(p, "_get_client", return_value=mock_client), \
             patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             pytest.raises(ValueError, match="Unsupported PCM sample width"):
            list(p.stream("hello"))
        assert source_closed.is_set()
