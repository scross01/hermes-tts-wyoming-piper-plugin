# Hermes Wyoming Piper TTS Plugin

A Hermes TTS plugin that connects to remote Piper TTS services via Wyoming Protocol.

## Features

- Connect to any Wyoming-compatible Piper server
- Query available voices automatically
- Stream synthesis results over TCP
- Auto-reconnect on connection loss
- Voice selection via config or per-request

## Installation

### Option 1: Symlink (development)

```bash
ln -s ~/Development/hermes-wyoming-piper ~/.hermes/plugins/hermes-wyoming-piper
hermes plugins enable hermes-wyoming-piper
```

### Option 2: Copy

```bash
cp -r ~/Development/hermes-wyoming-piper ~/.hermes/plugins/hermes-wyoming-piper
hermes plugins enable hermes-wyoming-piper
```

## Configuration

Add to `~/.hermes/config.yaml`:

```yaml
tts:
  provider: wyoming-piper
  providers:
    wyoming-piper:
      host: raspberrypi08.home.lan
      port: 10200
      voice: ""  # Leave empty for server default
      timeout: 10
```

## Usage

Once configured, use TTS as normal:

```
/voice tts
```

Or use the `text_to_speech` tool with the `provider` parameter set to `wyoming-piper`.

## Requirements

- Python 3.9+
- Network access to the Piper server
- ffmpeg (optional, for non-WAV output formats)

## How It Works

1. Plugin registers as a TTS provider named `wyoming-piper`
2. On synthesis request, connects to the Wyoming Piper server via TCP
3. Sends `Describe` event to discover available voices
4. Sends `TextToSpeak` event with the text
5. Receives `AudioStart` → `AudioChunk` × N → `AudioStop` events
6. Writes WAV audio to output file
7. Optionally converts to MP3/OGG with ffmpeg

## Troubleshooting

**Connection refused:**
- Verify Piper server is running: `nc -z raspberrypi08.home.lan 10200`
- Check firewall rules
- Ensure Wyoming Protocol server is configured (not HTTP)

**No voices found:**
- Check server logs for voice loading errors
- Verify Piper voice models are installed on the server

**Audio quality issues:**
- Piper uses 22050Hz sample rate by default
- Some voices may sound robotic; try different voice names

## License

MIT
