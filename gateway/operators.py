"""Contract between the gateway and the operators.

The gateway only knows `OperatorClient.send()` and the report signature scheme;
it does not know whether the operator is real or simulated.
"""

import hashlib
import hmac
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from django.conf import settings
from django.utils.module_loading import import_string

SIGNATURE_HEADER = "X-Signature"
REPORT_STATUSES = ("DELIVRE", "ECHEC_TEMPORAIRE", "ECHEC_DEFINITIF")


@dataclass(frozen=True)
class OutgoingSms:
    sms_id: str
    to: str
    operator: str
    text: str
    attempt: int

    def __repr__(self):  # keep the text (maybe an OTP) out of any log
        return f"OutgoingSms({self.sms_id}, {self.operator}, attempt={self.attempt})"


@dataclass(frozen=True)
class SendResult:
    """Immediate answer of the operator: accepted (with its message id) or unavailable."""

    accepted: bool
    message_id: str | None = None
    error: str | None = None


class OperatorClient(Protocol):
    def send(self, sms: OutgoingSms) -> SendResult: ...


@lru_cache(maxsize=1)
def get_operator_client() -> OperatorClient:
    return import_string(settings.OPERATOR_CLIENT)()


def sign(operator: str, body: bytes) -> str:
    """HMAC-SHA256 (hex) of the raw report body with the operator's shared secret."""
    secret = settings.OPERATOR_SECRETS[operator]
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(operator: str, body: bytes, signature: str | None) -> bool:
    if not signature or operator not in settings.OPERATOR_SECRETS:
        return False
    return hmac.compare_digest(sign(operator, body), signature)
