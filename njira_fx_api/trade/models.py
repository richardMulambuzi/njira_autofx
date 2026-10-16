"""
Provider-aware persistence models for the broker domain (``trade`` app).

Design contract
----------------
* Every record carries a ``provider`` column whose values are the canonical
  ``ProviderName`` values (``pepperstone`` / ``mock`` — ``deriv`` no longer
  exists) plus a broker-native ``external_*_id`` column. The composite
  ``(provider, external_id)`` unique constraint is the ONLY global identity:
  broker IDs are only guaranteed unique *within* a provider, so we never
  assume they are globally unique — the provider column namespaces them.
* ``provider`` / ``mode`` / ``side`` / status choices are derived from the
  canonical enums in ``forex.broker.models`` (the single source of truth for
  the broker abstraction layer) so the persistence layer can never drift from
  the runtime provider registry.
* These models are the SQL persistence layer for the normalized broker
  dataclasses (``BrokerAccount``, ``BrokerPosition``, ``BrokerOrder``,
  ``BrokerTrade``) defined in ``forex.broker``. They define structure and
  relationships ONLY — no business logic lives here.
* ``ProviderHealthRecord`` persists provider health snapshots so auth /
  connection / sync state survives process restarts.
"""
from __future__ import annotations

from django.db import models

from forex.broker.models import (
    OrderSide,
    OrderStatus,
    PositionStatus,
    ProviderName,
    TradingMode,
)

# Choices derived from the canonical broker enums (single source of truth).
PROVIDER_CHOICES = [(member.value, member.value) for member in ProviderName]
MODE_CHOICES = [(member.value, member.value) for member in TradingMode]
SIDE_CHOICES = [(member.value, member.value) for member in OrderSide]
POSITION_STATUS_CHOICES = [
    (member.value, member.value) for member in PositionStatus
]
ORDER_STATUS_CHOICES = [(member.value, member.value) for member in OrderStatus]


class TradingAccount(models.Model):
    """A provider-side trading account (demo / paper / live)."""

    provider = models.CharField(max_length=32, choices=PROVIDER_CHOICES)
    external_account_id = models.CharField(max_length=128)
    mode = models.CharField(
        max_length=16, choices=MODE_CHOICES, default=TradingMode.DEMO.value
    )
    status = models.CharField(max_length=32, default="active")
    currency = models.CharField(max_length=8, default="USD")
    balance = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    equity = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    margin = models.DecimalField(max_digits=20, decimal_places=8, default=0)
    free_margin = models.DecimalField(
        max_digits=20, decimal_places=8, default=0
    )
    name = models.CharField(max_length=255, blank=True)
    leverage = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("provider", "external_account_id")
        indexes = [
            models.Index(
                fields=["provider", "status"],
                name="trade_acc_provider_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.external_account_id} ({self.mode})"


class BrokerPosition(models.Model):
    """An open or closed position at a broker, keyed by (provider, external id)."""

    provider = models.CharField(max_length=32, choices=PROVIDER_CHOICES)
    external_position_id = models.CharField(max_length=128)
    account = models.ForeignKey(
        TradingAccount,
        on_delete=models.CASCADE,
        related_name="positions",
        null=True,
        blank=True,
    )
    symbol = models.CharField(max_length=32)
    side = models.CharField(max_length=8, choices=SIDE_CHOICES)
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    entry_price = models.DecimalField(max_digits=20, decimal_places=8)
    current_price = models.DecimalField(
        max_digits=20, decimal_places=8, default=0
    )
    status = models.CharField(
        max_length=16,
        choices=POSITION_STATUS_CHOICES,
        default=PositionStatus.OPEN.value,
    )
    stop_loss = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    take_profit = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    realized_pnl = models.DecimalField(
        max_digits=20, decimal_places=8, default=0
    )
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("provider", "external_position_id")
        indexes = [
            models.Index(
                fields=["provider", "symbol", "status"],
                name="trade_pos_prov_sym_status_idx",
            ),
            models.Index(
                fields=["provider", "status"],
                name="trade_pos_provider_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.provider} pos {self.external_position_id} "
            f"{self.symbol} {self.side}"
        )


class BrokerOrder(models.Model):
    """An order submitted to a broker, keyed by (provider, external id)."""

    provider = models.CharField(max_length=32, choices=PROVIDER_CHOICES)
    external_order_id = models.CharField(max_length=128)
    account = models.ForeignKey(
        TradingAccount,
        on_delete=models.CASCADE,
        related_name="orders",
        null=True,
        blank=True,
    )
    symbol = models.CharField(max_length=32)
    side = models.CharField(max_length=8, choices=SIDE_CHOICES)
    order_type = models.CharField(max_length=16, default="market")
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    filled_quantity = models.DecimalField(
        max_digits=20, decimal_places=8, default=0
    )
    price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    status = models.CharField(
        max_length=16,
        choices=ORDER_STATUS_CHOICES,
        default=OrderStatus.PENDING.value,
    )
    stop_loss = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    take_profit = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("provider", "external_order_id")
        indexes = [
            models.Index(
                fields=["provider", "status"],
                name="trade_order_prov_status_idx",
            ),
            models.Index(
                fields=["provider", "symbol"],
                name="trade_order_prov_symbol_idx",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.provider} order {self.external_order_id} "
            f"{self.symbol} {self.side}"
        )


class BrokerTrade(models.Model):
    """A completed/closed trade at a broker, keyed by (provider, external id)."""

    provider = models.CharField(max_length=32, choices=PROVIDER_CHOICES)
    external_trade_id = models.CharField(max_length=128)
    account = models.ForeignKey(
        TradingAccount,
        on_delete=models.CASCADE,
        related_name="trades",
        null=True,
        blank=True,
    )
    symbol = models.CharField(max_length=32)
    side = models.CharField(max_length=8, choices=SIDE_CHOICES)
    quantity = models.DecimalField(max_digits=20, decimal_places=8)
    entry_price = models.DecimalField(max_digits=20, decimal_places=8)
    exit_price = models.DecimalField(
        max_digits=20, decimal_places=8, null=True, blank=True
    )
    realized_pnl = models.DecimalField(
        max_digits=20, decimal_places=8, default=0
    )
    status = models.CharField(max_length=16, default="open")
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("provider", "external_trade_id")
        indexes = [
            models.Index(
                fields=["provider", "symbol"],
                name="trade_trade_prov_symbol_idx",
            ),
            models.Index(
                fields=["provider", "status"],
                name="trade_trade_prov_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider} trade {self.external_trade_id} {self.symbol}"


class ProviderHealthRecord(models.Model):
    """A persisted provider health snapshot (auth / connection / sync state)."""

    provider = models.CharField(max_length=32)
    environment = models.CharField(max_length=16, default="demo")
    authentication_status = models.CharField(max_length=64, default="unknown")
    connection_status = models.CharField(max_length=64, default="disconnected")
    api_available = models.BooleanField(default=False)
    account_sync_status = models.CharField(max_length=64, default="never_synced")
    last_successful_request = models.DateTimeField(null=True, blank=True)
    last_market_data_update = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["provider", "recorded_at"],
                name="trade_health_prov_recorded_idx",
            ),
        ]
        ordering = ["-recorded_at"]

    def __str__(self) -> str:
        return f"{self.provider} health @ {self.recorded_at}"