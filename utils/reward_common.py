# -*- coding: utf-8 -*-
"""
WWM - Reward system shared helpers (referrals, points, lucky draw).
Small, dependency-free helpers used by every reward module.
"""

import uuid
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError  # re-exported for the services

__all__ = ["DuplicateKeyError", "utcnow", "short_id", "long_id", "as_utc"]


def utcnow() -> datetime:
    """Naive UTC 'now' - matches how Motor returns datetimes (tz_aware=False)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(dt: datetime) -> datetime:
    """Make any datetime timezone-aware (UTC) for comparison with Discord times."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def short_id() -> str:
    """8-char id for admin-facing objects (prizes, draws)."""
    return uuid.uuid4().hex[:8]


def long_id() -> str:
    """Full id for ledger entries."""
    return uuid.uuid4().hex
