# TTS Wyoming Piper Plugin

A Hermes TTS plugin that connects to remote Piper TTS services via Wyoming Protocol.

## Features

- Connect to any Wyoming-compatible Piper server
- Query available voices automatically
- Voice selection via config or per-request
- Voice bubble support (Telegram, Discord)

## Installation

### Option 1: Symlink (development)

```bash
ln -s ~/Development/tts-wyoming-piper ~/.hermes/plugins/tts-wyoming-piper
hermes plugins enable tts-wyoming-piper
```

### Option 2: Copy

```bash
cp -r ~/Development/tts-wyoming-piper ~/.hermes/plugins/tts-wyoming-piper
hermes plugins enable tts-wyoming-piper
```

## Configuration

Add to `~/.hermes/config.yaml`:

```yaml
tts:
  provider: wyoming-piper

plugins:
  entries:
    tts-wyoming-piper:
      settings:
        host: raspberrypi08
        port: 10200
        voice: en_US-lessac-medium
        timeout: 10
```

## Usage

Once configured, use TTS as normal:

```
/voice tts
```

Or use the `text_to_speech` tool — it routes through your Piper server automatically.

## Requirements

- Python 3.9+
- Network access to the Piper server
- ffmpeg (for MP3/Opus output)

## How It Works

1. Plugin registers as a TTS provider named `wyoming-piper`
2. On synthesis request, connects to the Wyoming Piper server via TCP
3. Sends `describe` event to discover available voices
4. Sends `synthesize` event with the text
5. Receives `audio-start` → `audio-chunk` × N → `audio-stop` events
6. Writes WAV audio to output file
7. Converts to MP3/Opus with ffmpeg

## Troubleshooting

**Connection refused:**
- Verify Piper server is running: `nc -z raspberrypi08 10200`
- Check firewall rules
- Ensure Wyoming Protocol server is configured (not HTTP)

**No voices found:**
- Check server logs for voice loading errors
- Verify Piper voice models are installed on the server

## License

MIT
