"""
QA Gate 2 — forex/clickhouse/schema.py helper tests.

Covers insert_ticks_batch, get_recent_ticks, get_ohlc_bars, get_daily_pnl and
ensure_schema with a fully mocked ClickHouse client — the live server is
NEVER contacted.

* Parameterization — every query uses ``parameters=`` binding (never string
  interpolation of user input).
* Graceful failure — reads return [] / 0.0 on query errors; inserts log and
  re-raise so the caller's retry policy applies; DDL failures are swallowed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from forex.clickhouse import schema


class FakeQueryResult:
    """Mimics clickhouse-connect's QueryResult surface used by the helpers."""

    def __init__(self, column_names, result_set):
        self.column_names = column_names
        self.result_set = result_set


class FakeClient:
    """Records command/insert/query calls; query() returns a preset result."""

    def __init__(self, query_result=None, query_error=None):
        self.query_result = query_result
        self.query_error = query_error
        self.commands = []
        self.inserts = []
        self.queries = []

    def command(self, ddl):
        self.commands.append(ddl)

    def insert(self, table, rows, column_names=None):
        self.inserts.append((table, rows, column_names))

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        if self.query_error is not None:
            raise self.query_error
        return self.query_result


class InsertTicksBatchTests(SimpleTestCase):
    """insert_ticks_batch — normalization + parameterized insert."""

    def test_epoch_milliseconds_converted_to_utc_datetimes(self):
        client = FakeClient()
        rows = [
            ("EURUSD", 1.1050, 1.1052, 2.0, 5, 1750000000000),
            ("USDJPY", 155.100, 155.110, 1.0, 3, 1750000001000),
        ]
        schema.insert_ticks_batch(client, rows)

        self.assertEqual(len(client.inserts), 1)
        table, normalized, column_names = client.inserts[0]
        self.assertEqual(table, schema.TICKS_TABLE)
        self.assertEqual(
            column_names,
            ["provider", "symbol", "bid", "ask", "spread", "digits", "timestamp"],
        )
        # Epoch ms -> timezone-aware UTC datetime (DateTime64(3, 'UTC')).
        first_ts = normalized[0][6]
        self.assertIsInstance(first_ts, datetime)
        self.assertEqual(first_ts.tzinfo, timezone.utc)
        self.assertEqual(first_ts, datetime.fromtimestamp(1750000000, tz=timezone.utc))
        # Legacy 6-tuples default to the canonical provider.
        self.assertEqual(normalized[0][0], schema.DEFAULT_PROVIDER)
        # Second row keeps its own timestamp.
        self.assertEqual(
            normalized[1][6],
            datetime.fromtimestamp(1750000001, tz=timezone.utc),
        )

    def test_datetime_values_pass_through_unchanged(self):
        client = FakeClient()
        moment = datetime(2026, 8, 17, 10, 0, 0, tzinfo=timezone.utc)
        schema.insert_ticks_batch(client, [("EURUSD", 1.1, 1.2, 1.0, 5, moment)])
        normalized = client.inserts[0][1]
        self.assertIs(normalized[0][6], moment)

    def test_failure_is_logged_and_reraises(self):
        client = FakeClient()
        client.insert = mock.Mock(side_effect=RuntimeError("disk full"))
        with self.assertRaises(RuntimeError):
            schema.insert_ticks_batch(
                client, [("EURUSD", 1.1, 1.2, 1.0, 5, 1750000000000)]
            )
        # The caller (tick pipeline) applies retry/backoff — never silent drop.
        client.insert.assert_called_once()


class GetRecentTicksTests(SimpleTestCase):
    """get_recent_ticks — parameterized query, chronological order, [] on error."""

    def test_returns_chronological_dicts(self):
        result = FakeQueryResult(
            column_names=["bid", "ask", "spread", "digits", "time_str"],
            result_set=[
                (1.1052, 1.1054, 2.0, 5, "2026-08-17 10:00:02"),  # newest first
                (1.1050, 1.1052, 2.0, 5, "2026-08-17 10:00:01"),
                (1.1048, 1.1050, 2.0, 5, "2026-08-17 10:00:00"),
            ],
        )
        client = FakeClient(query_result=result)
        ticks = schema.get_recent_ticks(client, "EURUSD", 500)

        self.assertEqual(len(ticks), 3)
        # Newest-first result is reversed into chronological order.
        self.assertEqual(ticks[0]["time_str"], "2026-08-17 10:00:00")
        self.assertEqual(ticks[-1]["time_str"], "2026-08-17 10:00:02")
        self.assertEqual(ticks[0]["bid"], 1.1048)
        self.assertEqual(ticks[0]["spread"], 2.0)

        sql, parameters = client.queries[0]
        self.assertIn(schema.TICKS_TABLE, sql)
        # Values are bound via parameters= — never interpolated into SQL.
        self.assertEqual(parameters, {"sym": "EURUSD", "lim": 500})
        self.assertNotIn("EURUSD", sql.replace(schema.TICKS_TABLE, ""))

    def test_query_failure_returns_empty_list(self):
        client = FakeClient(query_error=RuntimeError("table does not exist"))
        self.assertEqual(schema.get_recent_ticks(client, "EURUSD"), [])


