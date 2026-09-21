#!/usr/bin/env python3
"""Benchmark script for tts-wyoming-piper plugin configurations.

Compares synthesis performance across output formats (mp3, ogg, wav, flac)
and synthesis modes (pipe, stream) to help users determine the best config
for their environment.

Usage:
    .venv/bin/python3 scripts/benchmark.py [--host HOST] [--port PORT]
                                           [--voice VOICE] [--text TEXT]
                                           [--runs N] [--output-dir DIR]

Requires: wyoming, ffmpeg (on PATH), and a running Piper TTS server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

# ---------------------------------------------------------------------------
# Allow running from the repo root without installing the package
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from wyoming.audio import AudioChunk
from wyoming.client import AsyncTcpClient
from wyoming.event import Event
from wyoming.tts import Synthesize, SynthesizeVoice


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class TimingResult:
    mode: str
    format: str
    run: int
    network_s: float = 0.0       # Wyoming PCM round-trip
    ffmpeg_s: float = 0.0        # ffmpeg encoding (pipe mode only)
    total_s: float = 0.0         # wall clock
    pcm_bytes: int = 0
    output_bytes: int = 0
    output_path: str = ""
    audio_duration_s: float = 0.0  # detected audio duration of output
    error: Optional[str] = None


@dataclass
class BenchmarkSummary:
    mode: str
    format: str
    runs: int
    avg_network_s: float = 0.0
    avg_ffmpeg_s: float = 0.0
    avg_total_s: float = 0.0
    avg_pcm_bytes: int = 0
    avg_output_bytes: int = 0
    audio_duration_s: float = 0.0
    results: List[TimingResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Wyoming client helpers
# ---------------------------------------------------------------------------
async def _synth_pcm(
    host: str, port: int, text: str, voice: str, timeout: float
) -> tuple[bytes, int, int, int, float]:
    """Synthesize via Wyoming, return (pcm_bytes, rate, width, channels, network_s)."""
    client = AsyncTcpClient(host, port, connect_timeout=timeout, read_timeout=timeout)
    await client.connect()
    syn = Synthesize(text=text, voice=SynthesizeVoice(name=voice) if voice else None)

    t0 = time.monotonic()
    await client.write_event(syn.event())

    chunks: list[bytes] = []
    rate, width, channels = 22050, 2, 1
    while True:
        event = await client.read_event()
        if event is None:
            break
        if event.type == "audio-start":
            rate = event.data.get("rate", 22050)
            width = event.data.get("width", 2)
            channels = event.data.get("channels", 1)
        elif event.type == "audio-chunk":
            chunks.append(AudioChunk.from_event(event).audio)
        elif event.type in ("audio-stop", "synthesize-stopped"):
            break

    network_s = time.monotonic() - t0
    await client.disconnect()
    return b"".join(chunks), rate, width, channels, network_s


async def _synth_pcm_streaming(
    host: str, port: int, text: str, voice: str, timeout: float
) -> tuple[bytes, int, int, int, float]:
    """Synthesize via Wyoming streaming, return (pcm_bytes, rate, width, channels, network_s)."""
    q: asyncio.Queue = asyncio.Queue()
    _host, _port, _timeout, _text, _voice = host, port, timeout, text, voice

    async def _produce():
        client = AsyncTcpClient(_host, _port, connect_timeout=_timeout, read_timeout=_timeout)
        await client.connect()
        syn = Synthesize(text=_text, voice=SynthesizeVoice(name=_voice) if _voice else None)
        await client.write_event(syn.event())

        format_sent = False
        while True:
            event = await client.read_event()
            if event is None:
                break
            if event.type == "audio-start":
                await q.put(("format", event.data))
            elif event.type == "audio-chunk":
                chunk = AudioChunk.from_event(event)
                if not format_sent:
                    await q.put(("format", {"rate": 22050, "width": 2, "channels": 1}))
                    format_sent = True
                await q.put(("chunk", chunk.audio))
            elif event.type in ("audio-stop", "synthesize-stopped"):
                break
        await client.disconnect()
        await q.put(None)

    t0 = time.monotonic()
    task = asyncio.create_task(_produce())

    chunks: list[bytes] = []
    rate, width, channels = 22050, 2, 1
    format_received = False
    while True:
        item = await q.get()
        if item is None:
            break
        kind, data = item
        if kind == "format" and not format_received:
            rate = data.get("rate", 22050)
            width = data.get("width", 2)
            channels = data.get("channels", 1)
            format_received = True
        elif kind == "chunk":
            chunks.append(data)

    network_s = time.monotonic() - t0
    await task
    return b"".join(chunks), rate, width, channels, network_s


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
def _ffmpeg_encode(
    pcm: bytes, rate: int, channels: int, target_ext: str, out_path: str
) -> tuple[float, int]:
    """Encode raw PCM to target format via ffmpeg. Returns (ffmpeg_s, output_bytes)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    cmd = [ffmpeg, "-y", "-f", "s16le", "-ar", str(rate), "-ac", str(channels), "-i", "pipe:0"]
    if target_ext in ("ogg", "opus"):
        cmd += ["-acodec", "libopus", "-b:a", "48k", "-vbr", "on"]
    elif target_ext == "mp3":
        cmd += ["-acodec", "libmp3lame"]
    elif target_ext == "flac":
        cmd += ["-acodec", "flac"]
    # wav: no extra codec args
    cmd.append(out_path)

    t0 = time.monotonic()
    result = subprocess.run(cmd, input=pcm, capture_output=True, timeout=30, check=False)
    ffmpeg_s = time.monotonic() - t0
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"ffmpeg failed (rc={result.returncode}): {stderr}")
    return ffmpeg_s, os.path.getsize(out_path)


