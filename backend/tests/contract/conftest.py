"""Shared fixtures for snapshot-adapter contract tests.

These tests parse committed, audited fixture bundles under ``data/`` and
compare the adapter output against human-reviewed ``expected.json``. They run
fully offline — no network, no database, no model keys — so the repository
root is the only shared fixture required.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def project_root() -> Path:
    """Return the absolute repository root (the dir containing ``data/``)."""
    candidate = Path(__file__).resolve().parents[3]
    assert (candidate / "data").exists(), f"project root {candidate} has no data/ dir"
    return candidate
