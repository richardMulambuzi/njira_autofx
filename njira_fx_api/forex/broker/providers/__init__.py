"""
Provider implementations package.

Each provider (``pepperstone``, ``mock``) implements the
:class:`forex.broker.base.TradingProvider` ABC and is registered lazily by
:class:`forex.broker.factory.TradingProviderFactory` on first use.

Do NOT import provider classes from this package's ``__init__`` — the factory
imports them lazily to avoid circular imports.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("forex.broker.providers")

__all__: list[str] = []