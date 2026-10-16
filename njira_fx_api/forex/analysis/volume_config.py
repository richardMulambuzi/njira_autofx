"""Zero-dependency Volume Profile configuration module.

Reads all tunables from os.environ with safe defaults. No external deps.
Spec: improve.md §4, §72, §76, §80.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class ProfileMethodology(str, Enum):
    TICK_COUNT = "tick_count"
    BID = "bid"
    ASK = "ask"
    MID = "mid"
    DELTA = "delta"
    WEIGHTED = "weighted"


class ProfileWindow(str, Enum):
    SESSION = "session"
    DAILY = "daily"
    WEEKLY = "weekly"
    ROLLING_1H = "rolling_1h"
    ROLLING_4H = "rolling_4h"
    ROLLING_8H = "rolling_8h"
    ROLLING_24H = "rolling_24h"
    CUSTOM = "custom"


class DecayMode(str, Enum):
    UNIFORM = "uniform"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    HALF_LIFE = "half_life"


@dataclass(frozen=True)
class SymbolProfileConfig:
    symbol: str
    digits: int = 5
    default_bin_size: float = 0.00005
    tick_size: float = 0.00001
    min_ticks: int = 50
    value_area_pct: float = 70.0


@dataclass(frozen=True)
class VolumeProfileConfig:
    feature_enabled: bool
    default_methodology: ProfileMethodology
    bin_mode: str
    decay_mode: DecayMode
    decay_lambda: float
    decay_half_life_minutes: float
    value_area_pct: float
    min_ticks: int
    max_bins_per_profile: int
    max_developing_ticks: int
    cache_ttl_seconds: int
    hvn_threshold_pct: float
    lvn_threshold_pct: float
    profile_expiry_minutes: int
    symbol_configs: dict


# §76 symbol configuration table (digits → bin/tick derived per spec).
DEFAULT_SYMBOL_CONFIGS: dict[str, SymbolProfileConfig] = {
    "EURUSD": SymbolProfileConfig("EURUSD", 5, 0.00005, 0.00001),
    "GBPUSD": SymbolProfileConfig("GBPUSD", 5, 0.00005, 0.00001),
    "USDJPY": SymbolProfileConfig("USDJPY", 3, 0.010, 0.001),
    "XAUUSD": SymbolProfileConfig("XAUUSD", 2, 0.10, 0.01),
    "USDCHF": SymbolProfileConfig("USDCHF", 5, 0.00005, 0.00001),
    "AUDUSD": SymbolProfileConfig("AUDUSD", 5, 0.00005, 0.00001),
    "USDCAD": SymbolProfileConfig("USDCAD", 5, 0.00005, 0.00001),
    "NZDUSD": SymbolProfileConfig("NZDUSD", 5, 0.00005, 0.00001),
    "EURGBP": SymbolProfileConfig("EURGBP", 5, 0.00005, 0.00001),
    "EURJPY": SymbolProfileConfig("EURJPY", 3, 0.010, 0.001),
    "GBPJPY": SymbolProfileConfig("GBPJPY", 3, 0.010, 0.001),
}


def _digits_to_config(symbol: str, digits: int) -> SymbolProfileConfig:
    bins = {5: (0.00005, 0.00001), 3: (0.010, 0.001), 2: (0.10, 0.01), 1: (1.0, 0.1)}
    bin_size, tick_size = bins.get(digits, (0.00005, 0.00001))
    return SymbolProfileConfig(symbol, digits, bin_size, tick_size)


def get_symbol_config(symbol: str, config: VolumeProfileConfig) -> SymbolProfileConfig:
    if symbol in config.symbol_configs:
        return config.symbol_configs[symbol]
    # ponytail: unknown symbol → assume 5-digit forex default
    return _digits_to_config(symbol, 5)


def load_config() -> VolumeProfileConfig:
    def env_bool(key: str, default: bool) -> bool:
        return os.environ.get(key, str(default)).lower() in ("1", "true", "yes", "on")

    def env_enum(key: str, default, cls):
        return cls(os.environ.get(key, default.value))

    return VolumeProfileConfig(
        feature_enabled=env_bool("ENABLE_VOLUME_PROFILE", False),
        default_methodology=env_enum("VP_METHOD", ProfileMethodology.TICK_COUNT, ProfileMethodology),
        bin_mode=os.environ.get("VP_BIN_MODE", "fixed"),
        decay_mode=env_enum("VP_DECAY_MODE", DecayMode.UNIFORM, DecayMode),
        decay_lambda=float(os.environ.get("VP_DECAY_LAMBDA", "0.01")),
        decay_half_life_minutes=float(os.environ.get("VP_DECAY_HALF_LIFE", "30.0")),
        value_area_pct=float(os.environ.get("VP_VALUE_AREA_PCT", "70.0")),
        min_ticks=int(os.environ.get("VP_MIN_TICKS", "50")),
        max_bins_per_profile=1000,
        max_developing_ticks=50_000,
        cache_ttl_seconds=int(os.environ.get("VP_CACHE_TTL", "60")),
        hvn_threshold_pct=1.5,
        lvn_threshold_pct=0.5,
        profile_expiry_minutes=60,
        symbol_configs=DEFAULT_SYMBOL_CONFIGS,
    )
