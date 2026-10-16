"""
Authorization permissions for the Pepperstone pipeline endpoints (auth-engineer).

Security Gate 1 findings #6/#7 remediation:

    POST /api/trade/   — staff OR service account only (HIGH #6)
    POST /api/ticks/   — service account (X-API-Key) OR staff only (HIGH #7)

The service account authenticates with the ``X-API-Key`` header, which must
match the ``TICKS_INGEST_API_KEY`` environment variable (generated with
``secrets.token_urlsafe`` — see ``.env``). Comparison is constant-time
(``secrets.compare_digest``) so the key cannot be recovered via timing.

Regular authenticated users are explicitly denied (403) — they must never be
able to place real orders on the firm's Pepperstone account or inject ticks
into the spread risk gate / MTF strategy.
"""
from __future__ import annotations

import os
import secrets

from rest_framework.permissions import BasePermission

#: Header carrying the service-account credential (Django normalizes to HTTP_).
API_KEY_HEADER = "HTTP_X_API_KEY"


def _service_api_key_matches(request) -> bool:
    """True when the request carries the configured service-account API key."""
    expected = os.environ.get("TICKS_INGEST_API_KEY", "")
    if not expected:
        return False
    provided = request.META.get(API_KEY_HEADER, "")
    if not provided:
        return False
    return secrets.compare_digest(provided, expected)


class IsStaffOrServiceAccount(BasePermission):
    """
    Allow only staff users or the configured service account.

    - ``request.user.is_staff`` (authenticated staff member) → allowed.
    - ``X-API-Key`` header matching ``TICKS_INGEST_API_KEY`` → allowed
      (service account; no JWT required).
    - Everything else (regular authenticated users, anonymous) → 403.
    """

    message = "Staff or service-account credentials are required."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False) and user.is_staff:
            return True
        return _service_api_key_matches(request)