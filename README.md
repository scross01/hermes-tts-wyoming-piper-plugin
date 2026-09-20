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
- Configurable output format — MP3 (default), OGG, WAV, or FLAC
- Warm-up and connection reuse for fast repeated synthesis
- Two synthesis modes: `pipe` (efficient) and `stream` (streaming delivery)
- Benchmark script to profile format and mode performance on your hardware

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
        output_format: mp3
        voice_compatible: false
        debug: false
```

Or set individual values from the CLI:

```bash
hermes config set plugins.entries.tts-wyoming-piper.settings.voice "en_US-norman-medium"
hermes config set plugins.entries.tts-wyoming-piper.settings.output_format "mp3"
```

> **Note:** After changing plugin settings, restart both the Hermes gateway and desktop app for changes to take effect.

| Setting | Description | Default |
|---------|-------------|---------|
| `host` | Piper server hostname or IP | `localhost` |
| `port` | Wyoming Protocol port | `10200` |
| `voice` | Voice name (empty = server default) | `""` |
| `timeout` | Connection timeout in seconds | `10` |
| `mode` | Synthesis mode: `pipe` or `stream` | `pipe` |
| `output_format` | Output format: `mp3`, `ogg`, `wav`, `flac` | `mp3` |
| `voice_compatible` | Declare output as already voice-bubble compatible (skip Hermes conversion) | `false` |
| `debug` | Enable verbose debug logging | `false` |

### Output Format & Voice Compatibility

Piper always synthesizes raw PCM audio. The plugin converts it to your chosen format using ffmpeg. The two settings that control this work together:

**`output_format`** — What container the audio is written to:

| Format | Use when | File size (6s audio) |
|--------|----------|---------------------|
| `mp3` | Default. Plays everywhere — TUI, desktop, web, Telegram as attachment | ~27 KB |
| `ogg` | Voice bubbles on Telegram, Matrix, WhatsApp, Signal | ~39 KB |
| `wav` | Lossless, no ffmpeg needed (largest) | ~288 KB |
| `flac` | Lossless, compressed (good for archival) | ~163 KB |

**`voice_compatible`** — Controls whether the plugin tells Hermes the output is voice-bubble ready:

- **`false` (default)** — The plugin produces MP3 (or your chosen format). Hermes handles platform-specific conversion: when Telegram asks for a voice bubble, Hermes converts MP3 → OGG/Opus via ffmpeg automatically. When the TUI plays the file, it stays as MP3. This is the right default for most setups.

- **`true`** — The plugin tells Hermes the output is already voice-bubble compatible. Use this when you set `output_format: ogg` and want the plugin to handle conversion, skipping Hermes's own conversion step. Set this when your primary delivery target is a voice-bubble platform (Telegram, Matrix, etc.).

**Quick guide:**

| Your client | Recommended config |
|-------------|-------------------|
| TUI / Desktop only | `output_format: mp3`, `voice_compatible: false` |
| Telegram / Matrix only | `output_format: ogg`, `voice_compatible: true` |
| Both TUI and Telegram | `output_format: mp3`, `voice_compatible: false` (Hermes converts when needed) |

### Synthesis Modes

**`pipe` (default)** — Efficient single-pass conversion:
```
Piper TCP → PCM → ffmpeg → target format
```
Writes PCM directly to ffmpeg stdin, outputs the target format in one pass. No intermediate WAV or MP3 files. Best for reliability and speed.

**`stream`** — Streaming delivery via `TTSProvider.stream()`:
```
Piper TCP chunks → ffmpeg → Opus chunks (yielded incrementally)
```
Implements `TTSProvider.stream()` yielding Opus chunks as they arrive. Currently no Hermes consumer dispatches to `TTSProvider.stream()` — CLI voice mode and the dashboard use `StreamingTTSProvider` (raw PCM), a separate interface (Hermes #47896). Has potential future value once Hermes adds generic `stream()` dispatch.

Both modes support all output formats. Pipe mode is faster in practice (see benchmark results below).

## Benchmarking

The `scripts/benchmark.py` script profiles synthesis performance across formats and modes against your live Piper server. Use it to determine the best config for your hardware and network.

```bash
# Run with defaults (3 runs each, all formats, both modes)
.venv/bin/python3 scripts/benchmark.py

