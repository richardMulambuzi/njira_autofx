"""Contract test for the v1 signal envelope — ``Signal.to_dict_v1()``.

Pins the wire shape so a change to the serializer breaks loudly here instead
of downstream in ClickHouse / Pusher / the UI. The legacy flat ``to_dict()``
is pinned too: v1 is documented as additive, so v0 must not drift.

Covers:
- the 12 required top-level keys, exactly
- nested block keys (source/target/instrument/bias/execution/risk/validation)
- strength enum stays inside the five tiers classify_strength() defines
- idempotencyKey format + stability
- status mapping for every SignalState (terminal → closed, open → PENDING)

Run: python manage.py test forex.tests.test_signal_v1
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from django.test import SimpleTestCase

from forex.analysis.signal_lifecycle import (
    Signal,
    SignalState,
    _STATUS_V1_BY_STATE,
    _TERMINAL_STATES,
)

TOP_LEVEL_KEYS = {
    "schemaVersion", "id", "idempotencyKey", "createdAt", "status",
    "source", "target", "instrument", "bias", "execution", "risk", "validation",
}

NESTED_KEYS = {
    "source": {"platform", "signalType", "tags"},
    "target": {"platform", "account", "clientOrderId"},
    "instrument": {"coin", "symbol", "exchange", "pricePrecision", "quantityPrecision"},
    "bias": {"direction", "side", "confidence", "strength"},
    "execution": {
        "orderType", "limitPrice", "estimatedEntryTime",
        "timeInForce", "expiresAt", "postOnly", "reduceOnly",
    },
    "risk": {
        "stopLoss", "takeProfit", "riskRewardRatio",
        "leverage", "positionSize", "positionSizeType", "maxSlippageBps",
    },
    "validation": {"priceSanity", "rrThresholdMet", "spreadCheck"},
}

STRENGTH_ENUM = {"RECOMMENDED", "STRONG", "NORMAL", "WEAK", "RISKY"}

STATUS_ENUM = {"PENDING", "FILLED", "CANCELLED"}

CREATED_AT = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)


def _signal(**over) -> Signal:
    """A realistic CONFIRMED BUY. Override any field per-test."""
    base = dict(
        signal_id="sig_abc123def456",
        symbol="EURUSD",
        direction="BUY",
        state=SignalState.CONFIRMED,
        created_at=CREATED_AT,
        entry=1.1000,
        stop_loss=1.0970,
        take_profit=1.1060,
        entry_zone_low=1.0980,
        entry_zone_high=1.1020,
        confidence_score=72.0,
        strategy="malysian",
        timeframe="1H",
        market_regime="trending",
        risk_reward_ratio=2.0,
    )
    base.update(over)
    return Signal(**base)


# ── 1. Top-level shape ──────────────────────────────────────────────────────
class TopLevelShapeTests(SimpleTestCase):
    def test_exactly_the_required_keys(self):
        self.assertEqual(set(_signal().to_dict_v1()), TOP_LEVEL_KEYS)

    def test_schema_version_is_pinned(self):
        # Literal, not SCHEMA_VERSION — bumping the constant is a breaking
        # wire change and must break this test, not follow it silently.
        self.assertEqual(_signal().to_dict_v1()["schemaVersion"], "1.0")

    def test_id_and_created_at_carry_through(self):
        env = _signal().to_dict_v1()
        self.assertEqual(env["id"], "sig_abc123def456")
        self.assertEqual(env["createdAt"], "2026-01-01T13:00:00Z")
        self.assertTrue(env["createdAt"].endswith("Z"), "createdAt must be Z-suffixed on the wire")

    def test_naive_created_at_is_treated_as_utc(self):
        naive = _signal(created_at=datetime(2026, 1, 1, 13, 0)).to_dict_v1()
        self.assertEqual(naive["createdAt"], "2026-01-01T13:00:00Z")


# ── 2. Nested blocks ────────────────────────────────────────────────────────
class NestedBlockTests(SimpleTestCase):
    def test_every_block_has_its_contracted_keys(self):
        env = _signal().to_dict_v1()
        for block, keys in NESTED_KEYS.items():
            self.assertIsInstance(env[block], dict, f"{block} must be an object")
            self.assertEqual(set(env[block]), keys, f"{block} block drifted")

    def test_instrument_routing_fx_vs_crypto(self):
        fx = _signal(symbol="EURUSD").to_dict_v1()["instrument"]
        self.assertEqual((fx["coin"], fx["exchange"], fx["pricePrecision"]), ("EUR", "pepperstone", 5))

        gold = _signal(symbol="XAUUSD").to_dict_v1()["instrument"]
        self.assertEqual((gold["coin"], gold["exchange"], gold["pricePrecision"]), ("XAU", "pepperstone", 2))

        crypto = _signal(symbol="BTCUSDT").to_dict_v1()["instrument"]
        self.assertEqual((crypto["coin"], crypto["exchange"]), ("BTC", "binance"))
        self.assertEqual(crypto["quantityPrecision"], 6)

    def test_unknown_symbol_falls_back_without_raising(self):
        inst = _signal(symbol="AUDJPY").to_dict_v1()["instrument"]
        self.assertEqual(inst["symbol"], "AUDJPY")
        self.assertEqual(inst["pricePrecision"], 5)  # FX default

    def test_risk_block_mirrors_signal_prices(self):
        risk = _signal().to_dict_v1()["risk"]
        self.assertEqual(risk["stopLoss"], 1.0970)
        self.assertEqual(risk["takeProfit"], 1.1060)
        self.assertEqual(risk["riskRewardRatio"], 2.0)

    def test_rr_computed_from_prices_when_stored_ratio_missing(self):
        # Stored ratio is 0.0 → structural fallback |tp-entry| / |entry-sl|.
        risk = _signal(risk_reward_ratio=0.0).to_dict_v1()["risk"]
        self.assertAlmostEqual(risk["riskRewardRatio"], 0.0060 / 0.0030, places=6)

    def test_execution_expiry_is_created_plus_one_hour(self):
        ex = _signal().to_dict_v1()["execution"]
        self.assertEqual(ex["expiresAt"], "2026-01-01T14:00:00Z")
        self.assertEqual(ex["limitPrice"], 1.1000)
        self.assertTrue(ex["postOnly"])
        self.assertFalse(ex["reduceOnly"])

    def test_validation_price_sanity_is_directional(self):
        self.assertTrue(_signal(direction="BUY").to_dict_v1()["validation"]["priceSanity"])
        # Same prices, SELL direction → SL above / TP below is what a SELL needs.
        self.assertFalse(_signal(direction="SELL").to_dict_v1()["validation"]["priceSanity"])

        sell = _signal(direction="SELL", entry=1.1000, stop_loss=1.1030, take_profit=1.0940)
        self.assertTrue(sell.to_dict_v1()["validation"]["priceSanity"])

    def test_validation_rejects_inverted_prices(self):
        broken = _signal(stop_loss=1.1100).to_dict_v1()["validation"]
        self.assertFalse(broken["priceSanity"])

    def test_validation_rr_threshold_actually_gates(self):
        # RR = |1.1001-1.1000| / |1.1000-1.0990| = 0.1 → below V1_MIN_RR_RATIO.
        thin = _signal(entry=1.1000, stop_loss=1.0990, take_profit=1.1001,
                       risk_reward_ratio=0.0).to_dict_v1()["validation"]
        self.assertFalse(thin["rrThresholdMet"])

        # Same signal carrying a stored ratio above the bar → passes.
        fat = _signal(entry=1.1000, stop_loss=1.0990, take_profit=1.1001,
                      risk_reward_ratio=2.0).to_dict_v1()["validation"]
        self.assertTrue(fat["rrThresholdMet"])

    def test_validation_block_is_all_booleans(self):
        for direction in ("BUY", "SELL"):
            with self.subTest(direction=direction):
                val = _signal(direction=direction).to_dict_v1()["validation"]
                for k, v in val.items():
                    self.assertIsInstance(v, bool, f"{k} must be a JSON boolean")

    def test_source_tags_are_deduped_and_non_empty(self):
        tags = _signal(source_strategies=["malysian", "fvg"]).to_dict_v1()["source"]["tags"]
        self.assertEqual(tags.count("malysian"), 1)
        self.assertNotIn("", tags)
        self.assertEqual(_signal().to_dict_v1()["source"]["platform"], "njira_fx")


# ── 3. strength enum ────────────────────────────────────────────────────────
class StrengthEnumTests(SimpleTestCase):
    def test_bias_strength_is_always_a_known_tier(self):
        # Sweep the tier boundaries; a confidence outside the enum must fail here.
        for confidence in (0.0, 34.9, 35.0, 49.9, 50.0, 64.9, 65.0, 79.9, 80.0, 100.0):
            with self.subTest(confidence=confidence):
                bias = _signal(confidence_score=confidence).to_dict_v1()["bias"]
                self.assertIn(bias["strength"], STRENGTH_ENUM)

    def test_bias_strength_tracks_confidence(self):
        self.assertEqual(_signal(confidence_score=72.0).to_dict_v1()["bias"]["strength"], "STRONG")
        self.assertEqual(_signal(confidence_score=30.0).to_dict_v1()["bias"]["strength"], "RISKY")

    def test_explicit_strength_overrides_classification(self):
        bias = _signal(strength="RECOMMENDED").to_dict_v1()["bias"]
        self.assertEqual(bias["strength"], "RECOMMENDED")


# ── 4. idempotencyKey ({setup_key}:{timeframe}:{ts}, id stays signal_id) ────
class IdempotencyKeyTests(SimpleTestCase):
    KEY_RE = re.compile(r"^[^:]+:[^:]+:[^:]+:[^:]+:\d+$")

    def test_format(self):
        key = _signal().to_dict_v1()["idempotencyKey"]
        self.assertRegex(key, self.KEY_RE)
        self.assertEqual(key, "EURUSD:BUY:malysian:1H:1767272400")
        self.assertNotIn("sig_abc123def456", key)

    def test_stable_across_repeated_serialization(self):
        sig = _signal()
        self.assertEqual(sig.to_dict_v1()["idempotencyKey"], sig.to_dict_v1()["idempotencyKey"])

    def test_retry_same_setup_same_key_despite_new_signal_id(self):
        a = _signal(signal_id="sig_aaa111").to_dict_v1()["idempotencyKey"]
        b = _signal(signal_id="sig_bbb222").to_dict_v1()["idempotencyKey"]
        self.assertEqual(a, b)

    def test_distinct_per_setup(self):
        a = _signal(direction="BUY").to_dict_v1()["idempotencyKey"]
        b = _signal(direction="SELL").to_dict_v1()["idempotencyKey"]
        self.assertNotEqual(a, b)

    def test_distinct_per_timeframe(self):
        a = _signal(timeframe="M15").to_dict_v1()["idempotencyKey"]
        b = _signal(timeframe="H1").to_dict_v1()["idempotencyKey"]
        self.assertNotEqual(a, b)

    def test_empty_timeframe_defaults_to_multi(self):
        key = _signal(timeframe="").to_dict_v1()["idempotencyKey"]
        self.assertRegex(key, self.KEY_RE)
        self.assertIn(":multi:", key)


# ── 5. status mapping ───────────────────────────────────────────────────────
class StatusMappingTests(SimpleTestCase):
    EXPECTED = {
        SignalState.CANDIDATE: "PENDING",
        SignalState.WATCHING: "PENDING",
        SignalState.WAITING: "PENDING",
        SignalState.TRIGGERED: "PENDING",
        SignalState.CONFIRMED: "PENDING",
        SignalState.COMPLETED: "FILLED",
        SignalState.INVALIDATED: "CANCELLED",
        SignalState.EXPIRED: "CANCELLED",
    }

    def test_every_state_maps_as_specified(self):
        for state, expected in self.EXPECTED.items():
            with self.subTest(state=state.value):
                self.assertEqual(_signal(state=state).to_dict_v1()["status"], expected)

    def test_mapping_covers_every_state_no_default_leak(self):
        self.assertEqual(set(_STATUS_V1_BY_STATE), set(SignalState))
        for state in SignalState:
            self.assertIn(state, _STATUS_V1_BY_STATE, f"{state.value} unmapped — would silently fall back to PENDING")

    def test_open_states_stay_pending_terminal_states_are_closed(self):
        # Ties the wire status to the module's own terminal-state definition.
        for state in SignalState:
            with self.subTest(state=state.value):
                status = _signal(state=state).to_dict_v1()["status"]
                if state in _TERMINAL_STATES:
                    self.assertIn(status, {"FILLED", "CANCELLED"})
                    self.assertNotEqual(status, "PENDING")
                else:
                    self.assertEqual(status, "PENDING")

    def test_status_always_in_enum(self):
        for state in SignalState:
            with self.subTest(state=state.value):
                self.assertIn(_signal(state=state).to_dict_v1()["status"], STATUS_ENUM)


# ── 6. v0 payload must not drift (v1 is additive) ───────────────────────────
class LegacyToDictTests(SimpleTestCase):
    V0_KEYS = {
        "signal_id", "symbol", "direction", "state", "created_at", "entry",
        "stop_loss", "take_profit", "entry_zone_low", "entry_zone_high",
        "confidence_score", "confidence_evidence", "explanation", "strategy",
        "timeframe", "market_regime", "risk_reward_ratio", "strength",
        "combined", "source_strategies",
    }

    def test_v0_shape_unchanged(self):
        self.assertEqual(set(_signal().to_dict()), self.V0_KEYS)

    def test_v1_does_not_leak_into_v0(self):
        v0 = _signal().to_dict()
        self.assertNotIn("schemaVersion", v0)
        self.assertNotIn("idempotencyKey", v0)
        self.assertNotIn("validation", v0)

    def test_to_dict_v1_is_pure(self):
        # No clock/network read: the envelope is a function of the Signal only.
        sig = _signal()
        self.assertEqual(sig.to_dict_v1(), sig.to_dict_v1())
        self.assertEqual(set(sig.to_dict_v1()), TOP_LEVEL_KEYS)