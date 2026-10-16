"""
Instrument contract specifications for the trade simulation engine (trade.md §2,
§10.1 ``instruments``).

Pure data + pure helpers — no Django, no I/O.  A ``ContractSpec`` captures what a
broker needs to size a position and price a pip: the pip size, the USD value of
one pip for one standard lot, the lot step/min/max, and an optional commission
model.  Everything downstream (sizing, costs) reads these, so swapping a symbol's
contract or a broker's commission is a data change, not a code change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CommissionModel:
    """Per-broker commission (trade.md §1, §11).

    Exactly one mode is active; ``per_lot`` is the common retail case.
    """

    per_lot: float = 0.0          # USD per lot per side
    per_trade: float = 0.0        # USD per trade (flat)
    percent: float = 0.0          # percent of notional per side
    zero: bool = True             # no commission at all

    def cost(self, lots: float, notional: float) -> float:
        """Round-trip commission for ``lots`` at ``notional`` USD notional."""
        if self.zero:
            return 0.0
        return 2.0 * (self.per_lot * lots + self.per_trade + self.percent / 100.0 * notional)


@dataclass(frozen=True)
class ContractSpec:
    """Broker contract for one instrument (trade.md §2.4 ``Instrument``)."""

    symbol: str
    pip_size: float = 0.0001      # price units per pip (0.0001 for most FX, 0.01 for JPY)
    pip_value_per_lot: float = 10.0   # USD per pip for 1.0 lot
    lot_step: float = 0.01        # broker minimum size increment
    min_lot: float = 0.01         # nano on a $500 account (trade.md §9)
    max_lot: float = 100.0
    commission: CommissionModel = field(default_factory=CommissionModel)

    def pips(self, price_distance: float) -> float:
        """Convert an absolute price distance to pips."""
        if self.pip_size <= 0:
            raise ValueError(f"{self.symbol}: pip_size must be > 0")
        return abs(price_distance) / self.pip_size

    def round_lots(self, lots: float) -> float:
        """Round ``lots`` DOWN to the broker step (never up — up increases risk)."""
        if self.lot_step <= 0:
            return lots
        return (lots // self.lot_step) * self.lot_step

    def pip_value(self, lots: float) -> float:
        """USD value of one pip for ``lots``."""
        return self.pip_value_per_lot * lots


# Default contract table. EURUSD pip value per standard lot is $10; JPY pairs
# carry the same $10/pip at 1 lot despite a 0.01 pip size.  Keep this in sync
# with the broker the live engine talks to (Pepperstone) for realism.
_SPECS: dict[str, ContractSpec] = {
    "EURUSD": ContractSpec("EURUSD", 0.0001, 10.0),
    "GBPUSD": ContractSpec("GBPUSD", 0.0001, 10.0),
    "AUDUSD": ContractSpec("AUDUSD", 0.0001, 10.0),
    "NZDUSD": ContractSpec("NZDUSD", 0.0001, 10.0),
    "USDCHF": ContractSpec("USDCHF", 0.0001, 10.0),
    "USDJPY": ContractSpec("USDJPY", 0.01, 10.0),
    "GBPJPY": ContractSpec("GBPJPY", 0.01, 10.0),
    "AUDJPY": ContractSpec("AUDJPY", 0.01, 10.0),
    "EURJPY": ContractSpec("EURJPY", 0.01, 10.0),
    "XAUUSD": ContractSpec("XAUUSD", 0.1, 10.0),  # gold: 0.1 pip, $10/pip/lot
    # Silver quoted in USD per troy ounce.  Silverspec convention is a 0.001
    # pip unit, so a 1.0 lot moves $10 per 0.001 of price — same $10/pip/lot
    # shape as gold.  Without this, get_spec() fell through to the FX default
    # (0.0001 pip), pricing every silver pip 10x too small and understating
    # spread/slippage cost by the same factor.
    "XAGUSD": ContractSpec("XAGUSD", 0.001, 10.0),
}


def get_spec(symbol: str) -> ContractSpec:
    """Contract for ``symbol``; a sane FX default for unknown symbols."""
    key = (symbol or "").upper()
    spec = _SPECS.get(key)
    if spec is not None:
        return spec
    # Unknown symbol → conservative default so the engine still runs and is
    # auditable, rather than crashing mid-backtest.
    return ContractSpec(symbol=key or "UNKNOWN", pip_size=0.0001, pip_value_per_lot=10.0)


def register_spec(spec: ContractSpec) -> None:
    """Register/override a contract (used by tests and broker config)."""
    _SPECS[spec.symbol.upper()] = spec
