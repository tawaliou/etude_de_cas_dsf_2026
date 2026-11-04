"""Use cases of the gateway. Each public function runs in a single transaction."""

import logging
import uuid
from datetime import date, datetime

from django.db import IntegrityError, transaction
from django.db.models import Sum

from . import clock, domain
from .api.exceptions import Conflict, InvalidInput, NotFound, QuotaExceeded
from .auth import ClientApp
from .models import CANCELLABLE_STATUSES, Campaign, Category, Sms, SmsEvent, SmsStatus, sources_of

logger = logging.getLogger("gateway")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def transition(sms_id, target: str, *, allowed_from: set[str] | None = None, **fields) -> bool:
    """Move an SMS to `target` only if its current status allows it.

    Conditional UPDATE (compare-and-set): safe against concurrent workers, reports
    and cancellations. Returns False when the SMS was not in an allowed status.
    """
    sources = sources_of(target) if allowed_from is None else allowed_from & sources_of(target)
    updated = Sms.objects.filter(pk=sms_id, status__in=sources).update(
        status=target, updated_at=clock.now(), **fields
    )
    return updated == 1


def record_event(sms_id, kind: str, detail: str = "") -> None:
    SmsEvent.objects.create(sms_id=sms_id, kind=kind, detail=detail[:255])


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def get_app_sms(app: ClientApp, sms_id) -> Sms:
    """An SMS of the calling app. Other apps' SMS are reported as not found."""
    try:
        sms_id = uuid.UUID(str(sms_id))
    except ValueError:
        raise NotFound("SMS introuvable.", code="sms_not_found") from None
    sms = Sms.objects.filter(app_code=app.code, pk=sms_id).first()
    if sms is None:
        raise NotFound("SMS introuvable.", code="sms_not_found")
    return sms


def daily_usage(app: ClientApp) -> dict:
    today = clock.local_date()
    used = used_segments(app.code, today)
    return {
        "date": today,
        "quota": app.daily_quota,
        "used": used,
        "remaining": max(app.daily_quota - used, 0),
    }


def cancel_sms(app: ClientApp, sms_id) -> Sms:
    """Cancel an SMS not yet transmitted. Its segments go back to the quota."""
    sms = get_app_sms(app, sms_id)
    if not transition(sms.pk, SmsStatus.ANNULE, allowed_from=CANCELLABLE_STATUSES):
        sms.refresh_from_db()
        raise Conflict(
            f"Annulation impossible : le SMS est au statut {sms.status}. "
            "Seul un SMS en attente ou programmé peut être annulé.",
            code="not_cancellable",
            details={"status": sms.status},
        )
    record_event(sms.pk, SmsEvent.Kind.ANNULATION, "Annulé par l'application.")
    sms.refresh_from_db()
    logger.info("sms cancelled id=%s app=%s ref=%s", sms.id, app.code, sms.reference)
    return sms


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
            now = clock.now()
            sms = _create_sms(
                app, to=to, text=text, category=category, reference=reference, send_at=send_at,
                fingerprint=fingerprint, now=now, used=used_segments(app.code, clock.local_date(now)),
            )
    except IntegrityError:
        # Same reference inserted concurrently: behave as an idempotent replay.
        existing = Sms.objects.get(app_code=app.code, reference=reference)
        return _replay(existing, fingerprint), False
    return sms, True


