"""
Hermes Wyoming Piper TTS Plugin

Connects to a remote Piper TTS service via Wyoming Protocol (TCP)
and registers as a TTS provider in Hermes.

Usage:
  1. Install: ln -s ~/Development/hermes-wyoming-piper ~/.hermes/plugins/hermes-wyoming-piper
  2. Enable: hermes plugins enable hermes-wyoming-piper
  3. Configure in config.yaml:

    plugins:
      entries:
        hermes-wyoming-piper:
          settings:
            host: raspberrypi08
            port: 10200
            voice: en_US-lessac-medium
            timeout: 10

  4. Set tts.provider: wyoming-piper
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agent.tts_provider import TTSProvider

logger = logging.getLogger("hermes-wyoming-piper")


class WyomingPiperProvider(TTSProvider):
    """TTS provider that connects to a remote Piper via Wyoming Protocol."""

    _name = "wyoming-piper"

    def __init__(self, host: str = "localhost", port: int = 10200,
                 voice: str = "", timeout: int = 10):
        self._host = host
        self._port = port
        self._voice = voice
        self._timeout = timeout
        self._client = None
        self._voices: Optional[List[Dict[str, Any]]] = None

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
        except Exception as e:
            logger.warning("Failed to list voices: %s", e)
            return []

    def default_voice(self) -> Optional[str]:
        if self._voice:
            return self._voice
        voices = self.list_voices()
        return voices[0]["id"] if voices else None

    def synthesize(
        self,
        text: str,
        output_path: str,
        *,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        format: str = "mp3",
        **extra: Any,
    ) -> str:
        client = self._get_client()
        voice_name = voice or self.default_voice()

        wav_bytes = client.synthesize(text, voice=voice_name)

        wav_path = output_path
        if not wav_path.endswith(".wav"):
            wav_path = output_path.rsplit(".", 1)[0] + ".wav"

        with open(wav_path, "wb") as f:
            f.write(wav_bytes)

        if format.lower() not in ("wav", "pcm"):
            converted = self._convert_audio(wav_path, output_path, format)
            if converted:
                return converted

        return wav_path

    def _convert_audio(self, input_path: str, output_path: str, target_format: str) -> Optional[str]:
        import subprocess
        import os

        if not output_path.endswith(f".{target_format}"):
            output_path = output_path.rsplit(".", 1)[0] + f".{target_format}"

        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", input_path, "-acodec", "libmp3lame", output_path],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                if input_path != output_path:
                    os.remove(input_path)
                return output_path
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        return None

    def warm(self) -> None:
        try:
            self._get_client().connect()
            self.list_voices()
        except Exception as e:
            logger.debug("Warm-up failed: %s", e)

    def release(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    @property
    def voice_compatible(self) -> bool:
        return True


def register(ctx) -> None:
    provider = WyomingPiperProvider(
        host=ctx.get_config("host", "localhost"),
        port=ctx.get_config("port", 10200),
        voice=ctx.get_config("voice", ""),
        timeout=ctx.get_config("timeout", 10),
    )
    ctx.register_tts_provider(provider)
