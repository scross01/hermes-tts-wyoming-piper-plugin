import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, MagicMock, patch

from wyoming_client import WyomingPiperClient, WyomingVoice


class TestWyomingVoice:
    def test_repr(self):
        v = WyomingVoice(name="en_US-lessac-medium", languages=["en-US"])
        assert "en_US-lessac-medium" in repr(v)
        assert "en-US" in repr(v)

    def test_repr_no_languages(self):
        v = WyomingVoice(name="test-voice")
        assert "test-voice" in repr(v)
        assert "[]" in repr(v)


class TestWyomingPiperClientPure:
    def test_parse_voices_from_info_empty(self):
        client = WyomingPiperClient()
        voices = client._parse_voices_from_info({})
        assert voices == []

    def test_parse_voices_from_info_no_tts_key(self):
        client = WyomingPiperClient()
        voices = client._parse_voices_from_info({"other": "data"})
        assert voices == []

    def test_parse_voices_from_info_with_voices(self):
        client = WyomingPiperClient()
        info = {
            "tts": [
                {"voices": [{"name": "v1", "languages": ["en"]}]}
            ]
        }
        voices = client._parse_voices_from_info(info)
        assert len(voices) == 1
        assert voices[0].name == "v1"
        assert voices[0].languages == ["en"]

    def test_parse_voices_from_info_multiple_providers(self):
        client = WyomingPiperClient()
        info = {
            "tts": [
                {"voices": [{"name": "v1", "languages": ["en"]}]},
                {"voices": [{"name": "v2", "languages": ["fr"]}, {"name": "v3"}]},
            ]
        }
        voices = client._parse_voices_from_info(info)
        assert len(voices) == 3
        assert voices[1].name == "v2"
        assert voices[2].languages == []

    def test_is_connected_false_initially(self):
        client = WyomingPiperClient()
        assert client.is_connected() is False

    def test_is_connected_true_after_mocking_client(self):
        client = WyomingPiperClient()
        client._client = MagicMock()
        assert client.is_connected() is True

    def test_audio_format_initially_none(self):
        client = WyomingPiperClient()
        assert client.audio_format is None

    def test_audio_format_after_assignment(self):
        client = WyomingPiperClient()
        client._audio_format = (22050, 2, 1)
        assert client.audio_format == (22050, 2, 1)

    def test_get_loop_returns_new_loop_when_none(self):
        client = WyomingPiperClient()
        loop1 = client._get_loop()
        assert loop1 is not None
        assert not loop1.is_closed()

    def test_get_loop_returns_same_loop_on_second_call(self):
        client = WyomingPiperClient()
        loop1 = client._get_loop()
        loop2 = client._get_loop()
        assert loop1 is loop2

    def test_get_loop_creates_new_loop_after_closed(self):
        client = WyomingPiperClient()
        loop1 = client._get_loop()
        loop1.close()
        loop2 = client._get_loop()
        assert loop1 is not loop2
        assert not loop2.is_closed()

    def test_enter_exit_calls_connect_and_disconnect(self):
        client = WyomingPiperClient()
        with patch.object(client, "connect") as mock_connect, \
             patch.object(client, "disconnect") as mock_disconnect:
            with client:
                assert mock_connect.called
            assert mock_disconnect.called

    def test_enter_returns_self(self):
        client = WyomingPiperClient()
        with patch.object(client, "connect"):
            result = client.__enter__()
            assert result is client


class TestSynthesizeRawPcm:
    def test_synthesize_returns_raw_pcm_without_wav_header(self):
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient()
        pcm = b"\x01\x02" * 16
        mock_conn = MagicMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.read_event = AsyncMock(side_effect=[
            Event(type="audio-start", data={"rate": 22050, "width": 2, "channels": 1}),
            AudioChunk(audio=pcm, rate=22050, width=2, channels=1).event(),
            Event(type="audio-stop", data={}),
        ])
        client._client = mock_conn  # skips connect() entirely

        result = client.synthesize("hello")

        assert result == pcm
        assert not result.startswith(b"RIFF")
        assert client.audio_format == (22050, 2, 1)
