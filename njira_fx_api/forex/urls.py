from django.contrib import admin
from django.urls import path
from django.urls import include, path

# bot setting urls
from bot_settings.views import save_token_and_strategy
from bot_settings.views import update_trading_status
from bot_settings.views import get_start_time
from bot_settings.views import get_strategy
from bot_settings.views import save_symbols
from bot_settings.views import get_symbol
from bot_settings.views import delete_symbol
from bot_settings.views import symbols_catalogue_list
from bot_settings.views import symbols_catalogue_add
from bot_settings.views import symbols_catalogue_delete
from bot_settings.views import save_risks
from bot_settings.views import profit_and_loss_margin
from bot_settings.views import get_risks
from bot_settings.views import get_profit_and_loss_margin
from bot_settings.views import delete_candles
from forex.swagger import schema_view
from bot_settings.views import get_candles
from bot_settings.views import get_latest_candle



from django.urls import path, include
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

# trade module
from trade.views import executeTrade2
from trade.views import testContract
from trade.views import terminateTrade

# subscribe
from bot_settings.views import Unsubscribe
from bot_settings.views import Subscribe

# ai
from trading_optimizer.views import optimize_signal_view
from trading_optimizer.views import  optimalTradeTime

# pepperstone pipeline (JWT-protected)
from forex.ticks_views import ingest_tick
from forex.ticks_views import chart_data
from forex.ticks_views import mtf_status
from forex.ticks_views import place_trade

# dashboard market-data API (read-only, AllowAny) — DASHBOARD-API-CONTRACT
from forex.market_api.views import (
    market_candles,
    chart_drawings,
    market_analyze,
    signals_live,
    signals_history,
    strategies_performance,
    symbols_config,
    realtime_test,
    market_candles_v2,
    signals_live_v2,
    signals_snr,
)

# volume profile API (read-only, AllowAny) — backend-dev
from forex.market_api.volume_profile_views import (
    volume_profile_current,
    volume_profile_confluence,
    volume_profile_diagnostics,
    volume_profile_events,
    volume_profile_historical,
)

# broker abstraction layer — provider-independent API endpoints (backend-dev)
from forex.broker.urls import urlpatterns as broker_urlpatterns

# tick synchronization engine — /api/sync/* endpoints (contracts §7, T7)
from forex.sync.urls import urlpatterns as sync_urlpatterns

# open health-check endpoint (AllowAny, no JWT) — backend-dev
from forex.health import HealthCheckView

from forex.dashboard_view import (
    console_markets,
    console_signals,
    console_strategies,
    console_system,
    dashboard,
    dashboard_live,
    dashboard_logout,
)

# backtest engine (JWT-protected) — backend-dev
from forex.backtest.views import (
    backtest_upload,
    backtest_upload_stream,
    backtest_results,
    backtest_result_detail,
)

