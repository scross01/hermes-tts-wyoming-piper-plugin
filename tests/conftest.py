import importlib.util
import os
import sys
import types

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _ensure_agent_mock() -> None:
    """Inject a minimal ``agent.tts_provider`` mock into ``sys.modules``.

    The real ``agent.tts_provider`` base class is provided by the Hermes runtime
    and is unavailable in the test virtualenv.

    Because the repository root holds the plugin's own ``__init__.py``, pytest
    also collects it as a package (``<Package tts-wyoming-piper>``) and imports
    that ``__init__.py`` directly while setting up the package -- which happens
    *after* this conftest is imported but before any test runs. The mock (and the
    ``sys.path`` entry below) therefore has to be applied at module-import time
    rather than lazily, otherwise ``from agent.tts_provider import TTSProvider``
    in ``__init__.py`` raises ``ModuleNotFoundError`` during package setup.
    """
    if "agent.tts_provider" not in sys.modules:
        agent_mod = types.ModuleType("agent")
        agent_mod.__path__ = [_REPO_ROOT]
        sys.modules["agent"] = agent_mod

        tts_provider_mod = types.ModuleType("agent.tts_provider")
        tts_provider_mod.__path__ = []

        class TTSProvider:
            """Minimal stand-in for Hermes's TTSProvider base class."""

        tts_provider_mod.TTSProvider = TTSProvider  # type: ignore[attr-defined]
        sys.modules["agent.tts_provider"] = tts_provider_mod


# Make ``wyoming_client`` importable as a top-level module (the absolute-import
# fallback in ``__init__.py``) for both the test modules and pytest's standalone
# package-setup import of the repository-root ``__init__.py``.
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Ensure the ``agent`` mock exists before pytest imports the repo-root package.
_ensure_agent_mock()


def _load_wyoming_piper():
    """Load ``WyomingPiperProvider`` from ``__init__.py`` without hermes-agent.

    Imports ``__init__.py`` as the ``tts_wyoming_piper`` package (mirroring how
    the Hermes runtime loads a plugin) and wires up the relative
    ``wyoming_client`` import so the ``from .wyoming_client import ...``
    statements resolve correctly.
    """
    _ensure_agent_mock()

    _init_path = os.path.join(_REPO_ROOT, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "tts_wyoming_piper",
        _init_path,
        submodule_search_locations=[_REPO_ROOT],
    )
    if spec is None or spec.loader is None:
        raise ImportError("Failed to create spec for __init__.py")

    mod = importlib.util.module_from_spec(spec)
    sys.modules["tts_wyoming_piper"] = mod

    import wyoming_client

    sys.modules["tts_wyoming_piper.wyoming_client"] = wyoming_client
    spec.loader.exec_module(mod)
    return mod
