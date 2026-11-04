"""Pure business rules (no database access)."""

import hashlib
import json
import math
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ASCII_SEGMENT_SIZE = 160
UNICODE_SEGMENT_SIZE = 70
MAX_SEGMENTS = 6

BENIN_TZ = ZoneInfo("Africa/Porto-Novo")
INFO_WINDOW_START = time(7, 0)  # INFO SMS leave only between 07:00 (included)
INFO_WINDOW_END = time(21, 0)  # and 21:00 (excluded), Benin time

PHONE_NUMBER_RE = re.compile(r"^01\d{8}$")

# (lowest, highest, operator) on the two digits following "01".
OPERATOR_RANGES = [
    (40, 49, "CELTIIS"),
    (50, 69, "MTN"),
    (90, 99, "MOOV"),
]


class InvalidSms(ValueError):
    """A business rule rejects the SMS. `field` names the offending input."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def segment_size(text: str) -> int:
    return ASCII_SEGMENT_SIZE if text.isascii() else UNICODE_SEGMENT_SIZE


def count_segments(text: str) -> int:
    """Number of segments: length / segment size, rounded up (at least 1).

    Length is counted in Unicode code points (Python `len`).
    """
    return max(1, math.ceil(len(text) / segment_size(text)))


def check_segments(text: str) -> int:
    segments = count_segments(text)
    if segments > MAX_SEGMENTS:
        raise InvalidSms(
            "text",
            f"Le texte fait {segments} segments, le maximum est {MAX_SEGMENTS} "
            f"({MAX_SEGMENTS * segment_size(text)} caractères pour ce texte).",
        )
    return segments


def detect_operator(phone_number: str) -> str:
    """Operator of a recipient number, or InvalidSms if the number is not valid."""
    if not PHONE_NUMBER_RE.match(phone_number):
        raise InvalidSms("to", "Le numéro doit comporter 10 chiffres et commencer par 01.")
    prefix = int(phone_number[2:4])
    for lowest, highest, operator in OPERATOR_RANGES:
        if lowest <= prefix <= highest:
            return operator
    raise InvalidSms("to", f"Numéro non attribué : aucun opérateur pour le préfixe 01{prefix:02d}.")


def content_hash(*, to: str, text: str, category: str, send_at: datetime | None) -> str:
    """Fingerprint of the SMS content, used to detect a reused reference."""
    payload = json.dumps(
        {"to": to, "text": text, "category": category, "send_at": send_at.isoformat() if send_at else None},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def earliest_send_time(category: str, instant: datetime) -> datetime:
    """First instant >= `instant` at which an SMS of this category may leave.

    OTP: immediately. INFO: inside the 07:00-21:00 Benin window, otherwise
    deferred to the next 07:00.
    """
    if category != "INFO":
        return instant
    local = instant.astimezone(BENIN_TZ)
    if INFO_WINDOW_START <= local.time() < INFO_WINDOW_END:
        return instant
    day = local.date() if local.time() < INFO_WINDOW_START else local.date() + timedelta(days=1)
    return datetime.combine(day, INFO_WINDOW_START, tzinfo=BENIN_TZ)
