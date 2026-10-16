from django.urls import path
from . import views

urlpatterns = [
    # Legacy Deriv WebSocket routes (multiplier/, testapp, getpt, closeTrades)
    # removed with the Deriv backend.  Remaining endpoints are decommission
    # stubs that answer 410 with guidance to POST /api/trade/.
    path('testcontract', views.testContract, name='testContract'),
]
