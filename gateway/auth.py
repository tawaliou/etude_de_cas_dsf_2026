"""Client application registry and API key authentication."""

import hmac
from dataclasses import dataclass

from django.conf import settings
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions

API_KEY_HEADER = "X-API-Key"


@dataclass(frozen=True)
class ClientApp:
    """A client application declared in settings.CLIENT_APPS.

    Used as `request.user` once authenticated.
    """

    code: str
    api_key: str
    daily_quota: int

    is_authenticated = True


def get_client_apps() -> list[ClientApp]:
    return [
        ClientApp(code=code, api_key=conf["api_key"], daily_quota=conf["daily_quota"])
        for code, conf in settings.CLIENT_APPS.items()
    ]


def get_client_app(code: str) -> ClientApp | None:
    return next((app for app in get_client_apps() if app.code == code), None)


def find_app_by_key(api_key: str) -> ClientApp | None:
    # Constant-time comparison on every key to avoid timing leaks.
    match = None
    for app in get_client_apps():
        if hmac.compare_digest(app.api_key.encode(), api_key.encode()):
            match = app
    return match


class ApiKeyAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        api_key = request.headers.get(API_KEY_HEADER)
        if not api_key:
            return None  # -> 401 "authentication credentials were not provided"
        app = find_app_by_key(api_key)
        if app is None:
            raise exceptions.AuthenticationFailed("Clé d'API invalide.")
        return app, api_key

    def authenticate_header(self, request):
        # Makes DRF answer 401 (instead of 403) when credentials are missing.
        return API_KEY_HEADER


class ApiKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "gateway.auth.ApiKeyAuthentication"
    name = "ApiKeyAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "apiKey", "in": "header", "name": API_KEY_HEADER}
