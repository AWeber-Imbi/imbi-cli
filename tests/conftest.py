"""Fixtures every test gets."""

import os

import pytest


@pytest.fixture(autouse=True)
def _clear_imbi_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the caller's IMBI_* variables out of every test."""
    for name in list(os.environ):
        if name.startswith("IMBI_"):
            monkeypatch.delenv(name)
