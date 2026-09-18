"""
Hermes Wyoming Piper TTS Plugin

Connects to a remote Piper TTS service via Wyoming Protocol (TCP)
and registers as a TTS provider in Hermes.

Usage:
  1. Install plugin: symlink to ~/.hermes/plugins/hermes-wyoming-piper
  2. Enable: hermes plugins enable hermes-wyoming-piper
  3. Configure: set tts.provider to "wyoming-piper" in config.yaml
  4. Set connection: tts.providers.wyoming-piper.host/port in config.yaml
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from agent.tts_provider import TTSProvider, VALID_OUTPUT_FORMATS

logger = logging.getLogger("hermes-wyoming-piper")


class WyomingPiperProvider(TTSProvider):
    """TTS provider that connects to a remote Piper via Wyoming Protocol."""

    _name = "wyoming-piper"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = config or {}
        self._client = None
        self._voices: Optional[List[Dict[str, Any]]] = None

    @property
    def name(self) -> str:
        return self._name

    def _get_config(self) -> Dict[str, Any]:
        """Get provider config from Hermes config."""
        try:
            from hermes_constants import get_hermes_home
            import yaml

            config_path = os.path.join(get_hermes_home(), "config.yaml")
            with open(config_path) as f:
                full_config = yaml.safe_load(f) or {}

            tts_config = full_config.get("tts", {})
            providers = tts_config.get("providers", {})
            return providers.get(self._name, {})
        except Exception as e:
            logger.debug("Could not load config: %s", e)
            return {}

    def _get_client(self):
        """Get or create Wyoming client with lazy import."""
        if self._client is not None:
            return self._client

        from .wyoming_client import WyomingPiperClient

        config = self._get_config()
        host = config.get("host", "localhost")
        port = config.get("port", 10200)
        timeout = config.get("timeout", 10)

        self._client = WyomingPiperClient(
            host=host,
            port=port,
            timeout=timeout,
        )
        return self._client

    def list_voices(self) -> List[Dict[str, Any]]:
        """Query server for available voices."""
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
            logger.warning("Failed to list voices from Wyoming server: %s", e)
            return []

    def default_voice(self) -> Optional[str]:
        """Get default voice from config or server."""
        config = self._get_config()
        voice = config.get("voice", "")
        if voice:
            return voice

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
        """Synthesize text to audio file via Wyoming Piper."""
        from .wyoming_client import WyomingError

        client = self._get_client()
        voice_name = voice or self.default_voice()

        # Get raw WAV audio from Piper
        wav_bytes = client.synthesize(text, voice=voice_name)

        # Write WAV to output path (Piper always outputs WAV)
        wav_path = output_path
        if not wav_path.endswith(".wav"):
            wav_path = output_path.rsplit(".", 1)[0] + ".wav"

        with open(wav_path, "wb") as f:
            f.write(wav_bytes)

        # If format is not WAV, convert with ffmpeg if available
        if format.lower() not in ("wav", "pcm"):
            converted = self._convert_audio(wav_path, output_path, format)
            if converted:
                return converted

        return wav_path

    def _convert_audio(self, input_path: str, output_path: str, target_format: str) -> Optional[str]:
        """Convert audio format using ffmpeg if available."""
        import subprocess

        if not output_path.endswith(f".{target_format}"):
            output_path = output_path.rsplit(".", 1)[0] + f".{target_format}"

        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", input_path,
                    "-acodec", "libmp3lame" if target_format == "mp3" else "copy",
                    output_path,
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                # Remove intermediate WAV if different path
                if input_path != output_path:
                    try:
                        os.remove(input_path)
                    except OSError:
                        pass
                return output_path
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        return None

    def warm(self) -> None:
        """Pre-connect to server for faster first synthesis."""
        try:
            client = self._get_client()
            client.connect()
            # Pre-fetch voices
            self.list_voices()
            logger.info("Wyoming Piper provider warmed up")
        except Exception as e:
            logger.debug("Warm-up failed (will retry on synthesis): %s", e)

    def release(self) -> None:
        """Disconnect from server."""
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    @property
    def voice_compatible(self) -> bool:
        """Piper output is suitable for voice bubble delivery."""
        return True


def register(ctx) -> None:
    """Register the Wyoming Piper TTS provider with Hermes."""
    config = {}

    # Load config if available
    try:
        from hermes_constants import get_hermes_home
        import yaml

        config_path = os.path.join(get_hermes_home(), "config.yaml")
        with open(config_path) as f:
            full_config = yaml.safe_load(f) or {}

        tts_config = full_config.get("tts", {})
        providers = tts_config.get("providers", {})
        config = providers.get("wyoming-piper", {})
    except Exception:
        pass

    provider = WyomingPiperProvider(config=config)
    ctx.register_tts_provider(provider)
    logger.info("Registered Wyoming Piper TTS provider")
