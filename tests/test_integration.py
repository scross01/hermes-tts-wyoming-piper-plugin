"""
Integration tests for WyomingPiperClient network methods.

These require a running Wyoming Piper server and are skipped by default.
Run with: pytest tests/test_integration.py -v --integration
"""

import os
import sys

import pytest

sys_path_inserted = False
if "wyoming_client" not in sys.modules:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    sys_path_inserted = True

from wyoming_client import WyomingPiperClient


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration (deselect with '-m \"not integration\"')",
    )


@pytest.mark.integration
class TestWyomingPiperClientIntegration:
    def test_connect_requires_server(self):
        """Placeholder: run against a real server at host='localhost', port=10200."""
        client = WyomingPiperClient(host="localhost", port=10200)
        with pytest.raises(Exception):  # noqa: B017
            client.connect()


@pytest.mark.integration
class TestStreamConnectionErrorsEndToEnd:
    """Real-network variant of plan-009's connection-refused regression test.

    Deselected by default (`-m "not integration"`); run explicitly with
    `.venv/bin/pytest tests/ -m integration`. The DNS variant was dropped
    entirely: `nonexistent.invalid` resolution is environment-dependent
    (resolver hijacking) in ways that make even a marked test unreliable.
    """

    def test_connection_refused_raises_wyoming_error(self):
        from wyoming_client import WyomingError

        client = WyomingPiperClient(host="127.0.0.1", port=1, timeout=2.0)
        with pytest.raises(WyomingError):
            list(client.synthesize_stream("hello"))
