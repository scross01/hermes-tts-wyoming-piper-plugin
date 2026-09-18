# TTS Wyoming Piper Plugin

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) TTS plugin that connects to remote [Piper](https://github.com/OHF-Voice/wyoming-piper) TTS services via [Wyoming Protocol](https://github.com/OHF-Voice/wyoming).

## Background

[Wyoming Protocol](https://github.com/OHF-Voice/wyoming) is a peer-to-peer TCP protocol for voice assistants, created by the [Open Home Foundation](https://www.openhomefoundation.org/) (the team behind Home Assistant and Rhasspy). It enables real-time streaming of audio and voice events between services — originally designed for local voice pipelines in Home Assistant.

[Piper](https://github.com/OHF-Voice/wyoming-piper) is a fast, local neural text-to-speech engine that speaks Wyoming Protocol. It runs entirely on CPU, supports 44 languages with pre-trained voices, and needs no API key.

Most people use Wyoming Piper through Home Assistant's voice pipeline. This plugin bridges a different path: **Hermes Agent → Wyoming Protocol → Piper TTS server**. This is useful when you want your AI agent to generate speech through a Piper instance running on another machine (a Raspberry Pi, a home server, etc.) without Home Assistant in the loop.

### Who is this for?

- **Home lab tinkerers** running Piper on a Raspberry Pi or dedicated server who want their Hermes agent to use it
- **Privacy-focused users** who want TTS without cloud APIs — Piper runs entirely offline
- **Multi-device setups** where Piper serves multiple clients (Home Assistant, Hermes, custom tools) on the same network
- **Existing Wyoming Piper users** who want to add Hermes voice output to their setup

## Features

- Connect to any Wyoming-compatible Piper server
- 44 languages, 174+ pre-trained voices
- Voice selection via config or per-request
- Voice bubble support — Opus encoding via ffmpeg
- Warm-up and connection reuse for fast repeated synthesis
- Two synthesis modes: `pipe` (efficient) and `stream` (streaming delivery)

## Installation

```bash
hermes plugins install scross01/hermes-tts-wyoming-piper-plugin --enable
```

This clones the repo and enables the plugin. Restart the desktop app or gateway to activate.

## Configuration

Add to `~/.hermes/config.yaml`:

```yaml
tts:
  provider: wyoming-piper

plugins:
  entries:
    tts-wyoming-piper:
      settings:
        host: piper.local
        port: 10200
        voice: en_US-lessac-medium
        timeout: 10
        mode: pipe
```

| Setting | Description | Default |
|---------|-------------|---------|
| `host` | Piper server hostname or IP | `localhost` |
| `port` | Wyoming Protocol port | `10200` |
| `voice` | Voice name (empty = server default) | `""` |
| `timeout` | Connection timeout in seconds | `10` |
| `mode` | Synthesis mode: `pipe` or `stream` | `pipe` |

### Synthesis Modes

**`pipe` (default)** — Efficient single-pass conversion:
```
Piper TCP → PCM → ffmpeg → Opus/MP3
```
Writes PCM directly to ffmpeg stdin, outputs the target format in one pass. No intermediate WAV or MP3 files.

**`stream`** — Streaming delivery via `TTSProvider.stream()`:
```
Piper TCP chunks → ffmpeg → Opus chunks (yielded incrementally)
```
Implements `TTSProvider.stream()` yielding Opus chunks as they arrive. Currently no Hermes consumer dispatches to `TTSProvider.stream()` — CLI voice mode and the dashboard use `StreamingTTSProvider` (raw PCM), a separate interface (Hermes #47896). Has potential future value once Hermes adds generic `stream()` dispatch.

## Usage

Once configured, use TTS as normal:

```
/voice tts
```

Or use the `text_to_speech` tool — it routes through your Piper server automatically.

## Requirements

- Python 3.9+
- Network access to the Piper server
- ffmpeg (for Opus/MP3 output)

## How It Works

1. Plugin registers as a TTS provider named `wyoming-piper`
2. On synthesis request, connects to the Wyoming Piper server via TCP
3. Sends `describe` event to discover available voices (used for default voice selection)
4. Sends `synthesize` event with the text
5. Receives `audio-start` → `audio-chunk` × N → `audio-stop` events
6. In `pipe` mode: pipes PCM directly to ffmpeg for target format
7. In `stream` mode: yields Opus chunks as they arrive

## Troubleshooting

**Connection refused:**
- Verify Piper server is running: `nc -z piper.local 10200`
- Check firewall rules
- Ensure Wyoming Protocol server is configured (not HTTP)

**No voices found:**
- Check server logs for voice loading errors
- Verify Piper voice models are installed on the server

**Desktop app not picking up changes:**
- Quit and reopen the desktop app (plugins only load at startup)

## License

MIT
