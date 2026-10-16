from django.http import JsonResponse

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

# ---------------------------------------------------------------------------
# Legacy Deriv WebSocket trading endpoints — DECOMMISSIONED.
#
# The Deriv broker backend was removed in favour of the Pepperstone pipeline
# (forex/ticks_views.place_trade → POST /api/trade/).
#
# Every endpoint below is now a 410 stub so legacy clients receive a clear
# decommission message instead of a silent 404 or a dangling WebSocket
# connection to a retired broker.
# ---------------------------------------------------------------------------


DECOMMISSIONED_MESSAGE = "Decommissioned — use POST /api/trade/"


def _decommissioned_response():
    return JsonResponse(
        {"status": "error", "message": DECOMMISSIONED_MESSAGE},
        status=410,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def executeTrade(request):
    """Decommissioned Deriv trade execution helper (legacy)."""
    return _decommissioned_response()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def executeTrade2(request):
    """Decommissioned Deriv trade placement (legacy POST /placeTrade/)."""
    return _decommissioned_response()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def testApp(request):
    """Decommissioned Deriv test endpoint (legacy)."""
    return _decommissioned_response()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def testContract(request):
    """Decommissioned Deriv contract-monitoring endpoint (legacy)."""
    return _decommissioned_response()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def getpt(request):
    """Decommissioned Deriv profit-table endpoint (legacy)."""
    return _decommissioned_response()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def closeTrades(request):
    """Decommissioned Deriv close-trades endpoint (legacy)."""
    return _decommissioned_response()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def terminateTrade(request):
    """Decommissioned Deriv terminate-trade endpoint (legacy)."""
    return _decommissioned_response()
