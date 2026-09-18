# Hermes Wyoming Piper Plugin

## Goal

Create a Hermes TTS plugin that connects to a remote Piper TTS service via Wyoming Protocol, enabling text-to-speech synthesis on a Raspberry Pi (or any Wyoming-compatible Piper server).

## Architecture

### Wyoming Protocol Overview

Wyoming Protocol is a TCP-based peer-to-peer protocol for voice assistants. For TTS:

1. Client connects to server (TCP)
2. Client sends `Describe` event → server responds with capabilities (available voices, audio format)
3. Client sends `TextToSpeak` event with text and voice name
4. Server streams back audio as `AudioStart` → `AudioChunk` × N → `AudioStop` events
5. Audio format is typically WAV (PCM 16-bit, 22050Hz mono)

### Plugin Structure

```
hermes-wyoming-piper/
├── plugin.yaml          # Plugin metadata + config schema
├── __init__.py          # Plugin entry point, registers TTS provider
├── wyoming_client.py    # Wyoming Protocol TCP client
├── config.yaml          # Default configuration
└── README.md            # Documentation
```

### plugin.yaml

```yaml
name: hermes-wyoming-piper
version: 0.1.0
description: "TTS provider using Wyoming Protocol to connect to remote Piper services"
config:
  host:
    type: string
    default: "localhost"
    description: "Wyoming Piper server hostname"
  port:
    type: integer
    default: 10200
    description: "Wyoming Piper server port"
  voice:
    type: string
    default: ""
    description: "Voice name (leave empty for server default)"
  timeout:
    type: integer
    default: 30
    description: "Connection timeout in seconds"
```

### Wyoming Client Implementation

The client needs to handle:

1. **Connection management**: TCP socket with timeout, reconnection on failure
2. **Event framing**: Wyoming uses length-prefixed binary frames
3. **Handshake**: Send `Describe` event, parse response for voice list
4. **Synthesis**: Send `TextToSpeak`, receive audio chunks, assemble WAV

Wyoming event format (from the spec):
- Each event is a binary frame: `[4 bytes: payload length][payload]`
- Events are typed (AudioStart, AudioChunk, AudioStop, TextToSpeak, Describe, etc.)
- TextToSpeak payload includes the text and optional voice name
- AudioStart/AudioStop frame the audio stream

### TTS Provider Integration

Hermes TTS providers work by:
1. Being registered in the plugin's `__init__.py` via `ctx.register_tts_provider()`
2. Receiving text and returning audio file path
3. Supporting voice selection via config

The plugin should:
- Use the `wyoming` Python package if available, otherwise implement raw TCP
- Cache the voice list from the server
- Handle connection pooling (keep-alive for repeated calls)
- Fall back gracefully if server is unreachable

## Implementation Tasks

1. **Create plugin.yaml** with config schema
2. **Implement wyoming_client.py**:
   - `WyomingPiperClient` class
   - Connect/disconnect methods
   - `describe()` to get available voices
   - `synthesize(text, voice)` to get audio bytes
   - Error handling and reconnection logic
3. **Implement __init__.py**:
   - Register as TTS provider
   - Load config from plugin config
   - Wire client to provider interface
4. **Test locally**:
   - Verify connection to raspberrypi08.home.lan:10200
   - Test synthesis and audio output
   - Handle server unavailability gracefully

## Testing

- Unit tests for Wyoming client event framing
- Integration test with real Piper server (if available)
- Test fallback behavior when server is down

## Notes

- The `wyoming` PyPI package may have a client implementation we can use
- Audio format: WAV PCM 16-bit 22050Hz mono (Piper default)
- Wyoming protocol is text-based for events, binary for audio payloads
