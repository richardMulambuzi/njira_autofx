"""
JWT enforcement for plain Django views.

DRF's @api_view decorator does NOT support async (coroutine) views, so the
async function views across the project (trade/, bot_settings/) need an
async-safe auth guard. This decorator validates the Bearer access token via
SimpleJWT's JWTAuthentication and sets ``request.user`` exactly like DRF would.
"""
from functools import wraps

from asgiref.sync import sync_to_async
from django.http import JsonResponse
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken


def jwt_required_async(view_func):
    """
    Require a valid JWT access token on an async Django view.

    Returns 401 JSON when the Authorization header is missing or the token is
    invalid/expired. Preserves the original view's csrf_exempt behaviour and
    business logic — auth only.

    JWTAuthentication performs a synchronous DB lookup (user fetch), so it is
    executed via sync_to_async to satisfy Django's async-safety guard.
    """

    @wraps(view_func)
    async def wrapper(request, *args, **kwargs):
        auth = JWTAuthentication()
        try:
            result = await sync_to_async(auth.authenticate)(request)
        except (InvalidToken, AuthenticationFailed):
            return JsonResponse({'detail': 'Invalid or expired token.'}, status=401)
        if result is None:
            return JsonResponse(
                {'detail': 'Authentication credentials were not provided.'},
                status=401,
            )
        user, _ = result
        request.user = user
        return await view_func(request, *args, **kwargs)

    return wrapper