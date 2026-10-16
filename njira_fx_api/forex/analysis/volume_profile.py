"""Core Volume Profile engine for njira_auto_fx.

Zero-dependency (stdlib + sibling volume_config). Implements improve.md
§1, §2, §3, §5, §7, §8, §10, §11, §12: binning, developing profiles, POC,
value-area expansion, HVN/LVN detection, thread-safe tick feed.
"""

from __future__ import annotations

import logging
import math
import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from forex.analysis.volume_config import (
    VolumeProfileConfig, SymbolProfileConfig, ProfileMethodology,
    ProfileWindow, DecayMode, get_symbol_config, load_config,
)

logger = logging.getLogger(__name__)

# Methodologies fed on every tick (improve.md §2/§3). WEIGHTED is config-only.
_ACTIVE_METHODOLOGIES = (
    ProfileMethodology.TICK_COUNT,
    ProfileMethodology.BID,
    ProfileMethodology.ASK,
    ProfileMethodology.MID,
    ProfileMethodology.DELTA,
)


@dataclass(frozen=True)
class ProfileBin:
    """One price bin of a volume profile."""
    price: float
    activity: int
    bid_count: int = 0
    ask_count: int = 0
    delta: int = 0
    relative_activity: float = 0.0
    percentage: float = 0.0


class ProfileShape(str, Enum):
    """Qualitative shape of a volume profile (improve.md §13-§19)."""
    BALANCED = "balanced"          # symmetrical, centered POC
    P_SHAPED = "p_shaped"          # concentrated at top (high POC)
    B_SHAPED = "b_shaped"          # concentrated at bottom (low POC)
    D_SHAPED = "d_shaped"          # two peaks with LVN between
    BIMODAL = "bimodal"            # two distinct value areas
    NORMAL = "normal"              # bell curve
    SKEWED_LEFT = "skewed_left"    # tail to the left (lower prices)
    SKEWED_RIGHT = "skewed_right"  # tail to the right (higher prices)


# Default used by reliability scoring when a profile carries no min_ticks hint.
_DEFAULT_MIN_TICKS = 50
# ponytail: session UTC hour windows [start, end)
_SESSION_HOURS = {
    "asian": (0, 8),
    "london": (7, 16),
    "new_york": (12, 21),
    "overlap": (12, 16),
}


def calculate_entropy(bins: dict[float, "ProfileBin"]) -> float:
    """Shannon entropy of bin activity, normalized to [0, 1] (1 = uniform)."""
    total = sum(b.activity for b in bins.values())
    if total == 0 or len(bins) < 2:
        return 0.0
    h = 0.0
    for b in bins.values():
        if b.activity > 0:
            p = b.activity / total
            h -= p * math.log2(p)
    return h / math.log2(len(bins))


def classify_shape(bins: dict[float, "ProfileBin"], poc: float, vah: float,
                   val: float, profile_high: float, profile_low: float) -> ProfileShape:
    """Classify a profile's shape from its activity distribution (improve.md §13-§19)."""
    if not bins:
        return ProfileShape.NORMAL
    prices = sorted(bins.keys())
    acts = [bins[p].activity for p in prices]
    total = sum(acts)
    mean = total / len(acts)
    var = sum((a - mean) ** 2 for a in acts) / len(acts)
    if var == 0:
        return ProfileShape.BALANCED
    std = math.sqrt(var)
    skew = sum((a - mean) ** 3 for a in acts) / len(acts) / std ** 3
    kurt = sum((a - mean) ** 4 for a in acts) / len(acts) / std ** 4

    # Two clusters: a sub-mean valley separating two above-mean peaks.
    peaks = 0
    for i in range(1, len(acts) - 1):
        if acts[i - 1] < acts[i] > acts[i + 1] and acts[i] > mean:
            peaks += 1
    if peaks >= 2:
        return ProfileShape.BIMODAL

    rng = profile_high - profile_low
    if rng > 0:
        pos = (poc - profile_low) / rng
        if pos > 0.75:
            return ProfileShape.P_SHAPED
        if pos < 0.25:
            return ProfileShape.B_SHAPED

    if skew < -0.5:
        return ProfileShape.SKEWED_LEFT
    if skew > 0.5:
        return ProfileShape.SKEWED_RIGHT
    return ProfileShape.NORMAL if 2.0 < kurt < 4.0 else ProfileShape.BALANCED


