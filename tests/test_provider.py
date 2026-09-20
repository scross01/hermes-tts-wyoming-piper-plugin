import os
import subprocess
import sys
import tempfile
import wave
from unittest.mock import MagicMock, patch

import pytest

from wyoming_client import WyomingError, WyomingVoice

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

    def test_voice_compatible(self):
        p = WyomingPiperProvider()
        assert p.voice_compatible is True

    def test_write_fallback_wav_roundtrip(self):
        p = WyomingPiperProvider()
        pcm = b"\x00\x00" * 100  # 100 samples of silence
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            p._write_fallback_wav(pcm, 22050, 2, 1, path)
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

        with patch("uuid.uuid4", return_value=fixed_uuid):
            with patch("tts_wyoming_piper._debug") as mock_debug:
                p.synthesize("hello", "/tmp/out.mp3")

        first_call_arg = mock_debug.call_args[0][0]
        assert fixed_uuid in first_call_arg


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

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("subprocess.DEVNULL"):
            list(p.stream("hello"))

        mock_proc.kill.assert_called_once()

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
