"""URL routes for the tick synchronization engine API endpoints (contracts §7).

All routes live under ``/api/sync/`` and are JWT-protected (``IsAuthenticated``
declared per view). Included from ``forex/urls.py`` with a bare ``include()``
(mirrors ``forex/broker/urls.py`` — full paths are declared here).
"""
from django.urls import path

from . import views

urlpatterns = [
    path('api/sync/status/', views.sync_status, name='api-sync-status'),
    path('api/sync/ticks/', views.sync_ticks, name='api-sync-ticks'),
    path('api/sync/backfill/', views.sync_backfill, name='api-sync-backfill'),
]