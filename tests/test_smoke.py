"""Smoke tests for LocalDeck."""

from __future__ import annotations

import localdeck
from localdeck.__main__ import main


def test_package_version_and_cli():
    assert localdeck.__version__ == "0.1.1"
    assert main(["--version"]) == 0
