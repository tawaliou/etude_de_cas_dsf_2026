"""Transmission of due SMS to operators, with retries (run by the worker)."""

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Case, IntegerField, Q, Value, When

from . import clock, domain
from .models import Category, Sms, SmsEvent, SmsStatus
from .operators import OutgoingSms, SendResult, get_operator_client
from .services import defer_outside_window, record_event, transition

logger = logging.getLogger("gateway.dispatch")

RETRIES = 3  # "au plus 3 nouvelles tentatives"
MAX_ATTEMPTS = 1 + RETRIES


def retry_delay(attempt: int) -> timedelta:
    """Delay before the attempt following `attempt` (increasing backoff)."""
    delays = settings.RETRY_DELAYS_SECONDS
    return timedelta(seconds=delays[min(attempt - 1, len(delays) - 1)])


def due_sms(limit: int = 50):
    """SMS ready to be transmitted now: OTP first, then oldest first."""
    now = clock.now()
    return (
        Sms.objects.filter(status__in=[SmsStatus.EN_ATTENTE, SmsStatus.PROGRAMME])
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .annotate(
            priority=Case(When(category=Category.OTP, then=Value(0)), default=Value(1), output_field=IntegerField())
        )
        .order_by("priority", "created_at")[:limit]
    )


def dispatch_due(limit: int = 50) -> int:
    """Transmit every due SMS. Returns the number of SMS handed to an operator."""
    return sum(1 for sms in due_sms(limit) if dispatch_one(sms))


def dispatch_one(sms: Sms) -> bool:
    # INFO may only leave inside the 07:00-21:00 window (a retry or a backlog can
    # cross 21:00): postpone it to the next opening instead of transmitting.
    now = clock.now()
    send_time = domain.earliest_send_time(sms.category, now)
    if send_time > now:
        defer_outside_window(sms, send_time)
        return False

    attempt = sms.attempts + 1
    # Claim the SMS (compare-and-set): if it was cancelled or taken meanwhile, skip it.
    claimed = transition(
        sms.pk,
        SmsStatus.TRANSMIS,
        allowed_from={SmsStatus.EN_ATTENTE, SmsStatus.PROGRAMME},
        attempts=attempt,
        next_attempt_at=None,
        operator_message_id=None,
    )
    if not claimed:
        return False

    outgoing = OutgoingSms(sms_id=str(sms.pk), to=sms.to, operator=sms.operator, text=sms.text, attempt=attempt)
    try:
        result = get_operator_client().send(outgoing)
    except Exception:  # operator client crashed: treat as unavailable
        logger.exception("operator call failed sms=%s", sms.pk)
        result = SendResult(accepted=False, error="Erreur lors de l'appel à l'opérateur.")

    if result.accepted:
        Sms.objects.filter(pk=sms.pk).update(operator_message_id=result.message_id)
        record_event(
            sms.pk, SmsEvent.Kind.TRANSMISSION,
            f"Tentative {attempt} acceptée par {sms.operator} (id {result.message_id}).",
        )
        logger.info("sms transmitted id=%s operator=%s attempt=%s", sms.pk, sms.operator, attempt)
    else:
        record_event(sms.pk, SmsEvent.Kind.OPERATEUR_INDISPONIBLE, f"Tentative {attempt} : {result.error}")
        logger.info("operator unavailable sms=%s operator=%s attempt=%s", sms.pk, sms.operator, attempt)
        retry_or_fail(sms.pk, attempt, reason="opérateur indisponible")
    return True


def retry_or_fail(sms_id, attempt: int, *, reason: str) -> None:
    """After a temporary failure: schedule a new attempt, or fail after the last one."""
    if attempt < MAX_ATTEMPTS:
        next_at = clock.now() + retry_delay(attempt)
        if transition(sms_id, SmsStatus.EN_ATTENTE, allowed_from={SmsStatus.TRANSMIS}, next_attempt_at=next_at):
            record_event(
                sms_id, SmsEvent.Kind.NOUVELLE_TENTATIVE,
                f"Après {reason} : tentative {attempt + 1}/{MAX_ATTEMPTS} à "
                f"{next_at.astimezone(clock.BENIN_TZ):%H:%M:%S} (heure du Bénin).",
            )
    elif transition(sms_id, SmsStatus.ECHOUE, allowed_from={SmsStatus.TRANSMIS}):
        record_event(sms_id, SmsEvent.Kind.ECHEC, f"Après {reason} : {MAX_ATTEMPTS} tentatives épuisées.")
        logger.info("sms failed id=%s after %s attempts", sms_id, attempt)
