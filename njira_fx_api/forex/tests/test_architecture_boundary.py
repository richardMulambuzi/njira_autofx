"""
Architecture boundary guard: cTrader types must never cross into the shared core.

The shared core (analysis / backtest / strategy / sync) is cTrader-agnostic:
input adapters (forex.pepperstone) convert ProtoOA* messages to canonical
Tick/Bar, and the output adapter converts PolishedSignal to ProtoOANewOrderReq.
If a ProtoOA* type appears in the core, the boundary is broken.

Run: python manage.py test forex.tests.test_architecture_boundary
"""
from __future__ import annotations

from pathlib import Path

from django.test import SimpleTestCase

CORE_DIRS = ("analysis", "backtest", "strategy", "sync")
CORE_ROOT = Path(__file__).resolve().parents[1]  # forex/


class ArchitectureBoundaryTests(SimpleTestCase):
    """Assert no ProtoOA* references leak into the cTrader-agnostic core."""

    def test_no_protooa_in_shared_core(self) -> None:
        offenders: list[str] = []
        for dirname in CORE_DIRS:
            root = CORE_ROOT / dirname
            if not root.is_dir():
                continue
            for path in root.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                if "ProtoOA" in path.read_text(encoding="utf-8"):
                    offenders.append(str(path.relative_to(CORE_ROOT)))
        self.assertEqual(
            offenders,
            [],
            "cTrader types leaked into shared core — only forex.pepperstone "
            f"may reference ProtoOA*: {offenders}",
        )