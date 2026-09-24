import asyncio
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, MagicMock, patch

from wyoming_client import (
    SynthesisResult,
    WyomingError,
    WyomingPiperClient,
    WyomingServerError,
    WyomingVoice,
)


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

    def test_handshake_error_disconnects_local_client(self):
        from wyoming.event import Event

        client = WyomingPiperClient(timeout=0.1)
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.read_event = AsyncMock(
            return_value=Event(type="error", data={"text": "describe failed"})
        )

        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn), \
             pytest.raises(WyomingError, match="Expected 'info' event"):
            client.connect()

        mock_conn.disconnect.assert_awaited_once_with()
        assert client.is_connected() is False

    def test_disconnect_connection_reset_resets_state_and_closes_loop(self):
        client = WyomingPiperClient(timeout=0.1)
        mock_conn = MagicMock()
        mock_conn.disconnect = AsyncMock(side_effect=ConnectionResetError("reset"))
        client._client = mock_conn
        client._info = {"tts": []}
        client._voices = []
        client._audio_format = (22050, 2, 1)
        loop = client._get_loop()

        client.disconnect()

        assert client.is_connected() is False
        assert client.audio_format is None
        assert client._info is None
        assert client._voices is None
        assert client._loop is None
        assert loop.is_closed()

    def test_disconnect_releases_loop_before_next_connect(self):
        from wyoming.event import Event

        client = WyomingPiperClient(timeout=0.1)
        released = MagicMock()
        released.disconnect = AsyncMock()
        client._client = released
        released_loop = client._get_loop()

        client.disconnect()

        assert released.disconnect.await_count == 1
        assert released_loop.is_closed()
        assert client._loop is None

        replacement = MagicMock()
        replacement.connect = AsyncMock()
        replacement.disconnect = AsyncMock()
        replacement.write_event = AsyncMock()
        replacement.read_event = AsyncMock(
            return_value=Event(type="info", data={"tts": []})
        )
        with patch("wyoming_client.AsyncTcpClient", return_value=replacement):
            client.connect()

        new_loop = client._loop
        assert new_loop is not None
        assert new_loop is not released_loop
        assert not new_loop.is_closed()
        assert client.is_connected() is True
        client.disconnect()


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

    def test_synthesize_result_keeps_audio_and_format_together(self):
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient()
        pcm = b"\x01\x02" * 8
        mock_conn = MagicMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.read_event = AsyncMock(
            side_effect=[
                Event(type="audio-start", data={"rate": 16000, "width": 2, "channels": 1}),
                AudioChunk(audio=pcm, rate=16000, width=2, channels=1).event(),
                Event(type="audio-stop", data={}),
            ]
        )
        client._client = mock_conn

        result = client.synthesize_result("hello")

        assert result == SynthesisResult(pcm, (16000, 2, 1))
        client.disconnect()

    def test_synthesis_empty_audio_raises(self):
        from wyoming.event import Event

        client = WyomingPiperClient(timeout=0.1)
        mock_conn = MagicMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.read_event = AsyncMock(
            side_effect=[
                Event(type="audio-start", data={"rate": 22050, "width": 2, "channels": 1}),
                Event(type="audio-stop", data={}),
            ]
        )
        client._client = mock_conn

        with pytest.raises(WyomingServerError, match="Server returned no audio"):
            client.synthesize("hello")

        assert client.is_connected() is False
        mock_conn.disconnect.assert_awaited_once_with()

    def test_synthesis_zero_length_chunk_then_audio_succeeds(self):
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient()
        mock_conn = MagicMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.read_event = AsyncMock(
            side_effect=[
                Event(type="audio-start", data={"rate": 22050, "width": 2, "channels": 1}),
                AudioChunk(audio=b"", rate=22050, width=2, channels=1).event(),
                AudioChunk(audio=b"\x01\x02", rate=22050, width=2, channels=1).event(),
                Event(type="audio-stop", data={}),
            ]
        )
        client._client = mock_conn

        assert client.synthesize("hello") == b"\x01\x02"
        client.disconnect()

    def test_reconnect_after_synthesis_error_invalidates_stale_connection(self):
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient(timeout=0.1)
        failed = MagicMock()
        failed.write_event = AsyncMock()
        failed.disconnect = AsyncMock()
        failed.read_event = AsyncMock(
            side_effect=[Event(type="error", data={"text": "synthesis failed"})]
        )
        client._client = failed

        with pytest.raises(WyomingServerError, match="synthesis failed"):
            client.synthesize("first")
        assert client.is_connected() is False
        failed.disconnect.assert_awaited_once_with()

        replacement = MagicMock()
        replacement.connect = AsyncMock()
        replacement.disconnect = AsyncMock()
        replacement.write_event = AsyncMock()
        replacement.read_event = AsyncMock(
            side_effect=[
                Event(type="info", data={}),
                Event(type="audio-start", data={"rate": 16000, "width": 2, "channels": 1}),
                AudioChunk(audio=b"replacement", rate=16000, width=2, channels=1).event(),
                Event(type="audio-stop", data={}),
            ]
        )
        with patch("wyoming_client.AsyncTcpClient", return_value=replacement) as constructor:
            result = client.synthesize("second")

        assert result == b"replacement"
        assert client.audio_format == (16000, 2, 1)
        constructor.assert_called_once()
        client.disconnect()

    def test_synthesis_budget_error_invalidates_connection(self):
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient(timeout=0.1)
        mock_conn = MagicMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.read_event = AsyncMock(
            side_effect=[
                Event(type="audio-start", data={"rate": 22050, "width": 2, "channels": 1}),
                AudioChunk(audio=b"1234", rate=22050, width=2, channels=1).event(),
            ]
        )
        client._client = mock_conn

        with patch("wyoming_client._SAFETY_MAX_PCM_BYTES", 3), \
             pytest.raises(WyomingServerError, match="PCM budget"):
            client.synthesize("hello")

        assert client.is_connected() is False
        mock_conn.disconnect.assert_awaited_once_with()

    def test_synthesis_deadline_invalidates_connection(self):
        client = WyomingPiperClient(timeout=0.1)
        mock_conn = MagicMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.disconnect = AsyncMock()

        async def slow_read():
            await asyncio.sleep(1)

        mock_conn.read_event = slow_read
        client._client = mock_conn

        with patch("wyoming_client._SAFETY_MAX_SYNTHESIS_SECONDS", 0.01), \
             pytest.raises(WyomingServerError, match="timed out"):
            client.synthesize("hello")

        assert client.is_connected() is False
        mock_conn.disconnect.assert_awaited_once_with()


