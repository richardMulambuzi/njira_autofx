"""
Provider registry & factory.

The factory is the runtime entry point for provider selection and switching:

* ``create()``  — instantiate a provider by name (lazy-registers the built-in
  providers on first use to avoid circular imports).
* ``get_active()`` — cached singleton accessor used by the rest of the app.
* ``switch()`` — runtime provider switching: disconnects the current provider,
  clears the cache, and activates the new one.

Provider classes are imported lazily inside ``create()`` so this module never
imports ``forex.broker.providers.*`` at import time.
"""
from __future__ import annotations

import logging

from forex.broker.base import TradingProvider
from forex.broker.config import TradingConfig
from forex.broker.errors import BrokerError, BrokerErrorCode

logger = logging.getLogger("forex.broker.factory")

# Canonical built-in provider names (used to decide when lazy registration is
# worth attempting). Must match the ProviderName enum values.
_BUILTIN_PROVIDER_NAMES = frozenset({"pepperstone", "mock"})


class TradingProviderFactory:
    """Registry + factory for :class:`TradingProvider` implementations."""

    _registry: dict[str, type[TradingProvider]] = {}
    _active: TradingProvider | None = None
    _builtins_registered: bool = False

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------
    @classmethod
    def register(cls, name: str, provider_class: type[TradingProvider]) -> None:
        """Register a provider class under a canonical name."""
        if not issubclass(provider_class, TradingProvider):
            raise TypeError(
                f"Provider class {provider_class.__name__} must subclass "
                "TradingProvider"
            )
        if name in cls._registry:
            logger.info(
                "Replacing registered provider %r with %s",
                name,
                provider_class.__name__,
            )
        cls._registry[name] = provider_class
        logger.debug("Registered provider %r -> %s", name, provider_class.__name__)

    @classmethod
    def registered(cls) -> list[str]:
        """Return the names of all registered providers."""
        return sorted(cls._registry)

    @classmethod
    def _ensure_registered(cls) -> None:
        """Lazily register the built-in providers (first call only).

        Imports are deferred to avoid circular imports between the factory and
        the provider implementations.

        Raises:
            BrokerError(UNKNOWN_PROVIDER_ERROR): if a provider implementation
                module is missing (keeps ``create()`` from leaking raw
                ``ImportError``).
        """
        if cls._builtins_registered:
            return
        try:
            from forex.broker.providers.mock import MockProvider
            from forex.broker.providers.pepperstone import PepperstoneProvider
        except ImportError as exc:
            raise BrokerError(
                BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
                "Provider implementation modules are not installed "
                f"(import failed: {exc.name or exc}). Ensure "
                "forex.broker.providers.{pepperstone,mock} exist.",
            ) from exc

        cls.register(PepperstoneProvider.name, PepperstoneProvider)
        cls.register(MockProvider.name, MockProvider)
        cls._builtins_registered = True

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        provider_name: str | None = None,
        config: TradingConfig | None = None,
    ) -> TradingProvider:
        """Instantiate a provider.

        Args:
            provider_name: Provider key; defaults to ``config.active_provider``.
            config: Trading configuration; defaults to ``TradingConfig.from_env()``.

        Raises:
            BrokerError(UNKNOWN_PROVIDER_ERROR): if the provider is not
                registered (or its implementation module is missing).
        """
        config = config or TradingConfig.from_env()
        name = (provider_name or config.active_provider).strip().lower()

        provider_class = cls._registry.get(name)
        if provider_class is None and name in _BUILTIN_PROVIDER_NAMES:
            # Built-in not registered yet — lazily import the implementations.
            cls._ensure_registered()
            provider_class = cls._registry.get(name)

        if provider_class is None:
            raise BrokerError(
                BrokerErrorCode.UNKNOWN_PROVIDER_ERROR,
                f"Provider '{name}' is not registered. "
                f"Registered providers: {', '.join(cls.registered()) or 'none'}",
                provider=name,
            )

        provider_config = config.get_provider(name)
        if provider_config is not None and not provider_config.enabled:
            logger.warning(
                "Provider '%s' is marked disabled in config (credentials "
                "invalid) — connection will likely fail with an auth error",
                name,
            )

        logger.info("Creating provider %s (mode=%s)", name, config.mode.value)
        return provider_class(config=config)

    # ------------------------------------------------------------------
    # Active provider (cached singleton)
    # ------------------------------------------------------------------
    @classmethod
    def get_active(cls, config: TradingConfig | None = None) -> TradingProvider:
        """Return the cached active provider, creating it on first use.

        Args:
            config: Optional config to inject (used by tests / bootstrap).
        """
        if cls._active is None:
            cls._active = cls.create(config=config)
        return cls._active

    @classmethod
    def reset(cls) -> None:
        """Clear the cached active provider (tests / provider switching)."""
        cls._active = None
        logger.debug("Active provider cache cleared")

    # ------------------------------------------------------------------
    # Runtime switching
    # ------------------------------------------------------------------
    @classmethod
    def switch(
        cls,
        provider_name: str,
        config: TradingConfig | None = None,
    ) -> TradingProvider:
        """Switch the active provider at runtime.

        Disconnects the current active provider (if any), clears the cache,
        and activates the requested provider.

        Args:
            provider_name: Provider key to activate.
            config: Optional config to inject (defaults to env).

        Returns:
            The newly activated provider (also cached as active).
        """
        if cls._active is not None:
            try:
                cls._active.disconnect()
            except Exception:
                logger.exception(
                    "Error disconnecting provider %s during switch",
                    cls._active.name,
                )
            cls._active = None

        provider = cls.create(provider_name=provider_name, config=config)
        cls._active = provider
        logger.info("Switched active provider to %s", provider_name)
        return provider


__all__ = ["TradingProviderFactory"]