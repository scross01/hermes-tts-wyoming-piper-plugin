#!/usr/bin/env python3
"""Compare local Piper TTS vs the remote Wyoming Piper server.

Measures, for the same voice and text:
  - local piper, cold spawn (one process per utterance — model load every time)
  - local piper, warm (long-lived process — how piper-http/wyoming-piper run)
  - remote wyoming pipe mode (full PCM round-trip), incl. plugin-style ffmpeg->mp3
  - remote wyoming stream mode (time to first audio chunk)

Metrics per config: TTFB (time to first audio), total synthesis time,
audio duration, and RTF (real-time factor = synthesis_time / audio_duration).

Usage:
    .venv/bin/python3 scripts/compare_local_vs_remote.py [--runs N] [--host H]
        [--port P] [--voice V] [--model PATH] [--skip-remote] [--skip-local]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import select
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from wyoming.audio import AudioChunk
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice

TEXTS = {
    "short": "The quick brown fox jumps over the lazy dog.",
    "medium": (
        "The quick brown fox jumps over the lazy dog. This is a benchmark test "
        "of the Wyoming Piper text to speech plugin for the Hermes voice assistant."
    ),
    "long": (
        "The quick brown fox jumps over the lazy dog. This is a benchmark test "
        "of the Wyoming Piper text to speech plugin for the Hermes voice assistant. "
        "Speech synthesis quality depends on the voice model, the sample rate, and "
        "the amount of compute available at synthesis time. Running Piper locally "
        "keeps all of the compute on this machine, while a remote server moves the "
        "inference elsewhere and adds network transfer time for the resulting audio."
    ),
}


@dataclass
class Stats:
    label: str
    ttfb_s: list[float] = field(default_factory=list)
    total_s: list[float] = field(default_factory=list)
    duration_s: float = 0.0
    errors: int = 0

    @property
    def rtf(self) -> float:
        if self.duration_s > 0 and self.total_s:
            return statistics.mean(self.total_s) / self.duration_s
        return 0.0


def find_piper_binary() -> str:
    """Resolve the piper binary installed in the uvx piper-tts environment."""
    try:
        r = subprocess.run(
            ["uvx", "--from", "piper-tts", "python", "-c", "import sys; print(sys.executable)"],
            capture_output=True, text=True, timeout=120, check=False,
        )
        venv_bin = Path(r.stdout.strip()).parent
        candidate = venv_bin / "piper"
        if r.returncode == 0 and candidate.exists():
            return str(candidate)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    found = shutil.which("piper")
    if found:
        return found
    return "uvx"  # caller must then prefix args: --from piper-tts piper


def model_sample_rate(model_path: str) -> int:
    cfg_path = model_path + ".json"
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        audio = cfg.get("audio", cfg)
        return int(audio.get("sample_rate", 22050))
    except (OSError, ValueError, KeyError, TypeError):
        return 22050


def pcm_duration(pcm: bytes, rate: int) -> float:
    return len(pcm) / (rate * 2 * 1)  # s16le mono


# ---------------------------------------------------------------------------
# Local piper
# ---------------------------------------------------------------------------
def _spawn_piper(piper_cmd: list[str], model: str) -> subprocess.Popen:
    return subprocess.Popen(
        piper_cmd + ["-m", model, "--output-raw"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def _read_until_gap(proc: subprocess.Popen, first_gap_s: float, quiet_s: float):
    """Read stdout until first byte, then until `quiet_s` of silence. Returns (ttfb, total, pcm)."""
    chunks: list[bytes] = []
    ttfb = None
    t0 = time.monotonic()
    fd = proc.stdout.fileno()
    while True:
        timeout = first_gap_s if ttfb is None else quiet_s
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            break  # quiet period / timeout
        data = os.read(fd, 65536)
        if not data:
            break  # EOF
        if ttfb is None:
            ttfb = time.monotonic() - t0
        chunks.append(data)
    total = time.monotonic() - t0
    return ttfb, total, b"".join(chunks)


def _read_until_quiet(fd: int, quiet_s: float, deadline_s: float) -> bytes:
    """Read fd until quiet_s of silence, EOF, or deadline. Returns bytes read."""
    out: list[bytes] = []
    end_by = time.monotonic() + deadline_s
    while time.monotonic() < end_by:
        ready, _, _ = select.select([fd], [], [], quiet_s)
        if not ready:
            break
        data = os.read(fd, 65536)
        if not data:
            break
        out.append(data)
    return b"".join(out)


def _stop_piper(proc: subprocess.Popen) -> None:
    """Close stdin, wait; kill if it will not exit. Never raises."""
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def bench_local_cold(piper_cmd: list[str], model: str, text: str, runs: int, rate: int) -> Stats:
    stats = Stats(label="local cold (spawn per utterance)")
    # Warm-up spawn: prime the OS page cache for the 60MB model
    proc = _spawn_piper(piper_cmd, model)
    proc.stdin.write(text.encode() + b"\n")
    proc.stdin.close()
    proc.stdout.read()
    proc.wait(timeout=60)

    for _ in range(runs):
        proc = _spawn_piper(piper_cmd, model)
        proc.stdin.write(text.encode() + b"\n")
        proc.stdin.close()  # EOF after one utterance -> clean process exit
        ttfb, total, pcm = _read_until_gap(proc, first_gap_s=30, quiet_s=10)
        # Confirmation drain: if the quiet gap fired mid-utterance, collect the rest.
        pcm += _read_until_quiet(proc.stdout.fileno(), quiet_s=1.0, deadline_s=60)
        _stop_piper(proc)
        if not pcm:
            stats.errors += 1
            continue
        stats.ttfb_s.append(ttfb)
        stats.total_s.append(total)
        stats.duration_s = pcm_duration(pcm, rate)
    return stats


def bench_local_warm(piper_cmd: list[str], model: str, text: str, runs: int, rate: int) -> Stats:
    """Warm: one long-lived piper, one utterance per run.

    Completion detection: quiet gap on stdout. A pause between sentences can
    look like completion, so after each break we confirm with a longer
    confirmation drain: any further audio means we broke mid-utterance and
    must keep draining (timing already recorded at first break).
    """
    stats = Stats(label="local warm (long-lived process)")
    QUIET_S = 0.5
    CONFIRM_S = 1.0
    proc = _spawn_piper(piper_cmd, model)
    try:
        for _ in range(runs):
            t0 = time.monotonic()
            proc.stdin.write(text.encode() + b"\n")
            proc.stdin.flush()
            ttfb = None
            chunks: list[bytes] = []
            fd = proc.stdout.fileno()
            while True:
                ready, _, _ = select.select([fd], [], [], 30 if ttfb is None else QUIET_S)
                if not ready:
                    if ttfb is None:
                        stats.errors += 1
                        _read_until_quiet(fd, quiet_s=CONFIRM_S, deadline_s=60)
                        break
                    break  # quiet -> candidate utterance end
                data = os.read(fd, 65536)
                if not data:
                    break  # EOF
                if ttfb is None:
                    ttfb = time.monotonic() - t0
                chunks.append(data)
            if ttfb is None:
                continue  # stall counted as an error above
            total = time.monotonic() - t0  # recorded at first quiet break
            # Confirmation drain: keep reading while audio keeps coming.
            while True:
                more = _read_until_quiet(fd, quiet_s=CONFIRM_S, deadline_s=60)
                if not more:
                    break
                chunks.append(more)
                total = time.monotonic() - t0
            stats.ttfb_s.append(ttfb)
            stats.total_s.append(total)
            stats.duration_s = pcm_duration(b"".join(chunks), rate)
    finally:
        _stop_piper(proc)
    return stats


# ---------------------------------------------------------------------------
# Remote wyoming
# ---------------------------------------------------------------------------
def bench_remote_pipe(host: str, port: int, voice: str, text: str, runs: int,
                      timeout: float, ffmpeg_path: str | None) -> Stats:
    stats = Stats(label="remote pipe (wyoming -> PCM)")
    for _ in range(runs):
        try:
            t0 = time.monotonic()
            pcm, _rate = _remote_synth(host, port, voice, text, timeout)
            total = time.monotonic() - t0
            if not pcm:
                stats.errors += 1
                continue
            stats.total_s.append(total)
            stats.duration_s = pcm_duration(pcm, _rate)
        except (TimeoutError, OSError, ConnectionError):
            stats.errors += 1
    return stats


def bench_remote_pipe_mp3(host: str, port: int, voice: str, text: str, runs: int,
                          timeout: float, ffmpeg_path: str) -> Stats:
    """The plugin's actual pipe path: wyoming PCM -> ffmpeg -> mp3 file."""
    stats = Stats(label="remote pipe + ffmpeg mp3 (plugin pipe path)")
    for _ in range(runs):
        try:
            t0 = time.monotonic()
            pcm, rate = _remote_synth(host, port, voice, text, timeout)
            if not pcm:
                stats.errors += 1
                continue
            subprocess.run(
                [ffmpeg_path, "-y", "-loglevel", "error", "-f", "s16le",
                 "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                 "-acodec", "libmp3lame", "-f", "mp3", "/dev/null"],
                input=pcm, capture_output=True, timeout=30, check=True,
            )
            stats.total_s.append(time.monotonic() - t0)
            stats.duration_s = pcm_duration(pcm, rate)
        except (TimeoutError, OSError, ConnectionError, subprocess.SubprocessError):
            stats.errors += 1
    return stats


def bench_remote_stream(host: str, port: int, voice: str, text: str, runs: int,
                        timeout: float) -> Stats:
    stats = Stats(label="remote stream (TTFB of first chunk)")
    for _ in range(runs):
        try:
            ttfb, total, pcm, rate = _remote_stream_one(host, port, voice, text, timeout)
            if not pcm:
                stats.errors += 1
                continue
            stats.ttfb_s.append(ttfb)
            stats.total_s.append(total)
            stats.duration_s = pcm_duration(pcm, rate)
        except (TimeoutError, OSError, ConnectionError):
            stats.errors += 1
    return stats


def _remote_synth(host: str, port: int, voice: str, text: str, timeout: float):
    async def run():
        client = AsyncTcpClient(host, port, connect_timeout=timeout, read_timeout=timeout)
        await client.connect()
        syn = Synthesize(text=text, voice=SynthesizeVoice(name=voice) if voice else None)
        await client.write_event(syn.event())
        chunks = []
        rate = 22050
        while True:
            event = await client.read_event()
            if event is None:
                break
            if event.type == "audio-start":
                rate = event.data.get("rate", 22050)
            elif event.type == "audio-chunk":
                chunks.append(AudioChunk.from_event(event).audio)
            elif event.type in ("audio-stop", "synthesize-stopped"):
                break
        await client.disconnect()
        return b"".join(chunks), rate
    return asyncio.run(run())


def _remote_stream_one(host: str, port: int, voice: str, text: str, timeout: float):
    async def run():
        t0 = time.monotonic()
        client = AsyncTcpClient(host, port, connect_timeout=timeout, read_timeout=timeout)
        await client.connect()
        syn = Synthesize(text=text, voice=SynthesizeVoice(name=voice) if voice else None)
        await client.write_event(syn.event())
        ttfb = None
        chunks = []
        rate = 22050
        while True:
            event = await client.read_event()
            if event is None:
                break
            if event.type == "audio-start":
                rate = event.data.get("rate", 22050)
            elif event.type == "audio-chunk":
                if ttfb is None:
                    ttfb = time.monotonic() - t0
                chunks.append(AudioChunk.from_event(event).audio)
            elif event.type in ("audio-stop", "synthesize-stopped"):
                break
        total = time.monotonic() - t0
        await client.disconnect()
        return ttfb, total, b"".join(chunks), rate
    return asyncio.run(run())


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _fmt(vals: list[float], dec: int = 3) -> str:
    if not vals:
        return "-"
    return f"{statistics.mean(vals):.{dec}f}"


def print_report(text_label: str, nchars: int, stats_list: list[Stats]) -> None:
    hdr = (f"  {'Path':<44} {'TTFB(s)':>8} {'Total(s)':>9} "
           f"{'Dur(s)':>7} {'RTF':>7} {'Err':>4}")
    print(f"\n  Text: {text_label} ({nchars} chars)")
    print(f"  {'-' * len(hdr)}")
    print(hdr)
    print(f"  {'-' * len(hdr)}")
    for s in stats_list:
        print(f"  {s.label:<44} {_fmt(s.ttfb_s):>8} {_fmt(s.total_s):>9} "
              f"{s.duration_s:>7.2f} {s.rtf:>7.2f} {s.errors:>4}")
    print(f"  {'-' * len(hdr)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10200)
    parser.add_argument("--voice", default="en_US-lessac-medium")
    parser.add_argument("--model", default=os.path.expanduser(
        "~/.cache/piper/lessac/en_US-lessac-medium.onnx"))
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--texts", nargs="+", default=["short", "medium", "long"],
                        choices=list(TEXTS))
    parser.add_argument("--skip-local", action="store_true")
    parser.add_argument("--skip-remote", action="store_true")
    args = parser.parse_args()

    if not Path(args.model).exists():
        print(f"ERROR: local model not found: {args.model}", file=sys.stderr)
        sys.exit(1)

    piper_bin = find_piper_binary()
    if piper_bin == "uvx":
        piper_cmd = ["uvx", "--from", "piper-tts", "piper"]
    else:
        piper_cmd = [piper_bin]
    print(f"  piper binary: {piper_cmd}")
    print(f"  model: {args.model} (rate={model_sample_rate(args.model)}Hz)")
    print(f"  remote: {args.host}:{args.port} voice={args.voice}")

    local_rate = model_sample_rate(args.model)
    ffmpeg_path = shutil.which("ffmpeg")

    for key in args.texts:
        text = TEXTS[key]
        rows: list[Stats] = []
        if not args.skip_local:
            print(f"\n  [{key}] local piper...", flush=True)
            rows.append(bench_local_cold(piper_cmd, args.model, text, args.runs, local_rate))
            rows.append(bench_local_warm(piper_cmd, args.model, text, args.runs, local_rate))
        if not args.skip_remote:
            print(f"  [{key}] remote wyoming...", flush=True)
            rows.append(bench_remote_pipe(args.host, args.port, args.voice, text,
                                          args.runs, args.timeout, ffmpeg_path))
            if ffmpeg_path:
                rows.append(bench_remote_pipe_mp3(args.host, args.port, args.voice, text,
                                                  args.runs, args.timeout, ffmpeg_path))
            rows.append(bench_remote_stream(args.host, args.port, args.voice, text,
                                            args.runs, args.timeout))
        print_report(key, len(text), rows)


if __name__ == "__main__":
    main()