def calculate_reliability(profile: "VolumeProfile") -> float:
    """Reliability score in [0, 1] from activity, completeness, entropy, age, bins."""
    activity_factor = min(1.0, profile.total_activity / _DEFAULT_MIN_TICKS)
    completeness = 1.0 if profile.data_complete else 0.5
    entropy_factor = 1.0 - calculate_entropy(profile.bins)
    age_min = 0.0
    if profile.calculated_at is not None:
        age_min = (datetime.now(timezone.utc) - profile.calculated_at).total_seconds() / 60.0
    age_factor = max(0.0, 1.0 - age_min / 60.0)
    bin_factor = 1.0 / (1.0 + len(profile.bins) / 50.0)
    score = (activity_factor * 0.3 + completeness * 0.2 + entropy_factor * 0.25
             + age_factor * 0.15 + bin_factor * 0.1)
    return min(1.0, max(0.0, score))


@dataclass
class VolumeProfile:
    """Computed (or developing) volume profile for a symbol/methodology."""
    symbol: str
    methodology: ProfileMethodology
    window_start: datetime
    window_end: Optional[datetime]
    total_activity: int = 0
    poc: float = 0.0
    poc_activity: int = 0
    vah: float = 0.0
    val: float = 0.0
    profile_high: float = 0.0
    profile_low: float = 0.0
    hvn_levels: list[float] = field(default_factory=list)
    lvn_levels: list[float] = field(default_factory=list)
    bins: dict[float, ProfileBin] = field(default_factory=dict)
    developing: bool = True
    data_complete: bool = True
    calculated_at: Optional[datetime] = None
    poc_trajectory: list[float] = field(default_factory=list)
    vah_trajectory: list[float] = field(default_factory=list)
    val_trajectory: list[float] = field(default_factory=list)

    def get_bin(self, price: float) -> Optional[ProfileBin]:
        """Return the bin stored at exactly this price (bin center), if any."""
        return self.bins.get(price)

    def price_position(self, price: float) -> str:
        """Classify price vs value area / POC."""
        if price > self.vah:
            return "above_vah"
        if price < self.val:
            return "below_val"
        if abs(price - self.poc) < 1e-9:
            return "at_poc"
        if price > self.poc:
            return "above_poc"
        if price < self.poc:
            return "below_poc"
        return "inside_value"

    def poc_distance(self, price: float) -> float:
        """Signed distance from price to POC (positive when POC is above price)."""
        return self.poc - price

    def to_dict(self) -> dict:
        """Serialize the profile for API / Pusher payloads."""
        return {
            "symbol": self.symbol,
            "methodology": self.methodology.value,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "total_activity": self.total_activity,
            "poc": self.poc,
            "poc_activity": self.poc_activity,
            "vah": self.vah,
            "val": self.val,
            "profile_high": self.profile_high,
            "profile_low": self.profile_low,
            "hvn_levels": self.hvn_levels,
            "lvn_levels": self.lvn_levels,
            "developing": self.developing,
            "data_complete": self.data_complete,
            "calculated_at": self.calculated_at.isoformat() if self.calculated_at else None,
            "bins": {
                str(p): {
                    "price": b.price,
                    "activity": b.activity,
                    "bid_count": b.bid_count,
                    "ask_count": b.ask_count,
                    "delta": b.delta,
                    "relative_activity": b.relative_activity,
                    "percentage": b.percentage,
                }
                for p, b in self.bins.items()
            },
        }


class PriceBinner:
    """Rounds prices to the nearest bin center for a symbol's bin size."""

    def __init__(self, bin_size: float):
        self._bin_size = bin_size

    def to_bin(self, price: float) -> float:
        """Round price to the nearest bin center."""
        return round(price / self._bin_size) * self._bin_size

    def bin_size(self) -> float:
        """Return the configured bin size."""
        return self._bin_size


