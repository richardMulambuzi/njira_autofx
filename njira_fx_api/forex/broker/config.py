"""
Centralized trading configuration.

Reads provider selection and execution mode from the environment
(``TRADING_PROVIDER`` / ``TRADING_MODE``) and validates each provider's
credentials WITHOUT ever exposing secret values — only environment variable
NAMES and validity booleans are surfaced.

This module is the safety layer that makes accidental live trading
impossible: ``safety_check()`` returns human-readable warnings whenever the
active provider is misconfigured for the requested mode.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from forex.broker.models import ProviderName, TradingMode

logger = logging.getLogger("forex.broker.config")

# Environment variables required per provider (names only — never values).
PEPPERSTONE_REQUIRED_ENV = [
    "PEPPERSTONE_CLIENT_ID",
    "PEPPERSTONE_CLIENT_SECRET",
    "PEPPERSTONE_ACCESS_TOKEN",
    "PEPPERSTONE_REFRESH_TOKEN",
]

# Placeholder values that count as "missing" credentials (e.g. values left in
# .env from a template). Any value starting with "your_" is treated as a
# placeholder.
_PLACEHOLDER_PREFIX = "your_"
_PLACEHOLDER_EXACT = {"your_access_token", "your_api_token", "your_app_id"}


def _is_placeholder(value: str) -> bool:
    """True when a value looks like an unsubstituted template placeholder."""
    normalized = value.strip().lower()
    if not normalized:
        return True
    if normalized in _PLACEHOLDER_EXACT:
        return True
    return normalized.startswith(_PLACEHOLDER_PREFIX)


@dataclass
class ProviderConfig:
    """Per-provider configuration summary.

    Mutable (not frozen) because ``validate_credentials`` refreshes
    ``credentials_valid`` / ``validation_errors`` at runtime.
    """

    name: str
    enabled: bool
    mode: TradingMode
    requires: list[str] = field(default_factory=list)
    credentials_valid: bool = False
    validation_errors: list[str] = field(default_factory=list)


class TradingConfig:
    """Runtime trading configuration resolved from the environment.

    Attributes:
        active_provider: Provider selected via ``TRADING_PROVIDER``
            (default ``"mock"``).
        mode: Execution mode via ``TRADING_MODE`` (default ``DEMO``; invalid
            values fall back to ``DEMO`` with a warning).
        pepperstone / mock: Per-provider :class:`ProviderConfig`.
    """

    def __init__(
        self,
        active_provider: str = "mock",
        mode: TradingMode = TradingMode.DEMO,
        pepperstone: ProviderConfig | None = None,
        mock: ProviderConfig | None = None,
    ) -> None:
        self.active_provider = active_provider
        self.mode = mode
        self.pepperstone = pepperstone or ProviderConfig(
            name="pepperstone",
            enabled=False,
            mode=mode,
            requires=list(PEPPERSTONE_REQUIRED_ENV),
        )
        self.mock = mock or ProviderConfig(
            name="mock",
            enabled=True,
            mode=mode,
            requires=[],
            credentials_valid=True,
        )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> TradingConfig:
        """Build a :class:`TradingConfig` from the process environment.

        Reads ``TRADING_PROVIDER`` and ``TRADING_MODE``, then validates each
        provider's credentials (present + non-empty + not a placeholder).
        """
        provider_raw = os.environ.get("TRADING_PROVIDER", "mock").strip().lower()
        active_provider = provider_raw or "mock"
        if active_provider not in ProviderName._value2member_map_:
            logger.warning(
                "TRADING_PROVIDER=%r is not a known provider; the factory will "
                "reject it with UNKNOWN_PROVIDER_ERROR",
                provider_raw,
            )

        mode_raw = os.environ.get("TRADING_MODE", "demo").strip().lower()
        try:
            mode = TradingMode(mode_raw or "demo")
        except ValueError:
            logger.warning(
                "Invalid TRADING_MODE=%r (expected demo|paper|live); "
                "defaulting to DEMO",
                mode_raw,
            )
            mode = TradingMode.DEMO

        pepperstone = cls._build_provider_config(
            name="pepperstone", mode=mode, requires=PEPPERSTONE_REQUIRED_ENV
        )
        mock = ProviderConfig(
            name="mock",
            enabled=True,
            mode=mode,
            requires=[],
            credentials_valid=True,
        )

        return cls(
            active_provider=active_provider,
            mode=mode,
            pepperstone=pepperstone,
            mock=mock,
        )

    @staticmethod
    def _build_provider_config(
        name: str, mode: TradingMode, requires: list[str]
    ) -> ProviderConfig:
        """Build a ProviderConfig, validating the required env vars."""
        valid, errors = TradingConfig._validate_env(requires)
        return ProviderConfig(
            name=name,
            enabled=valid,
            mode=mode,
            requires=list(requires),
            credentials_valid=valid,
            validation_errors=errors,
        )

    @staticmethod
    def _validate_env(requires: list[str]) -> tuple[bool, list[str]]:
        """Check that every required env var is present, non-empty, non-placeholder.

        Returns ``(valid, errors)`` where errors lists only env var NAMES.
        """
        errors: list[str] = []
        for var in requires:
            value = os.environ.get(var, "")
            if not value.strip():
                errors.append(f"{var} is missing or empty")
            elif _is_placeholder(value):
                errors.append(
                    f"{var} is still a placeholder — set a real value in .env"
                )
        return (not errors, errors)

    # ------------------------------------------------------------------
    # Runtime validation
    # ------------------------------------------------------------------
    def get_provider(self, provider_name: str) -> ProviderConfig | None:
        """Return the :class:`ProviderConfig` for a provider name, or ``None``."""
        for provider in (self.pepperstone, self.mock):
            if provider.name == provider_name:
                return provider
        return None

    def validate_credentials(self, provider_name: str) -> tuple[bool, list[str]]:
        """Re-validate a provider's credentials against the live environment.

        Refreshes the stored :class:`ProviderConfig` and returns
        ``(valid, errors)``.
        """
        provider = self.get_provider(provider_name)
        if provider is None:
            return False, [f"Unknown provider '{provider_name}'"]
        valid, errors = self._validate_env(provider.requires)
        provider.credentials_valid = valid
        provider.validation_errors = errors
        return valid, errors

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------
    def is_live(self) -> bool:
        """True when the configured mode is LIVE."""
        return self.mode == TradingMode.LIVE

    def safety_check(self) -> list[str]:
        """Return human-readable safety warnings for the current configuration.

        This is the guard rail that makes accidental live trading impossible:
        * LIVE mode with the mock provider → warning (no real execution).
        * LIVE mode with invalid credentials → warning listing env var names.
        * demo/paper mode → note that live trading is disabled.
        """
        warnings: list[str] = []
        provider = self.get_provider(self.active_provider)

        if self.is_live():
            if self.active_provider == ProviderName.MOCK.value:
                warnings.append(
                    "TRADING_MODE=live but TRADING_PROVIDER=mock — no real "
                    "orders will be sent to a broker"
                )
            else:
                warnings.append(
                    "TRADING_MODE=live — real orders may be placed. Confirm "
                    "TRADING_PROVIDER before continuing."
                )
        else:
            warnings.append(
                f"Trading is in {self.mode.value} mode — live trading is disabled."
            )

        if provider is not None and not provider.credentials_valid:
            warnings.append(
                f"Provider '{self.active_provider}' credentials are invalid — "
                f"check env vars: {', '.join(provider.requires)}"
            )
        return warnings

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        """Serializable summary.

        NEVER includes secret values — only env var NAMES and validity
        booleans.
        """
        return {
            "active_provider": self.active_provider,
            "mode": self.mode.value,
            "is_live": self.is_live(),
            "providers": {
                provider.name: {
                    "enabled": provider.enabled,
                    "mode": provider.mode.value,
                    "credentials_valid": provider.credentials_valid,
                    "validation_errors": list(provider.validation_errors),
                    "requires": list(provider.requires),
                }
                for provider in (self.pepperstone, self.mock)
            },
            "safety_warnings": self.safety_check(),
        }

    def __repr__(self) -> str:
        return (
            f"TradingConfig(active_provider={self.active_provider!r}, "
            f"mode={self.mode.value!r})"
        )


__all__ = [
    "PEPPERSTONE_REQUIRED_ENV",
    "ProviderConfig",
    "TradingConfig",
]