import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "forex.settings")
django.setup()

from django.test import Client  # noqa: E402


def test_disabled_clickhouse_is_reported_as_disabled_not_unhealthy():
    body = Client().get("/api/health/", HTTP_HOST="localhost").json()

    assert body["clickhouse"]["enabled"] is False, "test assumes the memory-only stub"
    assert body["clickhouse"]["connected"] is False
    assert body["status"] == "healthy"
