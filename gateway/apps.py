from django.apps import AppConfig


class GatewayConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gateway"

    def ready(self):
        # Registers the OpenAPI extension describing the X-API-Key scheme.
        from . import auth  # noqa: F401