# Custom options
.venv/bin/python3 scripts/benchmark.py \
  --host piper.local \
  --voice en_US-lessac-medium \
  --runs 5 \
  --formats mp3 ogg \
  --modes pipe
```

Sample output from a Raspberry Pi server over LAN:

```
Mode     Format   Runs   Net(s) FFmpeg(s) Total(s)   PCM(KB)  Out(KB) Dur(s)  Speedup
-------------------------------------------------------------------------------------
pipe     mp3       3/3    1.092     0.064    1.155     287.5     26.5   6.68    1.00x
pipe     ogg       3/3    1.092     0.094    1.186     287.5     39.2   6.68    0.97x
pipe     wav       3/3    1.092     0.000    1.092     287.5    287.5   6.68    1.06x
pipe     flac      3/3    1.092     0.035    1.126     287.5    162.6   6.68    1.03x
stream   mp3       3/3    1.206     0.000    1.206     292.5     27.0   6.65    0.96x
stream   ogg       3/3    1.442     0.000    1.442     299.2     40.7   6.93    0.80x
stream   wav       3/3    1.292     0.000    1.292     297.2    297.2   6.85    0.89x
stream   flac      3/3    1.321     0.000    1.321     294.7    164.1   6.86    0.87x
```

Key takeaways from typical benchmarks:

- **Network dominates** — the Wyoming PCM round-trip is the bottleneck; ffmpeg conversion is a small fraction (35–94ms)
- **Pipe is faster than stream** — stream has async queue overhead
- **MP3 is smallest** — best for universal playback
- **All formats produce identical audio quality** — only the container differs

Results are saved to `benchmark_output/benchmark_results.json` for comparison across runs.

Run `--help` for all options:

```bash
.venv/bin/python3 scripts/benchmark.py --help
```

## Usage

Once configured, use TTS as normal:

```
/voice tts
```

Or use the `text_to_speech` tool — it routes through your Piper server automatically.

## Requirements

- Python 3.12+
- Network access to the Piper server
- ffmpeg (for MP3/OGG/FLAC output; WAV works without it)

## How It Works

1. Plugin registers as a TTS provider named `wyoming-piper`
2. On synthesis request, connects to the Wyoming Piper server via TCP
3. Sends `describe` event to discover available voices (used for default voice selection)
4. Sends `synthesize` event with the text
5. Receives `audio-start` → `audio-chunk` × N → `audio-stop` events (always raw PCM)
6. In `pipe` mode: pipes PCM directly to ffmpeg for the configured output format
7. In `stream` mode: yields encoded chunks as they arrive

## Development & Testing

This project uses [uv](https://docs.astral.sh/uv/) for environment management.

```bash
# Create a local venv with the project's Python version
uv venv --python 3.12

# Install runtime and dev dependencies
uv sync --all-extras

# Run unit tests (excludes integration tests by default)
.venv/bin/pytest tests/ -v -m "not integration"

# Run linter
.venv/bin/ruff check __init__.py wyoming_client.py tests/
```

### Integration tests

Integration tests require a running Wyoming Piper server and are skipped by default:

```bash
.venv/bin/pytest tests/test_integration.py -v
```

## Troubleshooting

**Enable debug logging:**
Set `debug: true` in your plugin settings to write detailed logs:
```yaml
plugins:
  entries:
    tts-wyoming-piper:
      settings:
        debug: true
```
Logs are written to `~/.hermes/logs/wyoming-piper-debug.log`. Disable by removing the setting or setting it to `false`.

**Connection refused:**
- Verify Piper server is running: `nc -z piper.local 10200`
- Check firewall rules
- Ensure Wyoming Protocol server is configured (not HTTP)

**No voices found:**
- Check server logs for voice loading errors
- Verify Piper voice models are installed on the server

**Desktop app not picking up changes:**
- Quit and reopen the desktop app (plugins only load at startup)

**Audio cut off or won't play in TUI:**
- Ensure `output_format: mp3` and `voice_compatible: false` (the defaults). OGG files may not play correctly in terminal audio players.

## License

MIT
