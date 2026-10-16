"""
Auth endpoints — exact contract expected by the Next.js frontend:

    POST /auth/login    {email, password}          -> 200 {access_token, refresh_token, user}
    POST /auth/register {name, email, password}    -> 201 {access_token, refresh_token, user}
    GET  /auth/me       (Bearer access token)      -> 200 {id, name, email, status}
    POST /auth/refresh  {refresh}                  -> 200 {access_token[, refresh_token]}
    POST /auth/logout   (Bearer access token)      -> 200 {message}
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import LoginSerializer, RegisterSerializer, UserSerializer

User = get_user_model()


class LoginRateThrottle(AnonRateThrottle):
    """
    Stricter per-IP throttle for the login endpoint (brute-force mitigation).

    Uses its own cache scope ('login') so it does not share the default anon
    quota; the rate is configured in settings.DEFAULT_THROTTLE_RATES['login'].
    Authenticated requests are never throttled here (login is anonymous).
    """

    scope = 'login'


class RegisterRateThrottle(AnonRateThrottle):
    """
    Per-IP throttle for open registration (abuse mitigation — Security Gate 1
    LOW #21).

    The rate is declared on the class itself (``5/min``) so no change to
    settings.py is required. Like all anon throttles it only applies to
    unauthenticated requests, which is exactly the registration case.
    """

    scope = 'register'
    rate = '5/min'


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([LoginRateThrottle])
def login_view(request):
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    return Response(
        {
            'access_token': data['access_token'],
            'refresh_token': data['refresh_token'],
            'user': UserSerializer(data['user']).data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([RegisterRateThrottle])
def register_view(request):
    serializer = RegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.save()
    refresh = RefreshToken.for_user(user)
    return Response(
        {
            'access_token': str(refresh.access_token),
            'refresh_token': str(refresh),
            'user': UserSerializer(user).data,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me_view(request):
    return Response(UserSerializer(request.user).data, status=status.HTTP_200_OK)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def refresh_view(request):
    refresh_token = request.data.get('refresh')
    if not refresh_token:
        return Response(
            {'detail': 'refresh token is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        refresh = RefreshToken(refresh_token)
    except TokenError:
        return Response(
            {'detail': 'Invalid or expired refresh token.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    data = {'access_token': str(refresh.access_token)}
    if api_settings.ROTATE_REFRESH_TOKENS:
        if api_settings.BLACKLIST_AFTER_ROTATION:
            try:
                refresh.blacklist()
            except AttributeError:
                pass
        refresh.set_jti()
        refresh.set_exp()
        refresh.set_iat()
        data['refresh_token'] = str(refresh)
    return Response(data, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout_view(request):
    # Blacklist the refresh token when provided so it can no longer be used
    # to mint new access tokens. If omitted, the access token simply expires.
    refresh_token = request.data.get('refresh')
    if refresh_token:
        try:
            RefreshToken(refresh_token).blacklist()
        except TokenError:
            pass
    return Response({'message': 'Logged out successfully.'}, status=status.HTTP_200_OK)