"""
Broker execution-cost models (trade.md §11) — spread, slippage, commission, swap.

Pure, deterministic given a seed.  All randomness (slippage) draws from an
injected ``random.Random`` so the same seed replays identically (§10.4).  Costs
are returned per trade so the journal can show gross → net.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .instruments import ContractSpec, get_spec


@dataclass
class CostBreakdown:
    """Per-trade cost ledger (trade.md §6 Costs, §11)."""

    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    commission_usd: float = 0.0
    swap_usd: float = 0.0

    @property
    def total(self) -> float:
        return self.spread_cost + self.slippage_cost + self.commission_usd + self.swap_usd

    def to_dict(self) -> dict:
        return {
            "spread_cost": round(self.spread_cost, 4),
            "slippage_cost": round(self.slippage_cost, 4),
            "commission_usd": round(self.commission_usd, 4),
            "swap_usd": round(self.swap_usd, 4),
            "total": round(self.total, 4),
        }


class SpreadModel:
    """Fixed or variable spread, quoted in price units (trade.md §11)."""

    def __init__(self, fixed: float = 0.0, variable_fn=None) -> None:
        self.fixed = fixed
        self._variable_fn = variable_fn  # optional (candle) -> spread

    def price(self, mid: float = 0.0, candle: Optional[dict] = None) -> float:
        if self._variable_fn is not None and candle is not None:
            return float(self._variable_fn(candle))
        return self.fixed


class SlippageModel:
    """
    Slippage in price units, adverse to the trader (trade.md §11).

    Modes: ``fixed`` (constant), ``random`` (seeded uniform in [0, max]),
    ``volatility`` (proportional to the candle's range).
    """

    def __init__(
        self,
        mode: str = "random",
        max_slippage: float = 0.0,
        rng: Optional[random.Random] = None,
    ) -> None:
        if mode not in ("fixed", "random", "volatility"):
            raise ValueError(f"unknown slippage mode: {mode}")
        self.mode = mode
        self.max_slippage = max_slippage
        self._rng = rng or random.Random(0)

    def price(self, candle: Optional[dict] = None) -> float:
        if self.max_slippage <= 0:
            return 0.0
        if self.mode == "fixed":
            return self.max_slippage
        if self.mode == "random":
            return self._rng.uniform(0.0, self.max_slippage)
        # volatility: fraction of the candle's high-low range
        if not candle:
            return 0.0
        rng_px = float(candle["high"]) - float(candle["low"])
        return min(self.max_slippage, self._rng.uniform(0.0, rng_px))


class SwapModel:
    """
    Overnight financing (trade.md §11).  ``per_lot_per_night`` is USD per lot per
    night; positive means the trader PAYS (typical for a long FX carry trade).
    Triple swap on Wednesday is broker convention and modelled here.
    """

    def __init__(
        self,
        per_lot_per_night: float = 0.0,
        triple_on_wednesday: bool = True,
    ) -> None:
        self.per_lot_per_night = per_lot_per_night
        self.triple_on_wednesday = triple_on_wednesday

    def cost(self, lots: float, entry_time: datetime, exit_time: datetime) -> float:
        if self.per_lot_per_night == 0 or entry_time is None or exit_time is None:
            return 0.0
        total = 0.0
        cur = entry_time.date()
        end = exit_time.date()
        one = timezone.utc
        while cur < end:
            nxt = cur.fromordinal(cur.toordinal() + 1)
            day = datetime(nxt.year, nxt.month, nxt.day, tzinfo=one)
            # only charge when a UTC day boundary is crossed
            if entry_time < day <= exit_time:
                mult = 3.0 if (self.triple_on_wednesday and nxt.weekday() == 2) else 1.0
                total += mult * self.per_lot_per_night * lots
            cur = nxt
        return total


class BrokerCostModel:
    """Bundles spread + slippage + commission + swap into one ledger per trade."""

    def __init__(
        self,
        spread: Optional[SpreadModel] = None,
        slippage: Optional[SlippageModel] = None,
        swap: Optional[SwapModel] = None,
        spec: Optional[ContractSpec] = None,
    ) -> None:
        self.spread = spread or SpreadModel(fixed=0.0)
        self.slippage = slippage or SlippageModel(mode="fixed", max_slippage=0.0)
        self.swap = swap or SwapModel()
        self._spec = spec

    def _spec_for(self, symbol: str) -> ContractSpec:
        return self._spec or get_spec(symbol)

    def round_trip_cost(
        self,
        *,
        symbol: str,
        lots: float,
        mid_price: float,
        entry_time: datetime,
        exit_time: datetime,
        entry_candle: Optional[dict] = None,
        exit_candle: Optional[dict] = None,
    ) -> CostBreakdown:
        """
        Full round-trip cost for ``lots``.

        Spread is charged on both sides (open + close) as the half-spread each
        way times pip value; slippage likewise on both sides; commission is
        round-trip (CommissionModel.cost already doubles); swap charged per night.
        """
        spec = self._spec_for(symbol)
        pip_value = spec.pip_value(lots)
        notional = lots * mid_price * (spec.pip_value_per_lot / 10.0)  # USD notional approx

        spread_px = self.spread.price(mid_price, entry_candle)
        slip_entry = self.slippage.price(entry_candle)
        slip_exit = self.slippage.price(exit_candle)

        # Half the spread crosses the spread on entry, half on exit.
        spread_cost = 2.0 * spec.pips(spread_px) * pip_value / 2.0
        slippage_cost = (spec.pips(slip_entry) + spec.pips(slip_exit)) * pip_value
        commission = spec.commission.cost(lots, notional)
        swap = self.swap.cost(lots, entry_time, exit_time)

        return CostBreakdown(
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            commission_usd=commission,
            swap_usd=swap,
        )
