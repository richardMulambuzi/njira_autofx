"""
forex.sync — broker-independent tick synchronization engine (spec §2).

Public API (spec §2 module map / plan T3):
  * ``SyncConfig``      — env-driven tuning (config.py)
  * ``TickNormalizer``  — provider → canonical MarketTick (normalizer.py)
  * ``SegmentStore``    — segmented numpy rolling window (storage.py)
  * ``SyncEngine``      — the pipeline engine (engine.py)
  * ``create_engine()`` — public factory (spec §2)

Import-safety (spec §10 Phase 0): the package is dead code until
``SYNC_ENGINE_ENABLED=true``. The heavy symbols (``SegmentStore``,
``SyncEngine``, ``create_engine``) are imported lazily via PEP 562
``__getattr__`` so ``import forex.sync`` never triggers the broker/factory
import chain at Django app-load time.
"""
from __future__ import annotations

from forex.sync.config import SyncConfig
from forex.sync.errors import SyncError, SyncErrorCode
from forex.sync.normalizer import TickNormalizer

__all__ = [
    "SegmentStore",
    "SyncConfig",
    "SyncEngine",
    "SyncError",
    "SyncErrorCode",
    "TickNormalizer",
    "create_engine",
]


def __getattr__(name: str) -> object:
    """Lazy attribute resolution (PEP 562) for the heavy symbols."""
    if name == "SegmentStore":
        from forex.sync.storage import SegmentStore

        return SegmentStore
    if name in ("SyncEngine", "create_engine"):
        from forex.sync.engine import SyncEngine, create_engine

        return SyncEngine if name == "SyncEngine" else create_engine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")