"""
Broker/provider abstraction layer.

Public API surface:

* ``ProviderName`` / ``TradingMode`` — canonical provider & mode enums.
* ``TradingProvider`` — the unified provider interface (ABC).
* ``TradingProviderFactory`` — registry, cached singleton, runtime switching.
* ``BrokerError`` / ``BrokerErrorCode`` — normalized error types.
* ``TradingConfig`` — environment-driven, safety-checked configuration.
* ``ProviderHealth`` — health snapshot value object.

Provider implementations live in ``forex.broker.providers`` and are imported
lazily by the factory (never at package import time).
"""
from __future__ import annotations

import logging

from forex.broker.base import TradingProvider
from forex.broker.config import TradingConfig
from forex.broker.errors import BrokerError, BrokerErrorCode
from forex.broker.factory import TradingProviderFactory
from forex.broker.health import ProviderHealth
from forex.broker.models import ProviderName, TradingMode

logger = logging.getLogger("forex.broker")

__all__ = [
    "BrokerError",
    "BrokerErrorCode",
    "ProviderHealth",
    "ProviderName",
    "TradingConfig",
    "TradingMode",
    "TradingProvider",
    "TradingProviderFactory",
]