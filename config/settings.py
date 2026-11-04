"""Django settings for the shared SMS gateway."""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-secret-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "gateway",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite in IMMEDIATE mode: every write transaction takes the write lock upfront,
# so quota checks and idempotency checks are serialized between concurrent requests.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("DATABASE_PATH", BASE_DIR / "db.sqlite3"),
        "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 20},
    }
}

LANGUAGE_CODE = "fr"
TIME_ZONE = "Africa/Porto-Novo"  # Benin, GMT+1, no daylight saving
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Gateway configuration
# ---------------------------------------------------------------------------

# Client applications: code -> API key and daily quota (in segments).
CLIENT_APPS = {
    "ETATCIVIL": {
        "api_key": os.environ.get("ETATCIVIL_API_KEY", "etatcivil-test-key"),
        "daily_quota": 100,
    },
    "COURRIER": {
        "api_key": os.environ.get("COURRIER_API_KEY", "courrier-test-key"),
        "daily_quota": 5,
    },
}

# Shared secrets used by operators to sign their delivery reports (HMAC-SHA256).
OPERATOR_SECRETS = {
    "CELTIIS": os.environ.get("CELTIIS_SECRET", "celtiis-secret"),
    "MTN": os.environ.get("MTN_SECRET", "mtn-secret"),
    "MOOV": os.environ.get("MOOV_SECRET", "moov-secret"),
}

# Public URL of the gateway, used by the operator simulator to send reports back.
# Defaults to the local API on $PORT (8000), the port run.sh starts it on.
GATEWAY_BASE_URL = os.environ.get("GATEWAY_BASE_URL", f"http://127.0.0.1:{os.environ.get('PORT', '8000')}")

# Operator client used by the worker (the simulator, in this exercise).
OPERATOR_CLIENT = os.environ.get("OPERATOR_CLIENT", "gateway.simulator.SimulatedOperator")

# Delays (seconds) before retries 1, 2 and 3 after a temporary failure. Short for the demo.
RETRY_DELAYS_SECONDS = [int(s) for s in os.environ.get("RETRY_DELAYS_SECONDS", "2,4,8").split(",")]

# Simulator: seconds between the transmission and the delivery report(s).
SIMULATOR_REPORT_DELAY_SECONDS = float(os.environ.get("SIMULATOR_REPORT_DELAY_SECONDS", "1"))

# Worker: seconds between two polls of the due SMS.
WORKER_INTERVAL_SECONDS = float(os.environ.get("WORKER_INTERVAL_SECONDS", "1"))

# Optional fake "now" (Benin local time, ISO format) to test the INFO time window.
GATEWAY_FAKE_NOW = os.environ.get("GATEWAY_FAKE_NOW", "")

# ---------------------------------------------------------------------------
# Django REST Framework / OpenAPI
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["gateway.auth.ApiKeyAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "gateway.api.exceptions.exception_handler",
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Passerelle commune d'envoi de SMS",
    "DESCRIPTION": (
        "API de la passerelle SMS de la DSF. Authentification : en-tête `X-API-Key` "
        "(bouton Authorize)."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "SmsStatusEnum": "gateway.models.SmsStatus",
        "ReportStatusEnum": "gateway.operators.REPORT_STATUSES",
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {
        # Quiet during the test suite (tests capture logs explicitly with assertLogs).
        "gateway": {"handlers": ["console"], "level": "CRITICAL" if "test" in sys.argv[1:2] else "INFO"},
    },
}
