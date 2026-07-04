"""Shared test fixtures. Keeps the unit suite hermetic and deterministic."""
import pytest


@pytest.fixture(autouse=True)
def _hermetic_alc(monkeypatch):
    """Never invoke the real alc.exe during unit tests — force the compiler 'unavailable'.

    Tests that exercise the compiler wiring inject a fake runner / monkeypatch discover_compiler.
    """
    monkeypatch.setenv("BC_AGENTIC_DISABLE_COMPILER", "1")
    yield