class TestSynthesizeStreamErrors:
    def test_connection_refused_raises_wyoming_error(self):
        """Regression: ConnectionRefusedError used to vanish (empty stream).

        Hermetic: a real ConnectionRefusedError is simulated by making
        AsyncTcpClient.connect raise one on the worker thread; the worker's
        broad except must forward it and the consumer must re-raise it as
        WyomingError. (The original version of this test used a real
        localhost connection to a closed port; it was made hermetic for CI
        stability - see plans/015.)"""
        client = WyomingPiperClient()
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock(
            side_effect=ConnectionRefusedError("[Errno 61] Connect call failed")
        )
        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn), \
             pytest.raises(WyomingError):
            list(client.synthesize_stream("hello"))

    def test_unexpected_worker_exception_raises_wyoming_error(self):
        """Any non-WyomingError exception on the worker thread must surface as
        WyomingError in the consumer (the plan-009 contract), hermetically."""
        client = WyomingPiperClient()
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock(side_effect=OSError("some socket failure"))
        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn), \
             pytest.raises(WyomingError):
            list(client.synthesize_stream("hello"))

    def test_stream_empty_audio_raises(self):
        from wyoming.event import Event

        client = WyomingPiperClient(timeout=0.1)
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.read_event = AsyncMock(
            side_effect=[
                Event(type="audio-start", data={"rate": 22050, "width": 2, "channels": 1}),
                Event(type="audio-stop", data={}),
            ]
        )
        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn), \
             pytest.raises(WyomingServerError, match="Server returned no audio"):
            list(client.synthesize_stream("hello"))
        mock_conn.disconnect.assert_awaited_once_with()

    def test_stream_zero_length_chunk_then_audio_succeeds(self):
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient()
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.read_event = AsyncMock(
            side_effect=[
                Event(type="audio-start", data={"rate": 22050, "width": 2, "channels": 1}),
                AudioChunk(audio=b"", rate=22050, width=2, channels=1).event(),
                AudioChunk(audio=b"\x01\x02", rate=22050, width=2, channels=1).event(),
                Event(type="audio-stop", data={}),
            ]
        )
        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn):
            items = list(client.synthesize_stream("hello"))
        assert items == [(b"\x01\x02", (22050, 2, 1))]

    def test_midstream_error_event_raises_wyoming_server_error(self):
        from wyoming.event import Event

        client = WyomingPiperClient()
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.read_event = AsyncMock(side_effect=[
            Event(type="error", data={"text": "voice not found"}),
        ])
        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn), \
             pytest.raises(WyomingServerError, match="Synthesis error: voice not found"):
            list(client.synthesize_stream("hello"))

    def test_generator_close_does_not_raise_and_bounded(self):
        """Consumer-side finally: closing the generator early must not hang and
        must not lose the sentinel path."""
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient()
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.write_event = AsyncMock()
        # Worker that would produce many chunks; we consume one and close.
        chunk_events = [
            Event(type="audio-start", data={"rate": 22050, "width": 2, "channels": 1}),
            AudioChunk(audio=b"\x01\x02", rate=22050, width=2, channels=1).event(),
            AudioChunk(audio=b"\x03\x04", rate=22050, width=2, channels=1).event(),
            Event(type="audio-stop", data={}),
        ]
        mock_conn.read_event = AsyncMock(side_effect=chunk_events)
        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn):
            gen = client.synthesize_stream("hello")
            first = next(gen)
            assert first == (b"\x01\x02", (22050, 2, 1))
            gen.close()  # must return promptly, not hang

    def test_close_cancels_blocked_worker_and_disconnects(self):
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient(timeout=0.1)
        read_started = threading.Event()
        release_read = threading.Event()
        workers = []
        real_thread = threading.Thread
        read_count = 0

        async def read_event():
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                return Event(
                    type="audio-start",
                    data={"rate": 22050, "width": 2, "channels": 1},
                )
            if read_count == 2:
                return AudioChunk(
                    audio=b"\x01\x02", rate=22050, width=2, channels=1
                ).event()
            read_started.set()
            await asyncio.to_thread(release_read.wait)
            return Event(type="audio-stop", data={})

        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.read_event = read_event

        def make_thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            workers.append(worker)
            return worker

        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn), \
             patch("wyoming_client.Thread", side_effect=make_thread):
            generator = client.synthesize_stream("hello")
            try:
                assert next(generator) == (b"\x01\x02", (22050, 2, 1))
                assert read_started.wait(timeout=1)
                generator.close()
            finally:
                release_read.set()
                generator.close()
                workers[0].join(timeout=1)

        assert not workers[0].is_alive()
        mock_conn.disconnect.assert_awaited_once_with()

    def test_stream_pcm_budget_raises_and_disconnects(self):
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient(timeout=0.1)
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.read_event = AsyncMock(
            side_effect=[
                Event(type="audio-start", data={"rate": 22050, "width": 2, "channels": 1}),
                AudioChunk(audio=b"1234", rate=22050, width=2, channels=1).event(),
            ]
        )

        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn), \
             patch("wyoming_client._SAFETY_MAX_PCM_BYTES", 3), \
             pytest.raises(WyomingServerError, match="PCM budget"):
            list(client.synthesize_stream("hello"))

        mock_conn.disconnect.assert_awaited_once_with()

    def test_stream_deadline_raises_and_disconnects(self):
        from wyoming.event import Event

        client = WyomingPiperClient(timeout=0.1)
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.write_event = AsyncMock()

        async def slow_read():
            await asyncio.sleep(1)
            return Event(type="audio-stop", data={})

        mock_conn.read_event = slow_read

        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn), \
             patch("wyoming_client._SAFETY_MAX_SYNTHESIS_SECONDS", 0.01), \
             pytest.raises(WyomingServerError, match="timed out"):
            list(client.synthesize_stream("hello"))

        mock_conn.disconnect.assert_awaited_once_with()

    def test_disconnect_failure_does_not_mask_primary_error(self):
        """CORRECTNESS-04: a failing disconnect must not replace the real
        synthesis error."""
        from wyoming.event import Event

        client = WyomingPiperClient()
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock(side_effect=OSError("teardown boom"))
        mock_conn.write_event = AsyncMock()
        mock_conn.read_event = AsyncMock(side_effect=[
            Event(type="error", data={"text": "voice not found"}),
        ])
        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn), \
             pytest.raises(WyomingServerError, match="Synthesis error: voice not found"):
            list(client.synthesize_stream("hello"))

    def test_disconnect_hang_does_not_hang_consumer(self):
        """CORRECTNESS-04: a hanging disconnect must not stop the sentinel
        from being posted; the consumer gets its data. Runs in ~timeout, not
        the 10s the fake disconnect would take without the bound."""
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient(timeout=0.1)
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()

        async def hanging_disconnect():
            await asyncio.sleep(10)

        mock_conn.disconnect = hanging_disconnect
        mock_conn.write_event = AsyncMock()
        mock_conn.read_event = AsyncMock(side_effect=[
            Event(type="audio-start", data={"rate": 22050, "width": 2, "channels": 1}),
            AudioChunk(audio=b"\x01\x02", rate=22050, width=2, channels=1).event(),
            Event(type="audio-stop", data={}),
        ])
        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn):
            items = list(client.synthesize_stream("hello"))
        assert items == [(b"\x01\x02", (22050, 2, 1))]

    def test_disconnect_failure_alone_ends_stream_cleanly(self):
        """If synthesis succeeded but teardown fails, the stream still ends
        with its data intact (teardown error only logged)."""
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient()
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock(side_effect=OSError("teardown boom"))
        mock_conn.write_event = AsyncMock()
        mock_conn.read_event = AsyncMock(side_effect=[
            Event(type="audio-start", data={"rate": 22050, "width": 2, "channels": 1}),
            AudioChunk(audio=b"\x01\x02", rate=22050, width=2, channels=1).event(),
            Event(type="audio-stop", data={}),
        ])
        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn):
            items = list(client.synthesize_stream("hello"))
        assert items == [(b"\x01\x02", (22050, 2, 1))]


