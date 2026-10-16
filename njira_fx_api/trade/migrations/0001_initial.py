"""
Initial provider-aware schema for the ``trade`` app.

Hand-written (``makemigrations`` was intentionally NOT run). Field names,
types, defaults, unique constraints and index names mirror
``trade.models`` exactly.

Design: every entity carries a ``provider`` column that namespaces
broker-native external IDs; the composite ``(provider, external_id)``
unique constraint enforces global uniqueness without assuming broker IDs
are globally unique.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="TradingAccount",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "provider",
                    models.CharField(
                        choices=[
                            ("pepperstone", "pepperstone"),
                            ("deriv", "deriv"),
                            ("mock", "mock"),
                        ],
                        max_length=32,
                    ),
                ),
                ("external_account_id", models.CharField(max_length=128)),
                (
                    "mode",
                    models.CharField(
                        choices=[
                            ("demo", "demo"),
                            ("paper", "paper"),
                            ("live", "live"),
                        ],
                        default="demo",
                        max_length=16,
                    ),
                ),
                ("status", models.CharField(default="active", max_length=32)),
                ("currency", models.CharField(default="USD", max_length=8)),
                (
                    "balance",
                    models.DecimalField(
                        decimal_places=8, default=0, max_digits=20
                    ),
                ),
                (
                    "equity",
                    models.DecimalField(
                        decimal_places=8, default=0, max_digits=20
                    ),
                ),
                (
                    "margin",
                    models.DecimalField(
                        decimal_places=8, default=0, max_digits=20
                    ),
                ),
                (
                    "free_margin",
                    models.DecimalField(
                        decimal_places=8, default=0, max_digits=20
                    ),
                ),
                ("name", models.CharField(blank=True, max_length=255)),
                (
                    "leverage",
                    models.DecimalField(
                        decimal_places=2, default=0, max_digits=10
                    ),
                ),
                (
                    "last_sync_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "unique_together": {("provider", "external_account_id")},
                "indexes": [
                    models.Index(
                        fields=["provider", "status"],
                        name="trade_acc_provider_status_idx",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="BrokerPosition",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "provider",
                    models.CharField(
                        choices=[
                            ("pepperstone", "pepperstone"),
                            ("deriv", "deriv"),
                            ("mock", "mock"),
                        ],
                        max_length=32,
                    ),
                ),
                ("external_position_id", models.CharField(max_length=128)),
                (
                    "account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="positions",
                        to="trade.tradingaccount",
                    ),
                ),
                ("symbol", models.CharField(max_length=32)),
                (
                    "side",
                    models.CharField(
                        choices=[("buy", "buy"), ("sell", "sell")],
                        max_length=8,
                    ),
                ),
                (
                    "quantity",
                    models.DecimalField(decimal_places=8, max_digits=20),
                ),
                (
                    "entry_price",
                    models.DecimalField(decimal_places=8, max_digits=20),
                ),
                (
                    "current_price",
                    models.DecimalField(
                        decimal_places=8, default=0, max_digits=20
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("open", "open"), ("closed", "closed")],
                        default="open",
                        max_length=16,
                    ),
                ),
                (
                    "stop_loss",
                    models.DecimalField(
                        blank=True, decimal_places=8, max_digits=20, null=True
                    ),
                ),
                (
                    "take_profit",
                    models.DecimalField(
                        blank=True, decimal_places=8, max_digits=20, null=True
                    ),
                ),
                (
                    "realized_pnl",
                    models.DecimalField(
                        decimal_places=8, default=0, max_digits=20
                    ),
                ),
                ("opened_at", models.DateTimeField()),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "unique_together": {("provider", "external_position_id")},
                "indexes": [
                    models.Index(
                        fields=["provider", "symbol", "status"],
                        name="trade_pos_prov_sym_status_idx",
                    ),
                    models.Index(
                        fields=["provider", "status"],
                        name="trade_pos_provider_status_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="BrokerOrder",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "provider",
                    models.CharField(
                        choices=[
                            ("pepperstone", "pepperstone"),
                            ("deriv", "deriv"),
                            ("mock", "mock"),
                        ],
                        max_length=32,
                    ),
                ),
                ("external_order_id", models.CharField(max_length=128)),
                (
                    "account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="orders",
                        to="trade.tradingaccount",
                    ),
                ),
                ("symbol", models.CharField(max_length=32)),
                (
                    "side",
                    models.CharField(
                        choices=[("buy", "buy"), ("sell", "sell")],
                        max_length=8,
                    ),
                ),
                ("order_type", models.CharField(default="market", max_length=16)),
                (
                    "quantity",
                    models.DecimalField(decimal_places=8, max_digits=20),
                ),
                (
                    "filled_quantity",
                    models.DecimalField(
                        decimal_places=8, default=0, max_digits=20
                    ),
                ),
                (
                    "price",
                    models.DecimalField(
                        blank=True, decimal_places=8, max_digits=20, null=True
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "pending"),
                            ("accepted", "accepted"),
                            ("filled", "filled"),
                            ("rejected", "rejected"),
                            ("cancelled", "cancelled"),
                            ("expired", "expired"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                (
                    "stop_loss",
                    models.DecimalField(
                        blank=True, decimal_places=8, max_digits=20, null=True
                    ),
                ),
                (
                    "take_profit",
                    models.DecimalField(
                        blank=True, decimal_places=8, max_digits=20, null=True
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "unique_together": {("provider", "external_order_id")},
                "indexes": [
                    models.Index(
                        fields=["provider", "status"],
                        name="trade_order_prov_status_idx",
                    ),
                    models.Index(
                        fields=["provider", "symbol"],
                        name="trade_order_prov_symbol_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="BrokerTrade",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "provider",
                    models.CharField(
                        choices=[
                            ("pepperstone", "pepperstone"),
                            ("deriv", "deriv"),
                            ("mock", "mock"),
                        ],
                        max_length=32,
                    ),
                ),
                ("external_trade_id", models.CharField(max_length=128)),
                (
                    "account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="trades",
                        to="trade.tradingaccount",
                    ),
                ),
                ("symbol", models.CharField(max_length=32)),
                (
                    "side",
                    models.CharField(
                        choices=[("buy", "buy"), ("sell", "sell")],
                        max_length=8,
                    ),
                ),
                (
                    "quantity",
                    models.DecimalField(decimal_places=8, max_digits=20),
                ),
                (
                    "entry_price",
                    models.DecimalField(decimal_places=8, max_digits=20),
                ),
                (
                    "exit_price",
                    models.DecimalField(
                        blank=True, decimal_places=8, max_digits=20, null=True
                    ),
                ),
                (
                    "realized_pnl",
                    models.DecimalField(
                        decimal_places=8, default=0, max_digits=20
                    ),
                ),
                ("status", models.CharField(default="open", max_length=16)),
                ("opened_at", models.DateTimeField()),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "unique_together": {("provider", "external_trade_id")},
                "indexes": [
                    models.Index(
                        fields=["provider", "symbol"],
                        name="trade_trade_prov_symbol_idx",
                    ),
                    models.Index(
                        fields=["provider", "status"],
                        name="trade_trade_prov_status_idx",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ProviderHealthRecord",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("provider", models.CharField(max_length=32)),
                ("environment", models.CharField(default="demo", max_length=16)),
                (
                    "authentication_status",
                    models.CharField(default="unknown", max_length=64),
                ),
                (
                    "connection_status",
                    models.CharField(default="disconnected", max_length=64),
                ),
                ("api_available", models.BooleanField(default=False)),
                (
                    "account_sync_status",
                    models.CharField(default="never_synced", max_length=64),
                ),
                (
                    "last_successful_request",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "last_market_data_update",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("last_error", models.TextField(blank=True)),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["provider", "recorded_at"],
                        name="trade_health_prov_recorded_idx",
                    )
                ],
                "ordering": ["-recorded_at"],
            },
        ),
    ]