urlpatterns = [
    # swagger / redoc stay open for documentation (AllowAny set in forex/swagger.py)
    path('swagger/', schema_view.with_ui('swagger', cache_timeout=2), name='schema-swagger-ui'),
    path('doc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),

    path('admin/', admin.site.urls),

    # auth (JWT) — open endpoints
    path('auth/', include('auth_app.urls')),

    path('notifications/', include('notifications.routes')),   
    path('trade/', include('trade.routes')),  
    
    # bot setings
    path('save-strategy/',save_token_and_strategy, name='save_token_and_strategy' ),
    path('save_symbols/', save_symbols, name='save_symbols' ),
    path('update-trading/', update_trading_status, name='update_trading_status'),
    path('Get-start-time/', get_start_time, name='get_start_time'),
    path('choosen-strategy/', get_strategy, name='get_strategy' ),
    path('get_candles/', get_candles, name='get_candles' ),
    path('get_latest_candle/', get_latest_candle, name='get_latest_candle' ),
    path('get_symbols/', get_symbol, name='get_symbol' ),
    path('delete_symbols/', delete_symbol, name='delete_symbol' ),

    # Symbols catalogue (DB-backed, UI-managed)
    path('api/symbols/catalogue/', symbols_catalogue_list, name='symbols-catalogue-list'),
    path('api/symbols/catalogue/add/', symbols_catalogue_add, name='symbols-catalogue-add'),
    path('api/symbols/catalogue/delete/', symbols_catalogue_delete, name='symbols-catalogue-delete'),
    path('save_risks/', save_risks, name='save_risks' ),
    path('get_risks/', get_risks, name='get_risks' ),
    path('save_profit_and_loss/', profit_and_loss_margin, name='profit_and_loss_margin' ),
    path('get_profit_and_loss/', get_profit_and_loss_margin, name='get_profit_and_loss_margin' ),
    path('delete-candles/', delete_candles, name='delete_candles' ),

    # getting trading contarct
    path('testContract/', testContract, name='testContract'),

    # place trade
    path('placeTrade/', executeTrade2, name='placeTrade'),
    path('terminateTrade/', terminateTrade, name='terminateTrade' ),

    # suscribe
    path('subscribe/', Subscribe, name='subscribe' ),
    path('unsubscribe/', Unsubscribe, name='unsubscribe' ),

    # ai 
    path('optimise/', optimize_signal_view, name='optimise' ),
    path('optimalTime/',  optimalTradeTime, name='optimalTime' ),

    # pepperstone pipeline — JWT-protected (global IsAuthenticated default)
    path('api/ticks/', ingest_tick, name='api-ticks'),
    path('api/chart-data/', chart_data, name='api-chart-data'),
    path('api/mtf-status/', mtf_status, name='api-mtf-status'),
    path('api/trade/', place_trade, name='api-trade'),

    # dashboard market-data API (DASHBOARD-API-CONTRACT) — read-only, AllowAny
    path('api/market/candles/', market_candles, name='api-market-candles'),
    path('api/chart-drawings/', chart_drawings, name='api-chart-drawings'),
    path('api/market/analyze/<str:symbol>/', market_analyze, name='api-market-analyze'),
    path('api/signals/live/', signals_live, name='api-signals-live'),
    path('api/signals/history/', signals_history, name='api-signals-history'),
    path('api/strategies/performance/', strategies_performance, name='api-strategies-performance'),

    # trading symbol/asset config + realtime (Pusher) debug endpoint
    path('api/symbols/', symbols_config, name='api-symbols'),
    path('api/realtime/test/', realtime_test, name='api-realtime-test'),

    # Phase 5: enhanced endpoints (new candle engine + signal engine)
    path('api/market/candles-v2/', market_candles_v2, name='api-market-candles-v2'),
    path('api/signals/live-v2/', signals_live_v2, name='api-signals-live-v2'),
    path('api/v1/signals/snr/', signals_snr, name='api-signals-snr'),

    # Volume Profile API (read-only, AllowAny)
    path('api/volume-profile/current/', volume_profile_current, name='api-volume-profile-current'),
    path('api/volume-profile/confluence/', volume_profile_confluence, name='api-volume-profile-confluence'),
    path('api/volume-profile/diagnostics/', volume_profile_diagnostics, name='api-volume-profile-diagnostics'),
    path('api/volume-profile/events/', volume_profile_events, name='api-volume-profile-events'),
    path('api/volume-profile/historical/', volume_profile_historical, name='api-volume-profile-historical'),

    # broker abstraction layer — provider-independent endpoints (all /api/provider/*)
    path('', include(broker_urlpatterns)),

    # tick synchronization engine — additive /api/sync/* endpoints (contracts §7)
    path('', include(sync_urlpatterns)),

    # open health-check endpoint (AllowAny, no JWT)
    path('api/health/', HealthCheckView.as_view(), name='api-health'),

    path('dashboard/', dashboard, name='dashboard'),
    path('dashboard/live/', dashboard_live, name='dashboard-live'),
    path('dashboard/logout/', dashboard_logout, name='dashboard-logout'),
    path('markets/', console_markets, name='console-markets'),
    path('signals/', console_signals, name='console-signals'),
    path('strategies/', console_strategies, name='console-strategies'),
    path('system/', console_system, name='console-system'),

    # backtest engine (JWT-protected) — backend-dev
    path('api/backtest/upload/', backtest_upload, name='api-backtest-upload'),
    path('api/backtest/upload/stream/', backtest_upload_stream, name='api-backtest-upload-stream'),
    path('api/backtest/results/', backtest_results, name='api-backtest-results'),
    path('api/backtest/results/<int:pk>/', backtest_result_detail, name='api-backtest-result-detail'),

]
