"""Shared test fixtures. Keeps the unit suite hermetic and deterministic."""
import pytest


@pytest.fixture(autouse=True)
def _hermetic_alc(monkeypatch):
    """Never invoke the real alc.exe during unit tests — force the compiler 'unavailable'.

    Tests that exercise the compiler wiring inject a fake runner / monkeypatch discover_compiler.
    """
    monkeypatch.setenv("BC_AGENTIC_DISABLE_COMPILER", "1")
    # Hermetic against shell state: a leaked BC_AGENTIC_SPECS_ROOT from a live
    # session redirects every test's .specs to the external workspaces dir
    # (observed 2026-07-06: 71 phantom failures). Tests opt in explicitly.
    monkeypatch.delenv("BC_AGENTIC_SPECS_ROOT", raising=False)
    yield
