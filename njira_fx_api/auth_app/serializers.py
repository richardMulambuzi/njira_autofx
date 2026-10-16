"""
Serializers for the auth_app — implement the exact frontend contract:

    user: {id, name, email, status}

`name` is derived from first_name + last_name (falls back to username),
`status` is derived from the user's `is_active` flag.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import (
    validate_password as django_validate_password,
)
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """Serializes a user into the contract shape {id, name, email, status}."""

    name = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'name', 'email', 'status')

    def get_name(self, obj):
        full_name = f"{obj.first_name} {obj.last_name}".strip()
        return full_name or obj.username

    def get_status(self, obj):
        return 'active' if obj.is_active else 'inactive'


class LoginSerializer(serializers.Serializer):
    """Authenticates by email + password and issues JWT token pair."""

    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')

        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            # Run the default password hasher once to reduce the timing
            # difference between an existing and a nonexistent user, which
            # would otherwise allow username enumeration (Django #20760).
            User().set_password(password)
            raise serializers.ValidationError({'detail': 'Invalid email or password.'})
        if not user.check_password(password):
            raise serializers.ValidationError({'detail': 'Invalid email or password.'})
        if not user.is_active:
            raise serializers.ValidationError({'detail': 'User account is disabled.'})

        refresh = RefreshToken.for_user(user)
        attrs['user'] = user
        attrs['access_token'] = str(refresh.access_token)
        attrs['refresh_token'] = str(refresh)
        return attrs


class RegisterSerializer(serializers.ModelSerializer):
    """Creates a user from {name, email, password} and returns the user object."""

    name = serializers.CharField(max_length=255, write_only=True)
    password = serializers.CharField(write_only=True, min_length=8, trim_whitespace=False)

    class Meta:
        model = User
        fields = ('name', 'email', 'password')

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('A user with this email already exists.')
        return value

    def validate_password(self, value):
        """Enforce Django's AUTH_PASSWORD_VALIDATORS on top of min_length=8."""
        try:
            django_validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def create(self, validated_data):
        name = validated_data.pop('name', '')
        parts = name.strip().split(maxsplit=1)
        first_name = parts[0] if parts else ''
        last_name = parts[1] if len(parts) > 1 else ''
        email = validated_data['email']
        return User.objects.create_user(
            username=email,
            email=email,
            first_name=first_name,
            last_name=last_name,
            password=validated_data['password'],
        )