#!/usr/bin/env python3
"""Test script for Wyoming Piper plugin."""

import sys
import os

# Add Hermes venv to path for wyoming package
hermes_venv = os.path.expanduser("~/.hermes/hermes-agent/venv/lib/python3.11/site-packages")
if os.path.exists(hermes_venv):
    sys.path.insert(0, hermes_venv)

# Add plugin to path
sys.path.insert(0, os.path.expanduser("~/.hermes/plugins/hermes-wyoming-piper"))

from wyoming_client import WyomingPiperClient, WyomingConnectionError

HOST = "raspberrypi08"
PORT = 10200

def test_connection():
    """Test basic TCP connection."""
    print(f"Testing connection to {HOST}:{PORT}...")
    try:
        client = WyomingPiperClient(HOST, PORT, timeout=5)
        client.connect()
        print("✓ Connected!")
        client.disconnect()
        return True
    except WyomingConnectionError as e:
        print(f"✗ Connection failed: {e}")
        return False

def test_describe():
    """Test voice discovery."""
    print(f"\nQuerying voices from {HOST}:{PORT}...")
    try:
        client = WyomingPiperClient(HOST, PORT, timeout=5)
        voices = client.describe()
        print(f"✓ Found {len(voices)} voices:")
        for v in voices:
            print(f"  - {v.name} (languages: {v.languages})")
        client.disconnect()
        return voices
    except Exception as e:
        print(f"✗ Describe failed: {e}")
        return []

def test_synthesis(voice=None):
    """Test TTS synthesis."""
    text = "Hello from Hermes! This is a test of the Wyoming Piper integration."
    print(f"\nSynthesizing: '{text}'")
    if voice:
        print(f"Voice: {voice}")

    try:
        client = WyomingPiperClient(HOST, PORT, timeout=10)
        audio = client.synthesize(text, voice=voice)
        client.disconnect()

        # Save test output
        output_path = "/tmp/wyoming_piper_test.wav"
        with open(output_path, "wb") as f:
            f.write(audio)
        print(f"✓ Synthesis successful! Saved to {output_path}")
        print(f"  Audio size: {len(audio)} bytes")
        return True
    except Exception as e:
        print(f"✗ Synthesis failed: {e}")
        return False

if __name__ == "__main__":
    if not test_connection():
        sys.exit(1)

    voices = test_describe()
    if not voices:
        print("\nNo voices found, skipping synthesis test")
        sys.exit(1)

    # Test with first available voice
    test_synthesis(voice=voices[0].name if voices else None)
