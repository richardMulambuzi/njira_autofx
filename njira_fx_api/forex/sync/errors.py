"""
Sync layer error taxonomy.

Additive to ``forex.broker.errors`` (spec §2: "SyncError taxonomy reuses
BrokerErrorCode where applicable"). Provider failures always surface as
``BrokerError``; the sync layer adds codes the broker taxonomy cannot express
(contracts §7.4 + illegal state transitions per contracts §5.2).
"""
from __future__ import annotations

from enum import Enum

from forex.broker.errors import BrokerError, BrokerErrorCode  # re-exported

__all__ = [
    "BrokerError",
    "BrokerErrorCode",
    "SyncError",
    "SyncErrorCode",
]


class SyncErrorCode(str, Enum):
    """Machine-readable sync-layer error categories."""

    #: Illegal state transition (contracts §5.2) or operation not allowed in
    #: the current state.
    INVALID_STATE = "invalid_state"
    #: Engine absent/stopped (contracts §7.4 → HTTP 503).
    ENGINE_NOT_RUNNING = "engine_not_running"
    #: Concurrent backfill for the symbol (contracts §7.4 → HTTP 409).
    BACKFILL_IN_PROGRESS = "backfill_in_progress"
    #: Param/body validation failure (contracts §7.4 → HTTP 400).
    INVALID_REQUEST = "invalid_request"
    #: SYNC_ENGINE_ENABLED=false (spec §10 Phase 0) — engine refused to start.
    ENGINE_DISABLED = "engine_disabled"


class SyncError(Exception):
    """A normalized sync-layer error.

    Mirrors :class:`~forex.broker.errors.BrokerError` shape:
    ``code``, ``message``, optional ``details``; ``to_dict()`` is the wire
    form.
    """

    def __init__(
        self,
        code: SyncErrorCode,
        message: str,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self._message = message
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
            "details": self.details,
        }

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"
