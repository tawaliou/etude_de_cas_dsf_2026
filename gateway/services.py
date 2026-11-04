"""Use cases of the gateway. Each public function runs in a single transaction."""

import logging
from datetime import date, datetime

from django.db import IntegrityError, transaction
from django.db.models import Sum

from . import clock, domain
from .api.exceptions import Conflict, InvalidInput, QuotaExceeded
from .auth import ClientApp
from .models import Sms, SmsStatus

logger = logging.getLogger("gateway")


def used_segments(app_code: str, day: date) -> int:
    """Segments charged to an application for a Benin calendar day (cancelled SMS excluded)."""
    total = (
        Sms.objects.filter(app_code=app_code, quota_day=day)
        .exclude(status=SmsStatus.ANNULE)
        .aggregate(total=Sum("segments"))["total"]
    )
    return total or 0


def submit_sms(
    app: ClientApp,
    *,
    to: str,
    text: str,
    category: str,
    reference: str,
    send_at: datetime | None = None,
) -> tuple[Sms, bool]:
    """Accept an SMS for later transmission.

    Returns (sms, created). `created` is False when the same reference was already
    submitted with the same content (idempotent replay).
    """
    fingerprint = domain.content_hash(to=to, text=text, category=category, send_at=send_at)
    try:
        with transaction.atomic():
            existing = Sms.objects.filter(app_code=app.code, reference=reference).first()
            if existing is not None:
                return _replay(existing, fingerprint), False

            try:
                operator = domain.detect_operator(to)
                segments = domain.check_segments(text)
            except domain.InvalidSms as exc:
                raise InvalidInput(exc.message, details={exc.field: [exc.message]}) from exc

            now = clock.now()
            today = clock.local_date(now)
            used = used_segments(app.code, today)
            if used + segments > app.daily_quota:
                raise QuotaExceeded(
                    f"Quota journalier dépassé : {used}/{app.daily_quota} segments utilisés, "
                    f"ce SMS en demande {segments}.",
                    details={"used": used, "quota": app.daily_quota, "requested": segments},
                )

            scheduled = send_at is not None and send_at > now
            sms = Sms.objects.create(
                app_code=app.code,
                reference=reference,
                to=to,
                operator=operator,
                category=category,
                text=text,
                content_hash=fingerprint,
                segments=segments,
                status=SmsStatus.PROGRAMME if scheduled else SmsStatus.EN_ATTENTE,
                scheduled_at=send_at if scheduled else None,
                quota_day=today,
            )
    except IntegrityError:
        # Same reference inserted concurrently: behave as an idempotent replay.
        existing = Sms.objects.get(app_code=app.code, reference=reference)
        return _replay(existing, fingerprint), False

    logger.info(
        "sms accepted id=%s app=%s ref=%s category=%s operator=%s segments=%s status=%s",
        sms.id, app.code, reference, category, operator, segments, sms.status,
    )  # never log the text (OTP confidentiality)
    return sms, True


def _replay(existing: Sms, fingerprint: str) -> Sms:
    if existing.content_hash != fingerprint:
        raise Conflict(
            f"La référence « {existing.reference} » a déjà été utilisée avec un contenu différent.",
            code="reference_conflict",
            details={"sms_id": str(existing.id)},
        )
    logger.info("sms replay id=%s app=%s ref=%s", existing.id, existing.app_code, existing.reference)
    return existing
