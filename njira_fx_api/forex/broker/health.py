"""
Provider health monitoring.

Each provider instance owns a :class:`ProviderHealthMonitor` which tracks
authentication/connection status, API availability, last successful request,
last market-data update and last error. ``snapshot()`` produces a
:class:`ProviderHealth` value object that the API layer can expose without
leaking internal state or secrets.

Timestamps are timezone-aware UTC; ``to_dict()`` renders them as ISO-8601
strings (``None``-safe) for JSON serialization.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger("forex.broker.health")

# Substring hints used by ``record_error`` to auto-degrade statuses. These are
# heuristic only — providers may set explicit statuses via the setters.
_AUTH_FAILURE_HINTS = ("auth", "token", "credential", "login", "password")
_CONNECTION_FAILURE_HINTS = (
    "network",
    "timeout",
    "unavailable",
    "disconnect",
    "connection refused",
)


@dataclass(frozen=True)
class ProviderHealth:
    """Immutable snapshot of a provider's operational health."""

    provider: str
    environment: str  # "demo" | "paper" | "live"
    authentication_status: str  # e.g. "authenticated" | "waiting_for_authentication" | "authentication_failed" | "not_required"
    connection_status: str  # e.g. "connected" | "disconnected" | "error"
    api_available: bool
    last_successful_request: datetime | None = None
    last_market_data_update: datetime | None = None
    account_sync_status: str = "never_synced"  # e.g. "healthy" | "stale" | "never_synced"
    last_error: str | None = None

    def to_dict(self) -> dict:
        """JSON-serializable representation (ISO-8601 datetimes, None-safe)."""
        return {
            "provider": self.provider,
            "environment": self.environment,
            "authentication_status": self.authentication_status,
            "connection_status": self.connection_status,
            "api_available": self.api_available,
            "last_successful_request": _to_iso(self.last_successful_request),
            "last_market_data_update": _to_iso(self.last_market_data_update),
            "account_sync_status": self.account_sync_status,
            "last_error": self.last_error,
        }


def _to_iso(value: datetime | None) -> str | None:
    """Render a datetime as ISO-8601, or ``None`` when absent."""
    return value.isoformat() if value is not None else None


class ProviderHealthMonitor:
    """Mutable, provider-owned health tracker.

    Providers create one instance per session and update it as they operate;
    ``snapshot()`` is the read-only view consumed by monitoring endpoints and
    the health gate.
    """

    def __init__(self, provider_name: str, environment: str) -> None:
        self.provider_name = provider_name
        self.environment = environment
        self._last_successful_request: datetime | None = None
        self._last_market_data_update: datetime | None = None
        self._last_error: str | None = None
        self._authentication_status: str = "not_required"
        self._connection_status: str = "disconnected"
        self._api_available: bool = False
        self._account_sync_status: str = "never_synced"

    # -- event recorders ---------------------------------------------------
    def record_success(self, operation: str) -> None:
        """Record a successful API operation (updates last_successful_request)."""
        self._last_successful_request = datetime.now(timezone.utc)
        logger.debug(
            "Provider %s: successful operation %s", self.provider_name, operation
        )

    def record_market_data(self) -> None:
        """Record that market data was received (updates last_market_data_update)."""
        self._last_market_data_update = datetime.now(timezone.utc)
        logger.debug("Provider %s: market data update received", self.provider_name)

    def record_error(self, message: str) -> None:
        """Record a failure and heuristically degrade auth/connection status.

        The status degradation is best-effort keyword matching; providers that
        need precision should call the explicit setters instead.
        """
        self._last_error = message
        lowered = message.lower()
        if any(hint in lowered for hint in _AUTH_FAILURE_HINTS):
            self._authentication_status = "authentication_failed"
        if any(hint in lowered for hint in _CONNECTION_FAILURE_HINTS):
            self._connection_status = "error"
        logger.warning(
            "Provider %s recorded error: %s", self.provider_name, message
        )

    # -- explicit status setters -------------------------------------------
    def set_authentication_status(self, status: str) -> None:
        """Set the authentication status explicitly."""
        self._authentication_status = status

    def set_connection_status(self, status: str) -> None:
        """Set the connection status explicitly."""
        self._connection_status = status

    def set_account_sync_status(self, status: str) -> None:
        """Set the account sync status explicitly."""
        self._account_sync_status = status

    def set_api_available(self, available: bool) -> None:
        """Mark the provider API as available/unavailable."""
        self._api_available = available

    # -- snapshot -----------------------------------------------------------
    def snapshot(self) -> ProviderHealth:
        """Return the current health snapshot."""
        return ProviderHealth(
            provider=self.provider_name,
            environment=self.environment,
            authentication_status=self._authentication_status,
            connection_status=self._connection_status,
            api_available=self._api_available,
            last_successful_request=self._last_successful_request,
            last_market_data_update=self._last_market_data_update,
            account_sync_status=self._account_sync_status,
            last_error=self._last_error,
        )


__all__ = ["ProviderHealth", "ProviderHealthMonitor"]
