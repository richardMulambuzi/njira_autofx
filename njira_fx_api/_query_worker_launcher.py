#!/usr/bin/env python
"""
Thin launcher for forex.pepperstone.query_worker that pre-stubs
heavy/unavailable dependencies (clickhouse_connect, django, twisted,
google.protobuf, ctrader_open_api) before the import chain runs.

Usage (exact same args as the real worker):
    python _query_worker_launcher.py ticks_range '{"symbol":"XAUUSD",...}'
"""
from __future__ import annotations
import sys, os, types, json, pathlib

# ── Load .env ────────────────────────────────────────────────────────────────
_env = pathlib.Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

# ── Pre-stub unavailable modules (must happen before ANY forex import) ────────
class _Stub(types.ModuleType):
    def __getattr__(self, k):
        s = _Stub(f"{self.__name__}.{k}")
        setattr(self, k, s)
        return s
    def __call__(self, *a, **kw): return _Stub("_r")
    def __iter__(self): return iter([])
    def __bool__(self): return True
    def __int__(self): return 0
    def __float__(self): return 0.0
    def __str__(self): return ""

_STUBS = [
    "clickhouse_connect",
    "django", "django.db", "django.db.models", "django.db.models.signals",
    "django.conf", "django.utils", "django.utils.timezone",
    "django.apps", "django.core",
    "pusher",
    "google.protobuf", "google.protobuf.json_format",
]
for _m in _STUBS:
    sys.modules.setdefault(_m, _Stub(_m))

# ── Patch forex/__init__.py clickhouse import away ───────────────────────────
# We inject a fake "forex" package that does nothing, then let real sub-modules
# load normally (they live on disk and will be importable once the stubs are set).
import importlib, importlib.util, importlib.machinery, importlib.abc

_forex_root = pathlib.Path(__file__).parent / "forex"

class _ForexLoader(importlib.abc.Loader):
    """Loader for forex/__init__.py that skips the clickhouse import."""
    def create_module(self, spec): return None
    def exec_module(self, module):
        # Replicate only what forex/__init__.py does sans clickhouse
        from pathlib import Path
        try:
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).parent / ".env")
        except ImportError:
            pass
        # Done — skip get_clickhouse_client import entirely

_forex_spec = importlib.util.spec_from_file_location(
    "forex", _forex_root / "__init__.py",
    loader=_ForexLoader(),
    submodule_search_locations=[str(_forex_root)],
)
_forex_mod = importlib.util.module_from_spec(_forex_spec)
_forex_mod.__path__ = [str(_forex_root)]
_forex_mod.__package__ = "forex"
sys.modules["forex"] = _forex_mod
_forex_spec.loader.exec_module(_forex_mod)

# Now ctrader_open_api IS installed in the venv — remove our stub so the real
# package loads for the worker that needs it.
sys.modules.pop("ctrader_open_api", None)

# ── Run the real worker main() ───────────────────────────────────────────────
from forex.pepperstone.query_worker import main  # noqa: E402
sys.exit(main(sys.argv[1:]))
