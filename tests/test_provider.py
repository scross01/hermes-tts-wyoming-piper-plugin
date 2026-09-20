import os
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