@dataclass
class _DevelopingProfile:
    """Internal mutable state for one developing (symbol, methodology) profile."""
    config: VolumeProfileConfig
    bins: dict[float, int] = field(default_factory=lambda: defaultdict(int))
    bid_bins: dict[float, int] = field(default_factory=lambda: defaultdict(int))
    ask_bins: dict[float, int] = field(default_factory=lambda: defaultdict(int))
    total_activity: int = 0
    poc_price: float = 0.0
    poc_activity: int = 0
    profile_high: float = 0.0
    profile_low: float = float("inf")
    window_start: Optional[datetime] = None
    tick_count: int = 0
    last_update: float = 0.0
    last_mid: float = 0.0

    def feed(self, price: float, bid: float, ask: float, timestamp_ms: int, binner: PriceBinner) -> None:
        """Ingest one tick: bin price, update counters, POC, range (bounded)."""
        if self.tick_count >= self.config.max_developing_ticks:
            return  # ponytail: simplest backpressure, freeze at cap
        bin_price = binner.to_bin(price)
        self.bins[bin_price] += 1
        self.total_activity += 1

        # Tick-direction heuristic: uptick lifts ask (aggressive buy), downtick hits bid.
        mid = (bid + ask) / 2.0
        if self.last_mid and mid > self.last_mid:
            self.ask_bins[bin_price] += 1
        elif self.last_mid and mid < self.last_mid:
            self.bid_bins[bin_price] += 1
        self.last_mid = mid

        if self.bins[bin_price] > self.poc_activity:
            self.poc_activity = self.bins[bin_price]
            self.poc_price = bin_price

        if bin_price > self.profile_high:
            self.profile_high = bin_price
        if bin_price < self.profile_low:
            self.profile_low = bin_price

        if self.window_start is None:
            self.window_start = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        self.tick_count += 1
        self.last_update = time.time()

    def _chosen_activity(self, methodology: ProfileMethodology) -> dict[float, tuple[int, int, int]]:
        """Map bin price -> (activity, bid_count, ask_count) for the methodology."""
        prices = set(self.bins) | set(self.bid_bins) | set(self.ask_bins)
        out: dict[float, tuple[int, int, int]] = {}
        for p in prices:
            b = self.bins.get(p, 0)
            bb = self.bid_bins.get(p, 0)
            ab = self.ask_bins.get(p, 0)
            if methodology == ProfileMethodology.BID:
                act = bb
            elif methodology == ProfileMethodology.ASK:
                act = ab
            elif methodology == ProfileMethodology.DELTA:
                act = ab - bb
            else:  # TICK_COUNT, MID
                act = b
            out[p] = (act, bb, ab)
        return out

    def to_profile(self, symbol: str, methodology: ProfileMethodology, engine: "VolumeProfileEngine") -> VolumeProfile:
        """Convert internal state into an immutable VolumeProfile (computes VA/HVN/LVN)."""
        chosen = self._chosen_activity(methodology)
        if not chosen:
            return VolumeProfile(
                symbol=symbol, methodology=methodology,
                window_start=self.window_start or datetime.now(timezone.utc),
                window_end=None, calculated_at=datetime.now(timezone.utc),
            )

        total = sum(a for a, _, _ in chosen.values())
        max_act = max(a for a, _, _ in chosen.values())
        # POC = highest activity; tie-break on lowest price for determinism.
        poc_price = min(p for p, (a, _, _) in chosen.items() if a == max_act)

        bins: dict[float, ProfileBin] = {}
        for p, (act, bb, ab) in chosen.items():
            bins[p] = ProfileBin(
                price=p, activity=act, bid_count=bb, ask_count=ab, delta=ab - bb,
                relative_activity=(act / max_act) if max_act else 0.0,
                percentage=(act / total * 100.0) if total else 0.0,
            )

        hvn, lvn = engine.detect_hvn_lvn(bins)
        vah, val = engine.compute_value_area(bins, poc_price, engine._config.value_area_pct)

        return VolumeProfile(
            symbol=symbol, methodology=methodology,
            window_start=self.window_start, window_end=None,
            total_activity=total, poc=poc_price, poc_activity=max_act,
            vah=vah, val=val,
            profile_high=max(chosen), profile_low=min(chosen),
            hvn_levels=hvn, lvn_levels=lvn, bins=bins,
            developing=True, data_complete=True, calculated_at=datetime.now(timezone.utc),
        )


