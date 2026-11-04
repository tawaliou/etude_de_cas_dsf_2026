"""Processing of the delivery reports sent back by operators."""

import json
import logging
from dataclasses import dataclass

from django.conf import settings

from .api.exceptions import InvalidInput, InvalidSignature, NotFound
from .dispatch import retry_or_fail
from .models import Sms, SmsEvent, SmsStatus
from .operators import REPORT_STATUSES, verify_signature
from .services import record_event, transition

logger = logging.getLogger("gateway.reports")


@dataclass(frozen=True)
class ReportOutcome:
    applied: bool
    sms_id: str
    sms_status: str
    detail: str


def handle_report(operator: str, body: bytes, signature: str | None) -> ReportOutcome:
    if operator not in settings.OPERATOR_SECRETS:
        raise NotFound(f"Opérateur inconnu : {operator}.", code="unknown_operator")

    if not verify_signature(operator, body, signature):
        _trace_rejected_report(operator, body)
        raise InvalidSignature("Signature du compte rendu invalide : compte rendu ignoré.")

    message_id, status = _parse(body)
    sms = Sms.objects.filter(operator=operator, operator_message_id=message_id).first()
    if sms is None:
        raise NotFound(f"Aucun SMS transmis avec l'identifiant {message_id}.", code="unknown_message")

    if sms.status != SmsStatus.TRANSMIS:
        return _ignored(sms, status, f"le SMS est déjà au statut {sms.status}")

    if status == "ECHEC_TEMPORAIRE":
        # Trace the report before the retry (or final failure) it triggers.
        _record_applied(sms, status, operator, message_id)
        retry_or_fail(sms.pk, sms.attempts, reason="échec temporaire")
    else:
        target = SmsStatus.DELIVRE if status == "DELIVRE" else SmsStatus.ECHOUE
        if not transition(sms.pk, target, allowed_from={SmsStatus.TRANSMIS}):
            sms.refresh_from_db()  # status changed concurrently
            return _ignored(sms, status, f"le SMS est déjà au statut {sms.status}")
        _record_applied(sms, status, operator, message_id)

    sms.refresh_from_db()
    logger.info("report applied sms=%s report=%s new_status=%s", sms.pk, status, sms.status)
    return ReportOutcome(True, str(sms.pk), sms.status, f"Compte rendu {status} pris en compte.")


def _record_applied(sms: Sms, status: str, operator: str, message_id: str) -> None:
    record_event(sms.pk, SmsEvent.Kind.COMPTE_RENDU, f"{status} reçu de {operator} (id {message_id}).")


def _parse(body: bytes) -> tuple[str, str]:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise InvalidInput("Le compte rendu doit être un JSON valide.") from None
    if not isinstance(payload, dict):
        raise InvalidInput("Le compte rendu doit être un objet JSON.")
    message_id, status = payload.get("message_id"), payload.get("status")
    errors = {}
    if not isinstance(message_id, str) or not message_id:
        errors["message_id"] = ["Champ obligatoire."]
    if status not in REPORT_STATUSES:
        errors["status"] = [f"Valeurs possibles : {', '.join(REPORT_STATUSES)}."]
    if errors:
        raise InvalidInput("Compte rendu invalide.", details=errors)
    return message_id, status


def _ignored(sms: Sms, status: str, reason: str) -> ReportOutcome:
    record_event(sms.pk, SmsEvent.Kind.COMPTE_RENDU_IGNORE, f"{status} ignoré : {reason}.")
    logger.info("report ignored sms=%s report=%s reason=%s", sms.pk, status, reason)
    return ReportOutcome(False, str(sms.pk), sms.status, f"Compte rendu {status} ignoré : {reason}.")


def _trace_rejected_report(operator: str, body: bytes) -> None:
    """Keep a trace of a forged report on the SMS it claims to be about (no state change)."""
    try:
        message_id = json.loads(body).get("message_id")
    except (ValueError, UnicodeDecodeError, AttributeError):
        message_id = None
    sms = Sms.objects.filter(operator=operator, operator_message_id=message_id).first() if message_id else None
    if sms is not None:
        record_event(sms.pk, SmsEvent.Kind.COMPTE_RENDU_REJETE, f"Signature invalide (opérateur {operator}).")
    logger.warning("report rejected: invalid signature operator=%s message_id=%s", operator, message_id)