class GetOhlcBarsTests(SimpleTestCase):
    """get_ohlc_bars — parameterized aggregation, ISO times, [] on error."""

    def test_builds_bars_in_chronological_order(self):
        bar_time = datetime(2026, 8, 17, 10, 0, 0, tzinfo=timezone.utc)
        result = FakeQueryResult(
            column_names=["bar_time", "open", "high", "low", "close"],
            result_set=[
                (bar_time, 1.1050, 1.1060, 1.1040, 1.1055),  # newest first
                (
                    datetime(2026, 8, 17, 9, 0, 0, tzinfo=timezone.utc),
                    1.1000,
                    1.1010,
                    1.0990,
                    1.1005,
                ),
            ],
        )
        client = FakeClient(query_result=result)
        bars = schema.get_ohlc_bars(client, "EURUSD", 60, limit=100)

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["time"], "2026-08-17T09:00:00+00:00")
        self.assertEqual(bars[0]["open"], 1.1000)
        self.assertEqual(bars[0]["high"], 1.1010)
        self.assertEqual(bars[0]["low"], 1.0990)
        self.assertEqual(bars[0]["close"], 1.1005)
        self.assertEqual(bars[1]["time"], "2026-08-17T10:00:00+00:00")

        sql, parameters = client.queries[0]
        self.assertIn(schema.TICKS_TABLE, sql)
        self.assertEqual(parameters, {"tf": 60, "sym": "EURUSD", "lim": 100})

    def test_query_failure_returns_empty_list(self):
        client = FakeClient(query_error=RuntimeError("boom"))
        self.assertEqual(schema.get_ohlc_bars(client, "EURUSD", 5), [])


class GetDailyPnlTests(SimpleTestCase):
    """get_daily_pnl — float coercion, None -> 0.0, [] on error."""

    def test_returns_pnl_as_float(self):
        result = FakeQueryResult(column_names=["sum(realized_pnl)"], result_set=[(-250.5,)])
        client = FakeClient(query_result=result)
        self.assertEqual(schema.get_daily_pnl(client, 5327660), -250.5)

    def test_none_result_returns_zero(self):
        result = FakeQueryResult(column_names=["sum(realized_pnl)"], result_set=[(None,)])
        client = FakeClient(query_result=result)
        self.assertEqual(schema.get_daily_pnl(client, 5327660), 0.0)

    def test_query_failure_returns_zero(self):
        client = FakeClient(query_error=RuntimeError("no table"))
        self.assertEqual(schema.get_daily_pnl(client, 5327660), 0.0)

    def test_account_id_bound_as_parameter(self):
        result = FakeQueryResult(column_names=["sum(realized_pnl)"], result_set=[(0.0,)])
        client = FakeClient(query_result=result)
        schema.get_daily_pnl(client, 42)
        sql, parameters = client.queries[0]
        self.assertIn(schema.TRADE_HISTORY_TABLE, sql)
        self.assertEqual(parameters, {"acc": 42})


class EnsureSchemaTests(SimpleTestCase):
    """ensure_schema — idempotent DDL, failures swallowed (never crash boot)."""

    def test_creates_all_tables(self):
        client = FakeClient()
        schema.ensure_schema(client)
        # 4 tables in loop (ticks, trade_history, signals_v2, fvg) each get CREATE + provider ALTER
        # candles table handled by ensure_candle_schema (migration + TTL + provider)
        # Verify all 5 tables have CREATE commands
        create_commands = [c for c in client.commands if c.strip().upper().startswith("CREATE TABLE")]
        self.assertEqual(len(create_commands), 5)
        created_tables = set()
        for cmd in create_commands:
            for table in (schema.TICKS_TABLE, schema.TRADE_HISTORY_TABLE, schema.CANDLES_TABLE,
                          schema.SIGNALS_V2_TABLE, schema.FVG_TABLE):
                if table in cmd:
                    created_tables.add(table)
        self.assertEqual(created_tables, {schema.TICKS_TABLE, schema.TRADE_HISTORY_TABLE,
                                           schema.CANDLES_TABLE, schema.SIGNALS_V2_TABLE, schema.FVG_TABLE})
        # TTL ALTER on ticks table should be present
        ttl_commands = [c for c in client.commands if "MODIFY TTL" in c.upper()]
        self.assertTrue(any(schema.TICKS_TABLE in c for c in ttl_commands))

    def test_access_denied_is_swallowed(self):
        client = FakeClient()
        client.command = mock.Mock(
            side_effect=RuntimeError("Code: 497. DB::Exception: ACCESS_DENIED")
        )
        # Must not raise — the API keeps booting without the grant.
        schema.ensure_schema(client)
        self.assertEqual(client.command.call_count, 11)

    def test_generic_ddl_failure_is_swallowed(self):
        client = FakeClient()
        client.command = mock.Mock(side_effect=RuntimeError("network down"))
        schema.ensure_schema(client)  # no raise

    def test_is_access_denied_matches_known_forms(self):
        self.assertTrue(schema._is_access_denied(RuntimeError("ACCESS_DENIED")))
        self.assertTrue(schema._is_access_denied(RuntimeError("Code: 497")))
        self.assertFalse(schema._is_access_denied(RuntimeError("network down")))