def _detect_audio_duration(path: str) -> float:
    """Use ffprobe to detect the actual audio duration of a file."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", path],
            capture_output=True, timeout=5, check=False,
        )
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0))
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Format mapping (mirrors the plugin's _target_extension)
# ---------------------------------------------------------------------------
EXTENSION_MAP = {
    "mp3": "mp3",
    "ogg": "ogg",
    "opus": "ogg",
    "wav": "wav",
    "flac": "flac",
}


# ---------------------------------------------------------------------------
# Benchmark runners
# ---------------------------------------------------------------------------
def _run_pipe_benchmark(
    host: str, port: int, text: str, voice: str, fmt: str, run_num: int,
    tmp_dir: str, timeout: float, pcm_cache: tuple | None = None,
) -> TimingResult:
    """Pipe mode: full Wyoming round-trip + local ffmpeg."""
    ext = EXTENSION_MAP.get(fmt, "mp3")
    out_path = os.path.join(tmp_dir, f"bench_pipe_{fmt}_run{run_num}.{ext}")

    if pcm_cache is not None:
        pcm, rate, width, channels, network_s = pcm_cache
    else:
        pcm, rate, width, channels, network_s = asyncio.run(
            _synth_pcm(host, port, text, voice, timeout)
        )

    if not pcm:
        return TimingResult(mode="pipe", format=fmt, run=run_num,
                            network_s=network_s, error="No PCM data received")

    if ext in ("wav",):
        # WAV: write directly, no ffmpeg needed
        import wave
        t0 = time.monotonic()
        with open(out_path, "wb") as f, wave.open(f, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(width)
            wf.setframerate(rate)
            wf.writeframes(pcm)
        ffmpeg_s = time.monotonic() - t0
        out_size = os.path.getsize(out_path)
    else:
        ffmpeg_s, out_size = _ffmpeg_encode(pcm, rate, channels, ext, out_path)

    total_s = network_s + ffmpeg_s
    duration = _detect_audio_duration(out_path)

    return TimingResult(
        mode="pipe", format=fmt, run=run_num,
        network_s=network_s, ffmpeg_s=ffmpeg_s, total_s=total_s,
        pcm_bytes=len(pcm), output_bytes=out_size,
        output_path=out_path, audio_duration_s=duration,
    )


def _run_stream_benchmark(
    host: str, port: int, text: str, voice: str, fmt: str, run_num: int,
    tmp_dir: str, timeout: float,
) -> TimingResult:
    """Stream mode: streaming Wyoming + local ffmpeg pipe."""
    ext = EXTENSION_MAP.get(fmt, "mp3")
    out_path = os.path.join(tmp_dir, f"bench_stream_{fmt}_run{run_num}.{ext}")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return TimingResult(mode="stream", format=fmt, run=run_num,
                            error="ffmpeg not found")

    # Build ffmpeg command for streaming
    cmd = [ffmpeg, "-y", "-f", "s16le", "-ar", "22050", "-ac", "1", "-i", "pipe:0"]
    if ext in ("ogg", "opus"):
        cmd += ["-acodec", "libopus", "-b:a", "48k", "-vbr", "on"]
    elif ext == "mp3":
        cmd += ["-acodec", "libmp3lame"]
    elif ext == "flac":
        cmd += ["-acodec", "flac"]
    cmd.append(out_path)

    pcm_chunks: list[bytes] = []

    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)

    async def _stream_to_ffmpeg():
        client = AsyncTcpClient(host, port, connect_timeout=timeout, read_timeout=timeout)
        await client.connect()
        syn = Synthesize(text=text, voice=SynthesizeVoice(name=voice) if voice else None)
        await client.write_event(syn.event())

        rate = 22050
        channels = 1
        while True:
            event = await client.read_event()
            if event is None:
                break
            if event.type == "audio-start":
                rate = event.data.get("rate", 22050)
                channels = event.data.get("channels", 1)
            elif event.type == "audio-chunk":
                chunk = AudioChunk.from_event(event)
                pcm_chunks.append(chunk.audio)
                if proc.stdin:
                    proc.stdin.write(chunk.audio)
            elif event.type in ("audio-stop", "synthesize-stopped"):
                break
        await client.disconnect()

    asyncio.run(_stream_to_ffmpeg())
    if proc.stdin:
        proc.stdin.close()
    proc.wait(timeout=30)
    network_s = time.monotonic() - t0

    out_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    pcm_total = b"".join(pcm_chunks)
    duration = _detect_audio_duration(out_path)

    return TimingResult(
        mode="stream", format=fmt, run=run_num,
        network_s=network_s, ffmpeg_s=0.0, total_s=network_s,
        pcm_bytes=len(pcm_total), output_bytes=out_size,
        output_path=out_path, audio_duration_s=duration,
    )


# ---------------------------------------------------------------------------
# Summary & display
# ---------------------------------------------------------------------------
def _summarize(results: List[TimingResult], runs: int) -> BenchmarkSummary:
    if not results:
        return BenchmarkSummary(mode="", format="", runs=0, avg_total_s=0)
    good = [r for r in results if r.error is None]
    if not good:
        return BenchmarkSummary(
            mode=results[0].mode, format=results[0].format, runs=runs,
            avg_total_s=0,
        )
    return BenchmarkSummary(
        mode=good[0].mode,
        format=good[0].format,
        runs=len(good),
        avg_network_s=sum(r.network_s for r in good) / len(good),
        avg_ffmpeg_s=sum(r.ffmpeg_s for r in good) / len(good),
        avg_total_s=sum(r.total_s for r in good) / len(good),
        avg_pcm_bytes=sum(r.pcm_bytes for r in good) // len(good),
        avg_output_bytes=sum(r.output_bytes for r in good) // len(good),
        audio_duration_s=good[0].audio_duration_s,
        results=good,
    )


def _print_table(summaries: List[BenchmarkSummary], text_len: int) -> None:
    hdr = (
        f"{'Mode':<8} {'Format':<8} {'Runs':>4}  "
        f"{'Net(s)':>7} {'FFmpeg(s)':>9} {'Total(s)':>8}  "
        f"{'PCM(KB)':>8} {'Out(KB)':>8} {'Dur(s)':>6}  {'Speedup':>7}"
    )
    sep = "-" * len(hdr)
    print()
    print(f"  Text length: {text_len} chars")
    print()
    print(f"  {hdr}")
    print(f"  {sep}")

    # Reference: first pipe/mp3 entry for speedup calculation
    ref_total = None
    for s in summaries:
        if s.mode == "pipe" and s.format == "mp3" and s.avg_total_s > 0:
            ref_total = s.avg_total_s
            break

    for s in summaries:
        speedup = ""
        if ref_total and s.avg_total_s > 0:
            ratio = ref_total / s.avg_total_s
            speedup = f"{ratio:.2f}x"

        pcm_kb = f"{s.avg_pcm_bytes / 1024:.1f}" if s.avg_pcm_bytes else "-"
        out_kb = f"{s.avg_output_bytes / 1024:.1f}" if s.avg_output_bytes else "-"
        dur = f"{s.audio_duration_s:.2f}" if s.audio_duration_s else "-"
        net = f"{s.avg_network_s:.3f}" if s.avg_network_s else "-"
        ff = f"{s.avg_ffmpeg_s:.3f}" if s.avg_ffmpeg_s else "0.000"
        tot = f"{s.avg_total_s:.3f}" if s.avg_total_s else "-"
        runs = f"{s.runs}/{s.runs}"

        print(
            f"  {s.mode:<8} {s.format:<8} {runs:>4}  "
            f"{net:>7} {ff:>9} {tot:>8}  "
            f"{pcm_kb:>8} {out_kb:>8} {dur:>6}  {speedup:>7}"
        )

    print(f"  {sep}")
    print()


def _print_recommendation(summaries: List[BenchmarkSummary]) -> None:
    """Print a recommendation based on the benchmark results."""
    print("  ╔══════════════════════════════════════════════════════════════╗")
    print("  ║                    RECOMMENDATION                          ║")
    print("  ╠══════════════════════════════════════════════════════════════╣")

    # Find best by format for pipe mode
    pipe_results = [s for s in summaries if s.mode == "pipe" and s.avg_total_s > 0]
    if pipe_results:
        fastest_pipe = min(pipe_results, key=lambda s: s.avg_total_s)
        print(f"  ║  Fastest pipe mode:  {fastest_pipe.format:<6} "
              f"({fastest_pipe.avg_total_s:.3f}s total)            ║")

    # Stream results
    stream_results = [s for s in summaries if s.mode == "stream" and s.avg_total_s > 0]
    if stream_results:
        fastest_stream = min(stream_results, key=lambda s: s.avg_total_s)
        print(f"  ║  Fastest stream:     {fastest_stream.format:<6} "
              f"({fastest_stream.avg_total_s:.3f}s total)            ║")

    # Format recommendations
    print("  ║                                                                ║")
    print("  ║  Format guide:                                                 ║")
    print("  ║   • mp3  — Universal player support (TUI, desktop, web)       ║")
    print("  ║   • ogg  — Voice bubbles on Telegram/Matrix/WhatsApp          ║")
    print("  ║   • wav  — Lossless, no ffmpeg needed (slowest transfer)      ║")
    print("  ║   • flac — Lossless, compressed (good for archival)           ║")
    print("  ║                                                                ║")
    print("  ║  Mode guide:                                                   ║")
    print("  ║   • pipe   — Full synthesis before playback (reliable)        ║")
    print("  ║   • stream — Chunks arrive during synthesis (lower latency)   ║")
    print("  ║                                                                ║")
    print("  ║  Plugin config (plugins.entries.tts-wyoming-piper.settings):  ║")
    print("  ║   output_format: mp3       # Best for TUI/desktop             ║")
    print("  ║   mode:          pipe      # pipe for reliability              ║")
    print("  ║   voice_compatible: false  # Let Hermes handle OGG conversion ║")
    print("  ╚══════════════════════════════════════════════════════════════╝")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark tts-wyoming-piper: compare formats and modes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="Piper server hostname")
    parser.add_argument("--port", type=int, default=10200, help="Piper server port")
    parser.add_argument("--voice", default="en_US-lessac-medium", help="Voice name")
    parser.add_argument(
        "--text",
        default="The quick brown fox jumps over the lazy dog. This is a benchmark test "
        "of the Wyoming Piper text to speech plugin for the Hermes voice assistant.",
        help="Text to synthesize",
    )
    parser.add_argument("--runs", type=int, default=3, help="Number of runs per config")
    parser.add_argument("--timeout", type=float, default=15, help="Connection timeout seconds")
    parser.add_argument("--output-dir", default=None, help="Directory for output files")
    parser.add_argument(
        "--formats", nargs="+", default=["mp3", "ogg", "wav", "flac"],
        choices=["mp3", "ogg", "wav", "flac"],
        help="Formats to test",
    )
    parser.add_argument(
        "--modes", nargs="+", default=["pipe", "stream"],
        choices=["pipe", "stream"],
        help="Modes to test",
    )

    args = parser.parse_args()

    print()
    print("  ╔══════════════════════════════════════════════════════════════╗")
    print("  ║         tts-wyoming-piper benchmark                        ║")
    print("  ╠══════════════════════════════════════════════════════════════╣")
    print(f"  ║  Server:  {args.host}:{args.port:<38}║")
    print(f"  ║  Voice:   {args.voice:<38}║")
    print(f"  ║  Text:    {len(args.text)} chars{' ' * (34 - len(str(len(args.text))))}║")
    print(f"  ║  Runs:    {args.runs:<38}║")
    print(f"  ║  Formats: {', '.join(args.formats):<38}║")
    print(f"  ║  Modes:   {', '.join(args.modes):<38}║")
    print("  ╚══════════════════════════════════════════════════════════════╝")
    print()

    # Verify ffmpeg
    if not shutil.which("ffmpeg"):
        print("  ERROR: ffmpeg not found on PATH. Install ffmpeg first.")
        sys.exit(1)

    # Verify server connectivity
    print("  Connecting to server...", end=" ", flush=True)
    try:
        pcm, rate, width, channels, _ = asyncio.run(
            _synth_pcm(args.host, args.port, args.text, args.voice, args.timeout)
        )
        print(f"OK ({len(pcm)} PCM bytes, {rate}Hz)")
    except Exception as e:
        print(f"FAILED: {e}")
        sys.exit(1)

    # Warm up: run one synthesis per format to prime any caches
    print("  Warming up...", flush=True)
    with tempfile.TemporaryDirectory() as warm_dir:
        for fmt in args.formats:
            ext = EXTENSION_MAP.get(fmt, "mp3")
            warm_path = os.path.join(warm_dir, f"warm.{ext}")
            if fmt == "wav":
                import wave
                with open(warm_path, "wb") as f, wave.open(f, "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(width)
                    wf.setframerate(rate)
                    wf.writeframes(pcm)
            else:
                _ffmpeg_encode(pcm, rate, channels, ext, warm_path)
    print()

    # Run benchmarks
    all_summaries: List[BenchmarkSummary] = []
    output_dir = args.output_dir or os.path.join(str(_REPO_ROOT), "benchmark_output")
    os.makedirs(output_dir, exist_ok=True)

    # Cache PCM to avoid re-synthesizing for pipe mode
    pcm_data, pcm_rate, pcm_width, pcm_channels, pcm_net = asyncio.run(
        _synth_pcm(args.host, args.port, args.text, args.voice, args.timeout)
    )
    pcm_cache = (pcm_data, pcm_rate, pcm_width, pcm_channels, pcm_net)

    for mode in args.modes:
        for fmt in args.formats:
            print(f"  Benchmarking {mode}/{fmt}...", end=" ", flush=True)
            results: List[TimingResult] = []
            for run in range(1, args.runs + 1):
                try:
                    if mode == "pipe":
                        r = _run_pipe_benchmark(
                            args.host, args.port, args.text, args.voice, fmt,
                            run, output_dir, args.timeout, pcm_cache=pcm_cache,
                        )
                    else:
                        r = _run_stream_benchmark(
                            args.host, args.port, args.text, args.voice, fmt,
                            run, output_dir, args.timeout,
                        )
                    results.append(r)
                except Exception as e:
                    results.append(TimingResult(
                        mode=mode, format=fmt, run=run, error=str(e)
                    ))

            # Check for errors
            errors = [r for r in results if r.error]
            if errors:
                print(f"ERROR: {errors[0].error}")
            else:
                avg = sum(r.total_s for r in results) / len(results)
                print(f"done (avg {avg:.3f}s)")

            all_summaries.append(_summarize(results, args.runs))

    # Print results table
    _print_table(all_summaries, len(args.text))
    _print_recommendation(all_summaries)

    # Save results as JSON
    json_path = os.path.join(output_dir, "benchmark_results.json")
    json_data = []
    for s in all_summaries:
        json_data.append({
            "mode": s.mode, "format": s.format, "runs": s.runs,
            "avg_network_s": round(s.avg_network_s, 4),
            "avg_ffmpeg_s": round(s.avg_ffmpeg_s, 4),
            "avg_total_s": round(s.avg_total_s, 4),
            "avg_pcm_bytes": s.avg_pcm_bytes,
            "avg_output_bytes": s.avg_output_bytes,
            "audio_duration_s": round(s.audio_duration_s, 3),
            "per_run": [
                {"run": r.run, "network_s": round(r.network_s, 4),
                 "ffmpeg_s": round(r.ffmpeg_s, 4), "total_s": round(r.total_s, 4),
                 "pcm_bytes": r.pcm_bytes, "output_bytes": r.output_bytes,
                 "error": r.error}
                for r in s.results
            ],
        })
    with open(json_path, "w") as f:
        json.dump(json_data, f, indent=2)
    print(f"  Results saved to: {json_path}")
    print()


if __name__ == "__main__":
    main()
