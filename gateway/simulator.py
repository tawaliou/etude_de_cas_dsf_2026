"""Operator simulator.

Behaviour is driven by the LAST DIGIT of the recipient number:

    0, 8, 9  accepted, then report DELIVRE
    1        attempt 1: report ECHEC_TEMPORAIRE; next attempts: DELIVRE
    2        report ECHEC_DEFINITIF
    3        always unavailable
    4        report DELIVRE sent twice
    5        report DELIVRE with a wrong signature
    6        unavailable on attempt 1, then accepted and DELIVRE
    7        accepted, but no report ever comes

Reports are sent back to the gateway over HTTP (POST /api/operators/{op}/reports),
signed with HMAC-SHA256, a few moments after the transmission.
"""

import json
import logging
import threading
import uuid
from dataclasses import dataclass

import requests
from django.conf import settings

from . import clock
from .operators import SIGNATURE_HEADER, OutgoingSms, SendResult, sign

logger = logging.getLogger("gateway.simulator")


@dataclass(frozen=True)
class PlannedReport:
    status: str
    bad_signature: bool = False


def plan(to: str, attempt: int) -> tuple[bool, list[PlannedReport]]:
    """(accepted, reports to send) for a transmission attempt. Pure function."""
    digit = to[-1]
    delivered = PlannedReport("DELIVRE")
    if digit == "1":
        return True, [PlannedReport("ECHEC_TEMPORAIRE") if attempt == 1 else delivered]
    if digit == "2":
        return True, [PlannedReport("ECHEC_DEFINITIF")]
    if digit == "3":
        return False, []
    if digit == "4":
        return True, [delivered, delivered]
    if digit == "5":
        return True, [PlannedReport("DELIVRE", bad_signature=True)]
    if digit == "6":
        return (False, []) if attempt == 1 else (True, [delivered])
    if digit == "7":
        return True, []
    return True, [delivered]


def build_report(operator: str, message_id: str, status: str, bad_signature: bool = False) -> tuple[bytes, str]:
    """Signed report body and signature, as an operator would send it."""
    body = json.dumps(
        {"message_id": message_id, "status": status, "timestamp": clock.now().isoformat()}
    ).encode()
    signature = sign(operator, body)
    if bad_signature:
        signature = "0" * len(signature)
    return body, signature


def post_report(operator: str, message_id: str, status: str, bad_signature: bool = False) -> requests.Response | None:
    body, signature = build_report(operator, message_id, status, bad_signature)
    url = f"{settings.GATEWAY_BASE_URL}/api/operators/{operator}/reports"
    try:
        response = requests.post(
            url, data=body, headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature}, timeout=5
        )
    except requests.RequestException as exc:
        logger.warning("report %s %s -> %s failed: %s", operator, message_id, status, exc)
        return None
    logger.info("report %s %s -> %s answered %s", operator, message_id, status, response.status_code)
    return response


class SimulatedOperator:
    def send(self, sms: OutgoingSms) -> SendResult:
        accepted, reports = plan(sms.to, sms.attempt)
        if not accepted:
            return SendResult(accepted=False, error="Opérateur indisponible (simulé).")

        message_id = f"{sms.operator}-{uuid.uuid4().hex[:12]}"
        delay = settings.SIMULATOR_REPORT_DELAY_SECONDS
        for index, report in enumerate(reports, start=1):
            timer = threading.Timer(
                delay * index, post_report, args=(sms.operator, message_id, report.status, report.bad_signature)
            )
            timer.daemon = True
            timer.start()
        return SendResult(accepted=True, message_id=message_id)