class TestThreadSafety:
    def test_lock_is_reentrant(self):
        client = WyomingPiperClient()
        # Nested on purpose: re-entering the lock must not deadlock (plain Lock would).
        with client._lock:  # noqa: SIM117
            with client._lock:
                pass

    def test_concurrent_synthesize_serialized(self):
        """Two threads calling synthesize() must never overlap inside the
        event-loop section. Without the lock, both threads would routinely be
        inside read_event at once; with it, max_in_flight stays at 1."""
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient()
        state = {"in_flight": 0, "max": 0, "reads": 0}
        lock_for_state = threading.Lock()

        async def slow_read(*args, **kwargs):
            with lock_for_state:
                state["in_flight"] += 1
                state["max"] = max(state["max"], state["in_flight"])
                state["reads"] += 1
                read_number = state["reads"]
            await asyncio.sleep(0.05)
            with lock_for_state:
                state["in_flight"] -= 1
            if read_number % 2:
                return AudioChunk(
                    audio=b"\x01\x02", rate=22050, width=2, channels=1
                ).event()
            return Event(type="audio-stop", data={})

        mock_conn = MagicMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.read_event = AsyncMock(side_effect=slow_read)
        client._client = mock_conn  # skips connect(); loop section still runs

        threads = [
            threading.Thread(target=client.synthesize, args=("hello",))
            for _ in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert state["max"] == 1
        client.disconnect()

    def test_successful_stream_still_yields(self):
        """Guard: the broadened except must not change the happy path."""
        from wyoming.audio import AudioChunk
        from wyoming.event import Event

        client = WyomingPiperClient()
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn.disconnect = AsyncMock()
        mock_conn.write_event = AsyncMock()
        mock_conn.read_event = AsyncMock(side_effect=[
            Event(type="audio-start", data={"rate": 22050, "width": 2, "channels": 1}),
            AudioChunk(audio=b"\x01\x02", rate=22050, width=2, channels=1).event(),
            Event(type="audio-stop", data={}),
        ])
        with patch("wyoming_client.AsyncTcpClient", return_value=mock_conn):
            items = list(client.synthesize_stream("hello"))
        assert items == [(b"\x01\x02", (22050, 2, 1))]
