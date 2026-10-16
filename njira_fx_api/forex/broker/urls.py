"""URL routes for the provider-independent broker API endpoints."""
from django.urls import path

from . import views

urlpatterns = [
    path('api/provider/status/', views.provider_status, name='api-provider-status'),
    path('api/provider/account/', views.provider_account, name='api-provider-account'),
    path('api/provider/positions/', views.provider_positions, name='api-provider-positions'),
    path('api/provider/orders/', views.provider_orders, name='api-provider-orders'),
    path('api/provider/trade-history/', views.provider_trade_history, name='api-provider-trade-history'),
    path('api/provider/symbols/', views.provider_symbols, name='api-provider-symbols'),
    path('api/provider/candles/', views.provider_candles, name='api-provider-candles'),
    path('api/provider/market-data/', views.provider_market_data, name='api-provider-market-data'),
    path('api/provider/switch/', views.provider_switch, name='api-provider-switch'),
]