class VolumeProfileEngine:
    """Thread-safe engine that builds developing volume profiles from ticks."""

    def __init__(self, config: Optional[VolumeProfileConfig] = None):
        self._config = config or load_config()
        self._developing: dict[str, dict[str, _DevelopingProfile]] = defaultdict(
            lambda: defaultdict(lambda: _DevelopingProfile(self._config))
        )
        self._binners: dict[str, PriceBinner] = {}
        self._lock = threading.Lock()
        self._metrics: dict[str, int] = defaultdict(int)
        # ponytail: trajectory history keyed by "symbol:methodology"
        self._trajectories: dict[str, dict[str, list[float]]] = defaultdict(
            lambda: {"poc": [], "vah": [], "val": []}
        )
        # ponytail: capped recent (timestamp_ms, mid) ring for session filtering
        self._recent_ticks: dict[str, list[tuple[int, float]]] = defaultdict(list)

    def _binner(self, symbol: str) -> PriceBinner:
        """Return (and cache) the symbol-aware price binner."""
        b = self._binners.get(symbol)
        if b is None:
            cfg: SymbolProfileConfig = get_symbol_config(symbol, self._config)
            b = PriceBinner(cfg.default_bin_size)
            self._binners[symbol] = b
        return b

    def feed_tick(self, symbol: str, bid: float, ask: float, timestamp_ms: int) -> None:
        """Feed one quote; updates all active methodologies for the symbol."""
        if not self._config.feature_enabled:
            return
        start = time.time()
        binner = self._binner(symbol)
        mid = (bid + ask) / 2.0
        with self._lock:
            for method in _ACTIVE_METHODOLOGIES:
                price = bid if method == ProfileMethodology.BID else (
                    ask if method == ProfileMethodology.ASK else mid)
                self._developing[symbol][method.value].feed(price, bid, ask, timestamp_ms, binner)
            ring = self._recent_ticks[symbol]
            ring.append((timestamp_ms, mid))
            if len(ring) > 2000:  # ponytail: bounded ring
                del ring[:len(ring) - 2000]
        self._metrics["ticks_fed"] += 1
        self._metrics["last_feed_ms"] = int((time.time() - start) * 1000)

    def get_profile(self, symbol: str, methodology: Optional[ProfileMethodology] = None) -> Optional[VolumeProfile]:
        """Return the current developing profile (VA/HVN/LVN computed on demand)."""
        if not self._config.feature_enabled:
            return None
        method = methodology or self._config.default_methodology
        start = time.time()
        with self._lock:
            dev = self._developing.get(symbol, {}).get(method.value)
            if dev is None:
                return None
            profile = dev.to_profile(symbol, method, self)
        # Update POC/VAH/VAL trajectory history (capped at 20) and attach to profile.
        traj = self._trajectories[f"{symbol}:{method.value}"]
        for key, val in (("poc", profile.poc), ("vah", profile.vah), ("val", profile.val)):
            hist = traj[key]
            hist.append(val)
            if len(hist) > 20:
                del hist[:len(hist) - 20]
        profile.poc_trajectory = list(traj["poc"])
        profile.vah_trajectory = list(traj["vah"])
        profile.val_trajectory = list(traj["val"])
        self._metrics["profiles_requested"] += 1
        self._metrics["last_build_ms"] = int((time.time() - start) * 1000)
        return profile

    def compute_value_area(self, bins: dict[float, ProfileBin], poc: float, pct: float) -> tuple[float, float]:
        """Expand outward from POC until pct% of activity is covered; return (VAH, VAL)."""
        if not bins:
            return poc, poc
        prices = sorted(bins.keys())
        if poc in bins:
            idx = prices.index(poc)
        else:
            idx = min(range(len(prices)), key=lambda i: abs(prices[i] - poc))
        target = sum(b.activity for b in bins.values()) * pct / 100.0
        covered = bins[prices[idx]].activity
        included = {prices[idx]}
        lo, hi = idx - 1, idx + 1
        while covered < target and (lo >= 0 or hi < len(prices)):
            above = bins[prices[hi]].activity if hi < len(prices) else None
            below = bins[prices[lo]].activity if lo >= 0 else None
            if above is not None and below is not None:
                if above >= below:
                    covered += above; included.add(prices[hi]); hi += 1
                else:
                    covered += below; included.add(prices[lo]); lo -= 1
            elif above is not None:
                covered += above; included.add(prices[hi]); hi += 1
            elif below is not None:
                covered += below; included.add(prices[lo]); lo -= 1
            else:
                break
        return max(included), min(included)

    def detect_hvn_lvn(self, bins: dict[float, ProfileBin]) -> tuple[list[float], list[float]]:
        """Return (HVN prices, LVN prices) vs mean activity using config thresholds."""
        if not bins:
            return [], []
        mean = sum(b.activity for b in bins.values()) / len(bins)
        hvn_t = self._config.hvn_threshold_pct * mean
        lvn_t = self._config.lvn_threshold_pct * mean
        hvn = [p for p, b in bins.items() if b.activity > hvn_t]
        lvn = [p for p, b in bins.items() if b.activity < lvn_t]
        return sorted(hvn), sorted(lvn)

    def reset(self, symbol: str) -> None:
        """Clear all developing profiles for a symbol."""
        with self._lock:
            self._developing.pop(symbol, None)

    def get_metrics(self) -> dict:
        """Return engine metrics (feed/build counts and latencies)."""
        return dict(self._metrics)

    # --- Wave 2: shape, reliability, migration, acceptance, sessions (§13-§19, §39) ---

    def get_poc_trajectory(self, symbol: str, methodology: Optional[ProfileMethodology] = None,
                           n: int = 20) -> list[float]:
        """Return the last n POC prices for a symbol/methodology (empty if disabled)."""
        if not self._config.feature_enabled:
            return []
        method = methodology or self._config.default_methodology
        return list(self._trajectories[f"{symbol}:{method.value}"]["poc"][-n:])

    def get_value_migration(self, symbol: str, methodology: Optional[ProfileMethodology] = None) -> str:
        """Return value migration direction from POC trajectory (improve.md §14)."""
        if not self._config.feature_enabled:
            return "unknown"
        traj = self.get_poc_trajectory(symbol, methodology)
        if len(traj) < 2:
            return "unknown"
        half = len(traj) // 2
        first = sum(traj[:half]) / max(1, half)
        second = sum(traj[half:]) / max(1, len(traj) - half)
        if second > first * 1.0001:
            return "migrating_up"
        if second < first * 0.9999:
            return "migrating_down"
        return "stable"

    def score_acceptance(self, symbol: str, price: float,
                         timeframe: Optional[ProfileMethodology] = None) -> dict:
        """Score acceptance/rejection of a price vs the value area (improve.md §15)."""
        if not self._config.feature_enabled:
            return {"score": 0.0, "level": "unknown", "evidence": "feature disabled"}
        profile = self.get_profile(symbol, timeframe)
        if profile is None:
            return {"score": 0.0, "level": "unknown", "evidence": "no developing profile"}
        bin_size = self._binner(symbol).bin_size()
        in_va = profile.val <= price <= profile.vah
        near_band = (profile.vah - profile.val) * 0.1 or bin_size
        if in_va:
            bin = profile.get_bin(self._binner(symbol).to_bin(price))
            rel = bin.relative_activity if bin else 0.0
            return {"score": min(1.0, 0.5 + rel * 0.5), "level": "accepted",
                    "evidence": f"price {price} inside value area [{profile.val}, {profile.vah}]"}
        if abs(price - profile.vah) <= near_band or abs(price - profile.val) <= near_band:
            return {"score": 0.3, "level": "testing", "evidence": "price near VA boundary"}
        return {"score": 0.1, "level": "rejected", "evidence": "price outside value area (rejected)"}

    def feed_candle(self, symbol: str, timeframe_seconds: int, open_price: float,
                    high: float, low: float, close: float, timestamp_ms: int) -> None:
        """Feed OHLC as 4 synthetic mid ticks (improve.md §16). timeframe_seconds accepted for API symmetry."""
        if not self._config.feature_enabled:
            return
        eps = 1e-8  # ponytail: symmetric spread so mid == price exactly
        for p in (open_price, high, low, close):
            self.feed_tick(symbol, p - eps, p + eps, timestamp_ms)

    def get_all_profiles(self, symbol: str) -> dict[str, VolumeProfile]:
        """Return all developing profiles for a symbol across methodologies (§17)."""
        if not self._config.feature_enabled:
            return {}
        out: dict[str, VolumeProfile] = {}
        for method in _ACTIVE_METHODOLOGIES:
            prof = self.get_profile(symbol, method)
            if prof is not None:
                out[method.value] = prof
        return out

    def _profile_from_prices(self, symbol: str, methodology: ProfileMethodology,
                             prices: list[float], window_start: datetime) -> Optional[VolumeProfile]:
        """Build a VolumeProfile from a raw price list (used for session filtering)."""
        if not prices:
            return None
        binner = self._binner(symbol)
        counts: dict[float, int] = defaultdict(int)
        for p in prices:
            counts[binner.to_bin(p)] += 1
        total = sum(counts.values())
        max_act = max(counts.values())
        poc = min(p for p, a in counts.items() if a == max_act)
        bins = {
            p: ProfileBin(price=p, activity=a, relative_activity=a / max_act,
                          percentage=a / total * 100.0)
            for p, a in counts.items()
        }
        hvn, lvn = self.detect_hvn_lvn(bins)
        vah, val = self.compute_value_area(bins, poc, self._config.value_area_pct)
        return VolumeProfile(
            symbol=symbol, methodology=methodology, window_start=window_start, window_end=None,
            total_activity=total, poc=poc, poc_activity=max_act, vah=vah, val=val,
            profile_high=max(counts), profile_low=min(counts),
            hvn_levels=hvn, lvn_levels=lvn, bins=bins,
            developing=True, data_complete=True, calculated_at=datetime.now(timezone.utc),
        )

    def get_session_profile(self, symbol: str, session: str) -> Optional[VolumeProfile]:
        """Return a profile built from ticks within a UTC session window (§18)."""
        if not self._config.feature_enabled:
            return None
        bounds = _SESSION_HOURS.get(session)
        if bounds is None:
            return None
        start_h, end_h = bounds
        with self._lock:
            ticks = list(self._recent_ticks.get(symbol, []))
        prices: list[float] = []
        window_start: Optional[datetime] = None
        for ts, mid in ticks:
            dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
            if start_h <= dt.hour < end_h:
                prices.append(mid)
                if window_start is None or dt < window_start:
                    window_start = dt
        return self._profile_from_prices(
            symbol, self._config.default_methodology, prices,
            window_start or datetime.now(timezone.utc))

    # --- Wave 3: confluence / analysis (§27, §30, §31, §32, §33) ---

    def get_confluence_zones(self, symbol: str, price: float,
                             tolerance_pct: float = 0.001) -> list[dict]:
        """Multi-timeframe confluence zones where price meets POC/VAH/VAL (§27)."""
        if not self._config.feature_enabled:
            return []
        try:
            profiles = self.get_all_profiles(symbol)
            tol_band = max(price * tolerance_pct, 1e-9)
            groups: dict[float, dict] = {}
            for method, prof in profiles.items():
                for lvl_type, lvl in (("poc", prof.poc), ("vah", prof.vah), ("val", prof.val)):
                    if lvl <= 0 or price <= 0:
                        continue
                    if abs(price - lvl) / price > tolerance_pct:
                        continue
                    q = round(lvl / tol_band) * tol_band
                    g = groups.setdefault(q, {"center": lvl, "contributors": []})
                    g["contributors"].append(f"{method}_{lvl_type}")
            zones = []
            for g in groups.values():
                c = g["center"]
                zones.append({
                    "price_center": c,
                    "price_range": [c - tol_band, c + tol_band],
                    "score": float(len(g["contributors"])),
                    "contributors": g["contributors"],
                    "normalized_distance": abs(price - c) / price if price else 1.0,
                })
            zones.sort(key=lambda z: z["score"], reverse=True)
            return zones
        except Exception:
            return []

    def get_magnet_score(self, symbol: str, price: float) -> dict:
        """POC magnet attraction strength, 1.0 at POC decaying to 0 at 2x VA width (§30)."""
        if not self._config.feature_enabled:
            return {"score": 0.0, "poc": 0.0, "distance": 0.0, "direction": "unknown"}
        try:
            prof = self.get_profile(symbol)
            if prof is None or prof.poc <= 0:
                return {"score": 0.0, "poc": 0.0, "distance": 0.0, "direction": "unknown"}
            distance = abs(price - prof.poc)
            va_width = (prof.vah - prof.val) or 1e-9
            score = max(0.0, 1.0 - distance / (2.0 * va_width))
            direction = "toward_poc" if (price > prof.vah or price < prof.val) else "away"
            return {"score": score, "poc": prof.poc, "distance": distance, "direction": direction}
        except Exception:
            return {"score": 0.0, "poc": 0.0, "distance": 0.0, "direction": "unknown"}

    def detect_breakout(self, symbol: str, price: float) -> Optional[dict]:
        """Profile breakout with volume confirmation (§31)."""
        if not self._config.feature_enabled:
            return None
        try:
            prof = self.get_profile(symbol)
            if prof is None or prof.vah <= 0 or prof.val <= 0:
                return None
            if price > prof.vah:
                level, btype = prof.vah, "breakout_up"
            elif price < prof.val:
                level, btype = prof.val, "breakout_down"
            else:
                return None
            va_width = (prof.vah - prof.val) or 1e-9
            bin_ = prof.get_bin(self._binner(symbol).to_bin(price))
            mean_act = sum(b.activity for b in prof.bins.values()) / max(1, len(prof.bins))
            confirmed = bin_ is not None and bin_.activity > mean_act
            strength = max(0.0, min(1.0, abs(price - level) / va_width))
            return {"type": btype, "price": price, "level": level,
                    "confirmed": confirmed, "strength": strength}
        except Exception:
            return None

    def detect_reentry(self, symbol: str, price: float) -> Optional[dict]:
        """Profile reentry: POC was outside VA, now price back inside (§32)."""
        if not self._config.feature_enabled:
            return None
        try:
            prof = self.get_profile(symbol)
            if prof is None or prof.vah <= 0 or prof.val <= 0:
                return None
            if not (prof.val <= price <= prof.vah):
                return None
            traj = prof.poc_trajectory[-3:]
            if len(traj) < 2 or not any(p > prof.vah or p < prof.val for p in traj[:-1]):
                return None
            above = traj[-2] > prof.vah
            va_width = (prof.vah - prof.val) or 1e-9
            penetration = min(abs(price - prof.vah), abs(price - prof.val))
            strength = max(0.0, min(1.0, 1.0 - penetration / va_width))
            return {"type": "reentry_down" if above else "reentry_up", "price": price,
                    "vah": prof.vah, "val": prof.val, "strength": strength}
        except Exception:
            return None

    def detect_rotation(self, symbol: str) -> Optional[dict]:
        """Profile rotation: POC oscillates > 1 VA width with direction changes (§33)."""
        if not self._config.feature_enabled:
            return None
        try:
            prof = self.get_profile(symbol)
            if prof is None or prof.vah <= 0 or prof.val <= 0:
                return None
            traj = prof.poc_trajectory
            if len(traj) < 3:
                return None
            va_width = (prof.vah - prof.val) or 1e-9
            diffs = [traj[i + 1] - traj[i] for i in range(len(traj) - 1)]
            cycles = sum(1 for i in range(len(diffs) - 1) if diffs[i] * diffs[i + 1] < 0)
            moved = max(traj) - min(traj)
            if cycles == 0 or moved < va_width:
                return None
            strength = max(0.0, min(1.0, moved / (2.0 * va_width)))
            return {"type": "rotation", "from_price": traj[0], "to_price": traj[-1],
                    "cycles": cycles, "strength": strength}
        except Exception:
            return None


# Global engine registry — populated by TicksEngine at startup, read by API views.
_global_engine: Optional["VolumeProfileEngine"] = None


def set_global_engine(engine: "VolumeProfileEngine") -> None:
    """Register the running engine so API views can read live profiles."""
    global _global_engine
    _global_engine = engine


def get_global_engine() -> Optional["VolumeProfileEngine"]:
    """Return the registered engine (None when not running / feature disabled)."""
    return _global_engine
