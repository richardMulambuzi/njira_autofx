# Malaysian SNR Measurement Report
**Period:** Oct 10–15, 2026 (6 days)
**Symbols:** XAUUSD, EURUSD, GBPUSD, USDJPY
**Generated:** 2026-10-15 08:03 UTC

---

## Overall Summary

| Metric | Value |
|--------|-------|
| Signals Generated | 0 |
| Signals with Exit | 0 |
| Win Rate | **0%** |
| Net R (total) | 0 |
| Expectancy (per trade) | 0 R |
| Profit Factor | None |

> **Target:** ≥ 70% win rate. Current baseline before filtering.

---

## By Symbol

| Group | N | WR% | Net R | Expectancy | Profit Factor |
|-------|---|-----|-------|------------|---------------|

---

## By Analyzer

| Group | N | WR% | Net R | Expectancy | Profit Factor |
|-------|---|-----|-------|------------|---------------|

---

## By Session

| Group | N | WR% | Net R | Expectancy | Profit Factor |
|-------|---|-----|-------|------------|---------------|

---

## By Direction

| Group | N | WR% | Net R | Expectancy | Profit Factor |
|-------|---|-----|-------|------------|---------------|

---

## Confluence Filter Simulation

The table below shows what win rate and trade count would be if signals
with a confluence score below the threshold were dropped (Phase 2 of the plan).

| Min Score | N | WR% | Net R | Expectancy | Signals Kept |
|-----------|---|-----|-------|------------|--------------|
| ≥4 | 0 | 0% | 0 | 0 | 0% |
| ≥5 | 0 | 0% | 0 | 0 | 0% |
| ≥6 | 0 | 0% | 0 | 0 | 0% |
| ≥7 | 0 | 0% | 0 | 0 | 0% |
| ≥8 | 0 | 0% | 0 | 0 | 0% |

> [!TIP]
> The sweet spot is where WR% hits ≥ 70% while keeping ≥ 40% of signals.
> Use this to choose your `CONFLUENCE_REQUIRED` threshold.

---

## Signals Logged (Index)

_No signals generated_

---

## Next Steps

1. Check the **By Analyzer** table — drop analyzers with WR < 50%
2. Check the **Confluence Filter** table — pick your min score threshold
3. Implement Phase 1 (signal logger) and Phase 2 (confluence gate)
4. Re-run this script after implementation to confirm improvement