def _create_sms(
    app: ClientApp,
    *,
    to: str,
    text: str,
    category: str,
    reference: str,
    send_at: datetime | None,
    fingerprint: str,
    now: datetime,
    used: int,
    campaign: Campaign | None = None,
) -> Sms:
    """Validate and store one SMS. Raises InvalidInput or QuotaExceeded (nothing stored).

    `used` is the number of segments already charged to the app today.
    """
    try:
        operator = domain.detect_operator(to)
        segments = domain.check_segments(text)
    except domain.InvalidSms as exc:
        raise InvalidInput(exc.message, details={exc.field: [exc.message]}) from exc

    if used + segments > app.daily_quota:
        raise QuotaExceeded(
            f"Quota journalier dépassé : {used}/{app.daily_quota} segments utilisés, "
            f"ce SMS en demande {segments}.",
            details={"used": used, "quota": app.daily_quota, "requested": segments},
        )

    requested_at = send_at if send_at is not None and send_at > now else now
    send_time = domain.earliest_send_time(category, requested_at)
    deferred = send_time > now  # scheduled by the app and/or outside the INFO window
    sms = Sms.objects.create(
        app_code=app.code,
        reference=reference,
        to=to,
        operator=operator,
        category=category,
        text=text,
        content_hash=fingerprint,
        segments=segments,
        status=SmsStatus.PROGRAMME if deferred else SmsStatus.EN_ATTENTE,
        scheduled_at=send_at if requested_at != now else None,
        next_attempt_at=send_time if deferred else None,
        quota_day=clock.local_date(now),
        campaign=campaign,
    )
    if send_time != requested_at:
        record_event(sms.pk, SmsEvent.Kind.DIFFERE, _deferral_detail(send_time))
    logger.info(
        "sms accepted id=%s app=%s ref=%s category=%s operator=%s segments=%s status=%s",
        sms.id, app.code, reference, category, operator, segments, sms.status,
    )  # never log the text (OTP confidentiality)
    return sms


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def submit_campaign(
    app: ClientApp,
    *,
    reference: str,
    text: str,
    recipients: list[str],
    send_at: datetime | None = None,
) -> tuple[Campaign, bool]:
    """Send the same INFO text to each recipient, treated one by one.

    A recipient is accepted while the quota allows it, refused afterwards; invalid
    or duplicated numbers are refused. Returns (campaign, created) like submit_sms.
    """
    fingerprint = domain.content_hash(to=",".join(recipients), text=text, category=Category.INFO, send_at=send_at)
    try:
        with transaction.atomic():
            existing = Campaign.objects.filter(app_code=app.code, reference=reference).first()
            if existing is not None:
                return _replay(existing, fingerprint), False

            try:  # the text is common to all recipients: an invalid text refuses the campaign
                domain.check_segments(text)
            except domain.InvalidSms as exc:
                raise InvalidInput(exc.message, details={exc.field: [exc.message]}) from exc

            now = clock.now()
            used = used_segments(app.code, clock.local_date(now))
            campaign = Campaign.objects.create(
                app_code=app.code, reference=reference, text=text, content_hash=fingerprint,
                scheduled_at=send_at if send_at is not None and send_at > now else None,
                recipients_count=len(recipients), accepted_count=0,
            )
            results, seen = [], set()
            for position, to in enumerate(recipients, start=1):
                result = {"to": to}
                try:
                    if to in seen:
                        raise InvalidInput("Destinataire en double dans la campagne.", code="duplicate_recipient")
                    seen.add(to)
                    sms = _create_sms(
                        app, to=to, text=text, category=Category.INFO,
                        reference=f"campagne:{campaign.id}:{position}", send_at=send_at,
                        fingerprint=domain.content_hash(to=to, text=text, category=Category.INFO, send_at=send_at),
                        now=now, used=used, campaign=campaign,
                    )
                except (InvalidInput, QuotaExceeded) as exc:
                    result.update(accepted=False, error={"code": exc.get_codes(), "message": str(exc.detail)})
                else:
                    used += sms.segments
                    result.update(accepted=True, sms_id=str(sms.id))
                results.append(result)

            campaign.results = results
            campaign.accepted_count = sum(1 for r in results if r["accepted"])
            campaign.save(update_fields=["results", "accepted_count"])
    except IntegrityError:
        existing = Campaign.objects.get(app_code=app.code, reference=reference)
        return _replay(existing, fingerprint), False

    logger.info(
        "campaign accepted id=%s app=%s ref=%s accepted=%s/%s",
        campaign.id, app.code, reference, campaign.accepted_count, campaign.recipients_count,
    )
    return campaign, True


def get_app_campaign(app: ClientApp, campaign_id) -> Campaign:
    try:
        campaign_id = uuid.UUID(str(campaign_id))
    except ValueError:
        raise NotFound("Campagne introuvable.", code="campaign_not_found") from None
    campaign = Campaign.objects.filter(app_code=app.code, pk=campaign_id).first()
    if campaign is None:
        raise NotFound("Campagne introuvable.", code="campaign_not_found")
    return campaign


def _deferral_detail(send_time: datetime) -> str:
    return (
        "Message INFO hors plage horaire (7 h - 21 h, heure du Bénin) : envoi différé au "
        f"{send_time.astimezone(domain.BENIN_TZ):%d/%m/%Y à %H:%M}."
    )


def defer_outside_window(sms: Sms, send_time: datetime) -> bool:
    """Postpone a waiting SMS to `send_time` (used by the worker for INFO outside the window)."""
    updated = Sms.objects.filter(
        pk=sms.pk, status__in=[SmsStatus.EN_ATTENTE, SmsStatus.PROGRAMME]
    ).update(next_attempt_at=send_time, updated_at=clock.now())
    if updated:
        record_event(sms.pk, SmsEvent.Kind.DIFFERE, _deferral_detail(send_time))
        logger.info("sms deferred id=%s until=%s", sms.pk, send_time.isoformat())
    return bool(updated)


def _replay(existing: Sms | Campaign, fingerprint: str):
    """Same reference again: return the existing object, or refuse if the content differs."""
    if existing.content_hash != fingerprint:
        raise Conflict(
            f"La référence « {existing.reference} » a déjà été utilisée avec un contenu différent.",
            code="reference_conflict",
            details={"id": str(existing.id)},
        )
    logger.info("replay %s app=%s ref=%s", existing.id, existing.app_code, existing.reference)
    return existing
