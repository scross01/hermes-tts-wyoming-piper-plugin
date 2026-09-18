# tts-wyoming-piper

Hermes TTS plugin that connects to a remote Piper TTS server via Wyoming Protocol (TCP).

## Architecture

```
Hermes text_to_speech tool
  → tts_tool_plugins._dispatch_to_plugin_provider()
    → WyomingPiperProvider.synthesize()          # __init__.py
      → WyomingPiperClient.synthesize_stream()   # wyoming_client.py
        → new event loop + AsyncTcpClient in worker thread
          → Piper server (raspberrypi08:10200)
```

- `__init__.py` — `WyomingPiperProvider` (TTSProvider subclass), `register(ctx)` entry point
- `wyoming_client.py` — `WyomingPiperClient`, Wyoming Protocol TCP client
- `plugin.yaml` — plugin manifest + config schema (host, port, voice, timeout, mode, debug)
- `config.yaml` — local development config (not shipped)

## Synthesis Modes

- **`pipe`** (default) — PCM → ffmpeg → target format in one pass
- **`stream`** — streaming chunks via `TTSProvider.stream()` for voice bubbles

Mode is set in `plugins.entries.tts-wyoming-piper.settings.mode`.

## Key Gotchas

### Event loops (fixed in v0.2.1)

The wyoming library's `AsyncTcpClient` binds its transports to the event loop where
`asyncio.open_connection()` was called. Using the client from a different loop triggers
"Future attached to a different loop" errors.

**Rule:** `synthesize_stream()` must create its own `AsyncTcpClient` + event loop in the
worker thread. Never reuse a client connected on the main thread's loop.

### Plugin loading

Plugins load at gateway/desktop startup only. Code changes require restarting both
the gateway (`hermes gateway restart`) and the desktop app.

### Debug logging

Gated behind `debug: true` in plugin settings. When off, `_debug()` is a no-op.
When on, writes to `~/.hermes/logs/wyoming-piper-debug.log`.

## Deploy

```bash
git add -A && git commit -m "fix: ..."
git tag -a vX.Y.Z -m "vX.Y.Z: ..."
git push origin main --tags
```

Bump `version` in `plugin.yaml` before tagging.
