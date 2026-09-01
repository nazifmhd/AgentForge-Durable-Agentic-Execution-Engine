"""Deterministic digests for replay verification (ADR-0005).

A step's LLM/tool calls are recorded with a digest of their *request*. On replay
we recompute the digest and compare — a mismatch means the step code changed and
the recorded response may no longer be valid, which we surface rather than
silently trust.
"""

from __future__ import annotations

import hashlib
from typing import Any

import orjson


def canonical_json(value: Any) -> bytes:
    """Stable JSON encoding: sorted keys, no whitespace, UTC datetimes."""
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS | orjson.OPT_UTC_Z)


def digest(value: Any) -> str:
    """SHA-256 hex digest of the canonical JSON encoding of ``value``."""
    return hashlib.sha256(canonical_json(value)).hexdigest()
