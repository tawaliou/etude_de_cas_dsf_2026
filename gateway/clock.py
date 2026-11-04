"""Single source of time for the gateway.

`GATEWAY_FAKE_NOW` (Benin local time, ISO format, e.g. "2026-11-04T23:30") shifts
the clock: the gateway starts at that instant and time keeps flowing from there.
It lets the jury test the INFO time window without waiting for the night.
"""

from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

BENIN_TZ = ZoneInfo("Africa/Porto-Novo")


@lru_cache(maxsize=None)
def _offset(fake_now: str):
    fake = datetime.fromisoformat(fake_now)
    if timezone.is_naive(fake):
        fake = fake.replace(tzinfo=BENIN_TZ)
    return fake - timezone.now()


def now() -> datetime:
    """Current instant (timezone-aware, UTC)."""
    real = timezone.now()
    fake_now = settings.GATEWAY_FAKE_NOW
    if not fake_now:
        return real
    return real + _offset(fake_now)


def local_date(instant: datetime | None = None) -> date:
    """Calendar day in Benin (GMT+1) for the given instant (default: now)."""
    return (instant or now()).astimezone(BENIN_TZ).date()
