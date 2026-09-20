import importlib.util
import os
import sys
import types


def _load_wyoming_piper():
    """Load WyomingPiperProvider from __init__.py without requiring hermes-agent.

    Injects a minimal mock of ``agent.tts_provider`` into ``sys.modules`` so
    the import in ``__init__.py`` succeeds, then loads the module via importlib
    and wires up the relative ``wyoming_client`` import.
    """
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    if "agent" not in sys.modules:
        agent_mod = types.ModuleType("agent")
        agent_mod.__path__ = ["agent"]
        sys.modules["agent"] = agent_mod

    if "agent.tts_provider" not in sys.modules:
        tts_provider_mod = types.ModuleType("agent.tts_provider")
        tts_provider_mod.__path__ = ["agent/tts_provider"]

        class TTSProvider:
            """Minimal stand-in for Hermes's TTSProvider base class."""

        tts_provider_mod.TTSProvider = TTSProvider  # type: ignore[attr-defined]
        sys.modules["agent.tts_provider"] = tts_provider_mod

    _init_path = os.path.join(repo_root, "__init__.py")
    spec = importlib.util.spec_from_file_location(
        "tts_wyoming_piper",
        _init_path,
        submodule_search_locations=[repo_root],
    )
    if spec is None or spec.loader is None:
        raise ImportError("Failed to create spec for __init__.py")

    mod = importlib.util.module_from_spec(spec)
    sys.modules["tts_wyoming_piper"] = mod

    import wyoming_client

    sys.modules["tts_wyoming_piper.wyoming_client"] = wyoming_client
    spec.loader.exec_module(mod)
    return mod
