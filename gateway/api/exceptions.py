"""Uniform JSON error format for the API.

Every error is returned as:
    {"error": {"code": "<machine_code>", "message": "<human message>", "details": {...}}}
"""

from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.views import exception_handler as drf_exception_handler


class GatewayError(APIException):
    """Base class for business errors raised by the gateway services."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "bad_request"
    default_detail = "Requête invalide."

    def __init__(self, message: str | None = None, code: str | None = None, details=None):
        super().__init__(detail=message or self.default_detail, code=code or self.default_code)
        self.details = details


class NotFound(GatewayError):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = "not_found"
    default_detail = "Ressource introuvable."


class Conflict(GatewayError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"
    default_detail = "Conflit avec l'état actuel de la ressource."


class QuotaExceeded(GatewayError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = "quota_exceeded"
    default_detail = "Quota journalier dépassé."


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return None  # unexpected error: let Django log it

    if isinstance(exc, GatewayError):
        body = {"code": exc.get_codes(), "message": str(exc.detail)}
        if exc.details is not None:
            body["details"] = exc.details
    elif isinstance(exc, ValidationError):
        body = {"code": "validation_error", "message": "Données invalides.", "details": response.data}
    else:
        codes = exc.get_codes() if isinstance(exc, APIException) else None
        message = response.data.get("detail", str(exc)) if isinstance(response.data, dict) else str(exc)
        body = {"code": codes if isinstance(codes, str) else "error", "message": str(message)}

    response.data = {"error": body}
    return response
