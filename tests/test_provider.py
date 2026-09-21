import logging
import os
import subprocess
import sys
import tempfile
import wave
from unittest.mock import MagicMock, patch

import pytest

from wyoming_client import WyomingError, WyomingServerError, WyomingVoice

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.conftest import _load_wyoming_piper

WyomingPiperProvider = _load_wyoming_piper().WyomingPiperProvider


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

    @pytest.mark.parametrize("target_ext,expected_codec_args", [
        ("ogg", ["-acodec", "libopus", "-b:a", "48k", "-vbr", "on"]),
        ("opus", ["-acodec", "libopus", "-b:a", "48k", "-vbr", "on"]),
        ("mp3", ["-acodec", "libmp3lame"]),
        ("flac", ["-acodec", "flac"]),
        ("wav", []),
        ("pcm", []),
    ])
    def test_build_ffmpeg_cmd(self, target_ext, expected_codec_args):
        p = WyomingPiperProvider()
        cmd = p._build_ffmpeg_cmd("/usr/bin/ffmpeg", 22050, 1, target_ext, "/tmp/out")
        assert cmd[0] == "/usr/bin/ffmpeg"
        assert "-f" in cmd
        assert "s16le" in cmd
        for arg in expected_codec_args:
            assert arg in cmd
        assert cmd[-1] == "/tmp/out"

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
        mock_client.synthesize.return_value = b""
        mock_client.audio_format = (22050, 2, 1)
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
        mock_client.synthesize.return_value = pcm
        mock_client.audio_format = fmt
        p._client = mock_client
        return p

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


class TestStreamHangFix:
    def test_stream_kills_ffmpeg_on_timeout(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([(b"\x00\x00" * 100, (22050, 2, 1))])
        p._client = mock_client

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read.side_effect = [b"", b""]
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10), 0]
        mock_proc.kill = MagicMock()
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("subprocess.DEVNULL"):
            list(p.stream("hello"))

        mock_proc.kill.assert_called_once()


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
        proc.kill.assert_called_once()

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


class TestStreamFfmpegFailure:
    def test_stream_raises_on_nonzero_returncode(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([(b"in1", (22050, 2, 1))])
        p._client = mock_client

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read.side_effect = [b"out1"]   # loop body never runs; read once for `remaining`
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
        mock_proc.stdout.read.side_effect = [b"out1"]
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
        mock_proc.stdout.read.side_effect = [b"final-out"]  # only the drain
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
        mock_proc.stdout.read.side_effect = [b"final-out"]
        mock_proc.wait = MagicMock()
        mock_proc.returncode = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            gen = p.stream("hello")
            next(gen)
            gen.close()  # must not raise

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

    def test_stream_raises_when_ffmpeg_missing(self):
        p = WyomingPiperProvider()
        mock_client = MagicMock()
        mock_client.synthesize_stream.return_value = iter([
            (b"\x00\x00", (22050, 2, 1)),
        ])
        with patch.object(p, "_get_client", return_value=mock_client), \
             patch("shutil.which", return_value=None), \
             pytest.raises(RuntimeError, match="ffmpeg not found"):
            list(p.stream("hello"))
