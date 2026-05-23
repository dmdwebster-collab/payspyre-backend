"""Deterministic synthetic-data helpers for the mock adapters.

Same input email -> same numbers, always (Hard Rules #10 / #11). Uses SHA-256 so
results are stable across processes (Python's built-in ``hash`` is randomized per
run and must not be used here).
"""
from __future__ import annotations

from hashlib import sha256
from typing import Optional


def seed_from_email(email: Optional[str]) -> int:
    """Stable non-negative integer seed derived from an email (or 'anonymous')."""
    basis = (email or "anonymous").strip().lower()
    return int(sha256(basis.encode("utf-8")).hexdigest(), 16)


def scaled(seed: int, salt: str, lo: int, hi: int) -> int:
    """Deterministically map ``seed`` (with a ``salt``) into inclusive [lo, hi]."""
    if hi < lo:
        raise ValueError("hi must be >= lo")
    digest = int(sha256(f"{salt}:{seed}".encode("utf-8")).hexdigest(), 16)
    return lo + (digest % (hi - lo + 1))
