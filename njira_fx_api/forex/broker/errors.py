"""
Normalized broker error types.

Every provider implementation (pepperstone, mock) MUST raise
``BrokerError`` instead of leaking raw SDK/library exceptions. The
``BrokerErrorCode`` enum is the single source of truth for machine-readable
error categories, so callers (controllers, services, tasks) can map errors to
HTTP responses / retry policies without knowing provider internals.

Conventions:
* Never log secret values — only environment variable NAMES.
* ``BrokerError.to_dict()`` is the wire format for the API error envelope.
"""
from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger("forex.broker.errors")


class BrokerErrorCode(str, Enum):
    """Machine-readable error categories raised by broker providers."""

    AUTHENTICATION_ERROR = "authentication_error"
    TOKEN_EXPIRED = "token_expired"
    INVALID_CREDENTIALS = "invalid_credentials"
    RATE_LIMITED = "rate_limited"
    BROKER_UNAVAILABLE = "broker_unavailable"
    ACCOUNT_NOT_FOUND = "account_not_found"
    SYMBOL_NOT_FOUND = "symbol_not_found"
    ORDER_REJECTED = "order_rejected"
    INSUFFICIENT_MARGIN = "insufficient_margin"
    MARKET_CLOSED = "market_closed"
    NETWORK_ERROR = "network_error"
    UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"


class BrokerError(Exception):
    """A normalized, provider-aware trading error.

    Attributes:
        code: Machine-readable :class:`BrokerErrorCode`.
        message: Human-readable description (safe for logs/API responses).
        provider: Name of the provider that raised the error (``None`` when
            the error is infrastructure-level, e.g. config resolution).
        details: Optional structured payload (request ids, rejected payloads,
            missing env var names) — never secret VALUES.
    """

    def __init__(
        self,
        code: BrokerErrorCode,
        message: str,
        provider: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self._message = message
        self.provider = provider
        self.details = details

    @property
    def message(self) -> str:
        """Human-readable description of the failure."""
        return self._message

    def to_dict(self) -> dict:
        """Wire-format representation for API error envelopes."""
        return {
            "error": self.code.value,
            "message": self.message,
            "provider": self.provider,
            "details": self.details,
        }

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"


__all__ = ["BrokerError", "BrokerErrorCode"]
