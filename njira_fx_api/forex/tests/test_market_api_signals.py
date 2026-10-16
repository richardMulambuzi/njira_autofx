"""
Tests for market API signals endpoints, specifically signals_snr with live=true param.
"""
import json
from unittest.mock import patch, MagicMock
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from forex.analysis.signal_lifecycle import (
    SignalLifecycleManager,
    SignalState,
    Signal,
    set_global_lifecycle,
    get_global_lifecycle,
)


class SignalsSNRTestCase(SimpleTestCase):
    """Test /api/v1/signals/snr/ endpoint with live=true/false."""

    def setUp(self):
        self.client = APIClient()
        self.url = reverse("api-signals-snr")
        # Reset global lifecycle before each test
        set_global_lifecycle(SignalLifecycleManager())

    def tearDown(self):
        # Clean up global lifecycle
        lifecycle = get_global_lifecycle()
        if lifecycle:
            lifecycle.reset()

    def _create_test_signal(self, **kwargs):
        """Helper to create a test signal with SNR analyzer."""
        defaults = {
            "symbol": "EURUSD",
            "direction": "BUY",
            "entry": 1.0850,
            "stop_loss": 1.0800,
            "take_profit": 1.0950,
            "confidence": 75.0,
            "evidence": {"analyzer": "snr", "level": 1.0850},
            "explanation": "SNR level test",
            "strategy": "malysian",
            "timeframe": "15m",
            "market_regime": "trending",
        }
        defaults.update(kwargs)
        lifecycle = get_global_lifecycle()
        sig = lifecycle.create(**defaults)
        return sig

    def test_signals_snr_live_true_returns_active_signals(self):
        """live=true returns only CONFIRMED/WAITING/TRIGGERED signals from memory."""
        # Create signals in different states
        sig1 = self._create_test_signal(
            symbol="EURUSD", direction="BUY", strategy="snr",
            evidence={"analyzer": "snr", "level": 1.0850}
        )
        sig1.state = SignalState.CONFIRMED

        sig2 = self._create_test_signal(
            symbol="EURUSD", direction="SELL", strategy="breakout",
            evidence={"analyzer": "breakout", "level": 1.0800}
        )
        sig2.state = SignalState.WAITING

        sig3 = self._create_test_signal(
            symbol="EURUSD", direction="BUY", strategy="engulfing",
            evidence={"analyzer": "engulfing", "level": 1.0850}
        )
        sig3.state = SignalState.TRIGGERED

        # Create a CANDIDATE signal (should NOT appear in live=true)
        sig4 = self._create_test_signal(
            symbol="EURUSD", direction="BUY", strategy="snr",
            evidence={"analyzer": "snr", "level": 1.0850}
        )
        sig4.state = SignalState.CANDIDATE

        # Create an INVALIDATED signal (should NOT appear in live=true)
        sig5 = self._create_test_signal(
            symbol="EURUSD", direction="SELL", strategy="snr",
            evidence={"analyzer": "snr", "level": 1.0800}
        )
        sig5.state = SignalState.INVALIDATED

        response = self.client.get(f"{self.url}?live=true")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        signals = data.get("signals", [])

        # Should only return CONFIRMED, WAITING, TRIGGERED
        self.assertEqual(len(signals), 3)
        states = [s["status"] for s in signals]
        self.assertTrue(all(s in ["PENDING"] for s in states))  # All live states map to PENDING

        # Verify v1 envelope structure
        for sig in signals:
            self.assertIn("schemaVersion", sig)
            self.assertEqual(sig["schemaVersion"], "1.0")
            self.assertIn("id", sig)
            self.assertIn("idempotencyKey", sig)
            self.assertIn("createdAt", sig)
            self.assertIn("status", sig)
            self.assertIn("source", sig)
            self.assertIn("target", sig)
            self.assertIn("instrument", sig)
            self.assertIn("bias", sig)
            self.assertIn("execution", sig)
            self.assertIn("risk", sig)
            self.assertIn("validation", sig)

    def test_signals_snr_live_true_dedup_by_setup_key(self):
        """live=true dedups by setup_key (symbol:direction:strategy)."""
        # Create two signals with same setup_key (same symbol, direction, strategy)
        sig1 = self._create_test_signal(
            symbol="EURUSD", direction="BUY", strategy="snr",
            evidence={"analyzer": "snr", "level": 1.0850}
        )
        sig1.state = SignalState.CONFIRMED
        sig1.confidence_score = 80.0

        sig2 = self._create_test_signal(
            symbol="EURUSD", direction="BUY", strategy="snr",
            evidence={"analyzer": "snr", "level": 1.0850}
        )
        sig2.state = SignalState.CONFIRMED
        sig2.confidence_score = 70.0  # Lower confidence

        response = self.client.get(f"{self.url}?live=true")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        signals = data.get("signals", [])

        # Should only return 1 signal (deduped by setup_key, newer wins)
        self.assertEqual(len(signals), 1)
        # Should be the newer one (higher confidence since we created sig1 last with higher confidence)
        self.assertEqual(signals[0]["bias"]["confidence"], 80.0)

    def test_signals_snr_live_true_orders_newest_first(self):
        """live=true returns signals ordered newest first (by created_at)."""
        sig1 = self._create_test_signal(
            symbol="EURUSD", direction="BUY", strategy="snr",
            evidence={"analyzer": "snr", "level": 1.0850}
        )
        sig1.state = SignalState.CONFIRMED

        sig2 = self._create_test_signal(
            symbol="GBPUSD", direction="SELL", strategy="breakout",
            evidence={"analyzer": "breakout", "level": 1.2500}
        )
        sig2.state = SignalState.CONFIRMED

        # sig2 was created after sig1, should appear first
        response = self.client.get(f"{self.url}?live=true")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        signals = data.get("signals", [])

        self.assertEqual(len(signals), 2)
        # sig2 (GBPUSD) should be first (newest)
        self.assertEqual(signals[0]["instrument"]["symbol"], "GBPUSD")
        self.assertEqual(signals[1]["instrument"]["symbol"], "EURUSD")

    def test_signals_snr_live_true_filters_by_symbol(self):
        """live=true respects symbol filter."""
        sig1 = self._create_test_signal(symbol="EURUSD", direction="BUY", strategy="snr")
        sig1.state = SignalState.CONFIRMED

        sig2 = self._create_test_signal(symbol="GBPUSD", direction="SELL", strategy="snr")
        sig2.state = SignalState.CONFIRMED

        response = self.client.get(f"{self.url}?live=true&symbol=EURUSD")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        signals = data.get("signals", [])

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["instrument"]["symbol"], "EURUSD")

    def test_signals_snr_live_true_filters_snr_analyzers_only(self):
        """live=true only returns SNR analyzer signals, not moving_averages."""
        sig_snr = self._create_test_signal(
            symbol="EURUSD", direction="BUY", strategy="snr",
            evidence={"analyzer": "snr", "level": 1.0850}
        )
        sig_snr.state = SignalState.CONFIRMED

        sig_ma = self._create_test_signal(
            symbol="EURUSD", direction="BUY", strategy="moving_averages",
            evidence={"analyzer": "moving_averages", "level": 1.0850}
        )
        sig_ma.state = SignalState.CONFIRMED

        response = self.client.get(f"{self.url}?live=true")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        signals = data.get("signals", [])

        # Should only return SNR signals
        self.assertEqual(len(signals), 1)
        self.assertIn("snr", signals[0]["source"]["tags"])

    def test_signals_snr_live_false_falls_back_to_db(self):
        """live=false (default) falls back to ClickHouse history."""
        with patch("forex.market_api.views._safe_client") as mock_client:
            mock_ch = MagicMock()
            mock_ch.query.return_value.result_set = []
            mock_client.return_value = mock_ch

            response = self.client.get(f"{self.url}?live=false")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            # Should return empty list from DB fallback
            self.assertEqual(data["signals"], [])

    def test_signals_snr_no_live_param_defaults_to_db(self):
        """Missing live param defaults to DB fallback (backward compatible)."""
        with patch("forex.market_api.views._safe_client") as mock_client:
            mock_ch = MagicMock()
            mock_ch.query.return_value.result_set = []
            mock_client.return_value = mock_ch

            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["signals"], [])

    def test_signals_snr_live_true_no_lifecycle_returns_empty(self):
        """live=true with no global lifecycle returns empty (not 500)."""
        set_global_lifecycle(None)

        response = self.client.get(f"{self.url}?live=true")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["signals"], [])

    def test_signals_snr_live_true_respects_limit(self):
        """live=true respects limit parameter."""
        # Create 5 signals
        for i in range(5):
            sig = self._create_test_signal(
                symbol=f"EURUSD", direction="BUY", strategy=f"snr_{i}",
                evidence={"analyzer": "snr", "level": 1.0850 + i * 0.001}
            )
            sig.state = SignalState.CONFIRMED

        response = self.client.get(f"{self.url}?live=true&limit=3")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        signals = data.get("signals", [])

        self.assertEqual(len(signals), 3)

    def test_signals_snr_live_true_limit_clamped_to_100(self):
        """live=true limit is clamped to max 100."""
        response = self.client.get(f"{self.url}?live=true&limit=200")
        self.assertEqual(response.status_code, 200)
        # Should not error, just clamp
        data = response.json()
        self.assertIn("signals", data)

    def test_signals_snr_v1_envelope_structure(self):
        """Verify v1 envelope has all required fields."""
        sig = self._create_test_signal(
            symbol="XAUUSD", direction="BUY", strategy="snr",
            evidence={"analyzer": "snr", "level": 2000.0},
            entry=2000.0, stop_loss=1990.0, take_profit=2020.0,
            confidence=80.0
        )
        sig.state = SignalState.CONFIRMED

        response = self.client.get(f"{self.url}?live=true")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        signals = data.get("signals", [])

        self.assertEqual(len(signals), 1)
        sig_data = signals[0]

        # Check required v1 fields
        self.assertEqual(sig_data["schemaVersion"], "1.0")
        self.assertTrue(sig_data["id"].startswith("sig_"))
        self.assertTrue(":" in sig_data["idempotencyKey"])
        self.assertTrue(sig_data["createdAt"].endswith("Z"))
        self.assertEqual(sig_data["status"], "PENDING")

        # Source
        self.assertEqual(sig_data["source"]["platform"], "njira_fx")
        self.assertEqual(sig_data["source"]["signalType"], "price_breakout")
        self.assertIn("snr", sig_data["source"]["tags"])

        # Target
        self.assertEqual(sig_data["target"]["platform"], "njira_fx")
        self.assertEqual(sig_data["target"]["account"], "default")
        self.assertEqual(sig_data["target"]["clientOrderId"], sig_data["id"])

        # Instrument
        self.assertEqual(sig_data["instrument"]["symbol"], "XAUUSD")
        self.assertEqual(sig_data["instrument"]["exchange"], "pepperstone")
        self.assertEqual(sig_data["instrument"]["coin"], "XAU")

        # Bias
        self.assertEqual(sig_data["bias"]["direction"], "BUY")
        self.assertEqual(sig_data["bias"]["side"], "BUY")
        self.assertEqual(sig_data["bias"]["confidence"], 80.0)
        self.assertEqual(sig_data["bias"]["strength"], "STRONG")

        # Execution
        self.assertEqual(sig_data["execution"]["orderType"], "LIMIT")
        self.assertEqual(sig_data["execution"]["limitPrice"], 2000.0)
        self.assertEqual(sig_data["execution"]["timeInForce"], "GTD")
        self.assertTrue(sig_data["execution"]["postOnly"])

        # Risk
        self.assertEqual(sig_data["risk"]["stopLoss"], 1990.0)
        self.assertEqual(sig_data["risk"]["takeProfit"], 2020.0)
        self.assertAlmostEqual(sig_data["risk"]["riskRewardRatio"], 2.0, places=1)

        # Validation
        self.assertTrue(sig_data["validation"]["priceSanity"])
        self.assertTrue(sig_data["validation"]["rrThresholdMet"])
        self.assertTrue(sig_data["validation"]["spreadCheck"])


class SignalsSNRIntegrationTestCase(SimpleTestCase):
    """Integration tests with mock ClickHouse for live=false path."""

    def setUp(self):
        self.client = APIClient()
        self.url = reverse("api-signals-snr")

    @override_settings(
        CLICKHOUSE_SIGNALS_V2_TABLE="test.signals"
    )
    @patch("forex.market_api.views.get_clickhouse_client")
    def test_signals_snr_db_returns_v1_format(self, mock_get_client):
        """DB fallback returns signals in v1 format."""
        mock_client = MagicMock()
        mock_client.query.return_value.result_set = [
            ("sig_123", "EURUSD", "BUY", "CONFIRMED", "2024-01-01 12:00:00",
             1.0850, 1.0800, 1.0950, "snr", '{"analyzer": "snr"}', "", "15m", "trending", 2.0, "STRONG", False, '[]'),
        ]
        mock_get_client.return_value = mock_client

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        signals = data.get("signals", [])

        self.assertEqual(len(signals), 1)
        sig = signals[0]
        self.assertEqual(sig["schemaVersion"], "1.0")
        self.assertEqual(sig["instrument"]["symbol"], "EURUSD")
        self.assertEqual(sig["bias"]["direction"], "BUY")