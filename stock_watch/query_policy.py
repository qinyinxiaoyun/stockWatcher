"""Stateless query selection; alias coverage is revisited every 15 minutes.

A nonempty primary result never establishes complete coverage. Retrieval and
fallback remain the caller's responsibility; this module changes no settings.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .news import NewsTarget, SHANGHAI


def is_alias_window(now: datetime) -> bool:
    local = (now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI))
    return local.minute % 15 == 0


def select_queries(target: NewsTarget, now: datetime) -> NewsTarget:
    """Keep all aliases at :00/:15/:30/:45; otherwise select the primary.

    Empty primary queries retain the complete configured query set, allowing
    retrieval to fall back without silently losing alias-only or theme news.
    The original frozen configuration object is never mutated.
    """
    primary = target.query.strip()
    if is_alias_window(now) or not primary:
        return target
    return replace(target, queries=(primary,))


__all__ = ['select_queries', 'is_alias_window']
