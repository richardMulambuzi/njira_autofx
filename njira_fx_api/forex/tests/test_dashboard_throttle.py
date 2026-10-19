import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "forex.settings")
django.setup()

from django.contrib.auth.models import AnonymousUser  # noqa: E402
from django.core.cache import cache  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from forex.dashboard_view import SESSION_KEY  # noqa: E402
from forex.throttling import DashboardReadAnonThrottle  # noqa: E402

SESSIONED = {SESSION_KEY: True}


def _request(method, session):
    factory = RequestFactory()
    request = factory.get("/api/health/") if method == "GET" else factory.post("/api/health/")
    request.session = session
    request.user = AnonymousUser()
    request.META["REMOTE_ADDR"] = "203.0.113.9"
    return request


def test_dashboard_session_is_never_throttled_on_reads():
    cache.clear()
    throttle = DashboardReadAnonThrottle()
    allowed = [throttle.allow_request(_request("GET", SESSIONED), None) for _ in range(40)]
    assert allowed.count(False) == 0


def test_anonymous_reads_are_still_throttled():
    cache.clear()
    throttle = DashboardReadAnonThrottle()
    allowed = [throttle.allow_request(_request("GET", {}), None) for _ in range(40)]
    assert allowed.count(False) > 0


def test_dashboard_session_does_not_exempt_writes():
    cache.clear()
    throttle = DashboardReadAnonThrottle()
    allowed = [throttle.allow_request(_request("POST", SESSIONED), None) for _ in range(40)]
    assert allowed.count(False) > 0
