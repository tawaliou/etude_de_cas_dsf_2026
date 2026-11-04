from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from gateway import dispatch, simulator
from gateway.models import Sms, SmsEvent, SmsStatus
from gateway.operators import SIGNATURE_HEADER, SendResult, sign, verify_signature

from .test_submit_sms import ETATCIVIL_KEY, FIXED_NOW, sms_payload


class FakeOperator:
    """Simulated operator that keeps its reports instead of sending them over HTTP."""

    def __init__(self):
        self.pending_reports = []  # (operator, message_id, PlannedReport)
        self.sent = []

    def send(self, sms):
        self.sent.append(sms)
        accepted, reports = simulator.plan(sms.to, sms.attempt)
        if not accepted:
            return SendResult(accepted=False, error="Opérateur indisponible (simulé).")
        message_id = f"{sms.operator}-{sms.sms_id[:8]}-{sms.attempt}"
        self.pending_reports += [(sms.operator, message_id, report) for report in reports]
        return SendResult(accepted=True, message_id=message_id)


class SimulatorPlanTests(SimpleTestCase):
    def test_scenarios_by_last_digit(self):
        def statuses(to, attempt):
            accepted, reports = simulator.plan(to, attempt)
            return accepted, [(r.status, r.bad_signature) for r in reports]

        ok = ("DELIVRE", False)
        self.assertEqual(statuses("0197000000", 1), (True, [ok]))
        self.assertEqual(statuses("0197000001", 1), (True, [("ECHEC_TEMPORAIRE", False)]))
        self.assertEqual(statuses("0197000001", 2), (True, [ok]))
        self.assertEqual(statuses("0197000002", 1), (True, [("ECHEC_DEFINITIF", False)]))
        self.assertEqual(statuses("0197000003", 4), (False, []))
        self.assertEqual(statuses("0197000004", 1), (True, [ok, ok]))
        self.assertEqual(statuses("0197000005", 1), (True, [("DELIVRE", True)]))
        self.assertEqual(statuses("0197000006", 1), (False, []))
        self.assertEqual(statuses("0197000006", 2), (True, [ok]))
        self.assertEqual(statuses("0197000007", 1), (True, []))

    def test_built_reports_are_signed_unless_forged(self):
        body, signature = simulator.build_report("MTN", "MTN-1", "DELIVRE")
        self.assertTrue(verify_signature("MTN", body, signature))
        body, signature = simulator.build_report("MTN", "MTN-1", "DELIVRE", bad_signature=True)
        self.assertFalse(verify_signature("MTN", body, signature))

    def test_signature_is_bound_to_operator_and_body(self):
        body = b'{"message_id":"x","status":"DELIVRE"}'
        signature = sign("MOOV", body)
        self.assertFalse(verify_signature("MTN", body, signature))
        self.assertFalse(verify_signature("MOOV", body + b" ", signature))
        self.assertFalse(verify_signature("MOOV", body, None))
        self.assertFalse(verify_signature("INCONNU", body, signature))


class GatewayTestCase(TestCase):
    """Runs the gateway with a frozen, manually advanced clock and a fake operator."""

    def setUp(self):
        self.now = FIXED_NOW
        clock_patch = mock.patch("gateway.clock.now", side_effect=lambda: self.now)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.operator = FakeOperator()
        operator_patch = mock.patch("gateway.dispatch.get_operator_client", return_value=self.operator)
        operator_patch.start()
        self.addCleanup(operator_patch.stop)
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY=ETATCIVIL_KEY)

    def submit(self, **overrides) -> Sms:
        response = self.client.post("/api/sms", sms_payload(**overrides), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        return Sms.objects.get(pk=response.json()["id"])

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def post_report(self, operator, message_id, status, bad_signature=False, signature=None):
        body, good_signature = simulator.build_report(operator, message_id, status, bad_signature)
        headers = {} if signature == "" else {f"HTTP_{SIGNATURE_HEADER.upper().replace('-', '_')}": signature or good_signature}
        return APIClient().post(
            f"/api/operators/{operator}/reports", data=body, content_type="application/json", **headers
        )

    def deliver_pending_reports(self):
        reports, self.operator.pending_reports = self.operator.pending_reports, []
        return [self.post_report(op, mid, r.status, r.bad_signature) for op, mid, r in reports]

    def run_until_settled(self, steps=10):
        for _ in range(steps):
            dispatch.dispatch_due()
            self.deliver_pending_reports()
            self.advance(30)

    def kinds(self, sms):
        return list(SmsEvent.objects.filter(sms=sms).values_list("kind", flat=True))


class DispatchTests(GatewayTestCase):
    def test_accepted_sms_becomes_transmitted(self):
        sms = self.submit()
        self.assertEqual(dispatch.dispatch_due(), 1)
        sms.refresh_from_db()
        self.assertEqual(sms.status, SmsStatus.TRANSMIS)
        self.assertEqual(sms.attempts, 1)
        self.assertTrue(sms.operator_message_id)
        self.assertEqual(self.kinds(sms), [SmsEvent.Kind.TRANSMISSION])

    def test_otp_are_transmitted_before_pending_info(self):
        self.submit(reference="info-1", category="INFO")
        self.submit(reference="info-2", category="INFO")
        self.submit(reference="otp", category="OTP", to="0197000010")
        self.assertEqual(dispatch.due_sms()[0].reference, "otp")
        dispatch.dispatch_due()
        self.assertEqual(self.operator.sent[0].to, "0197000010")

    def test_scheduled_sms_waits_for_its_time(self):
        sms = self.submit(send_at=(FIXED_NOW + timedelta(minutes=10)).isoformat())
        self.assertEqual(dispatch.dispatch_due(), 0)
        self.advance(10 * 60)
        self.assertEqual(dispatch.dispatch_due(), 1)
        sms.refresh_from_db()
        self.assertEqual(sms.status, SmsStatus.TRANSMIS)

    def test_cancelled_sms_is_never_transmitted(self):
        sms = self.submit()
        self.client.post(f"/api/sms/{sms.pk}/cancel")
        self.assertEqual(dispatch.dispatch_due(), 0)
        self.assertEqual(self.operator.sent, [])

    def test_transmitted_sms_can_no_longer_be_cancelled(self):
        sms = self.submit()
        dispatch.dispatch_due()
        self.assertEqual(self.client.post(f"/api/sms/{sms.pk}/cancel").status_code, 409)

    def test_unavailable_operator_retries_with_increasing_delays_then_fails(self):
        sms = self.submit(to="0197000003")
        expected_delays = [2, 4, 8]  # settings.RETRY_DELAYS_SECONDS
        for attempt, delay in enumerate(expected_delays, start=1):
            dispatch.dispatch_due()
            sms.refresh_from_db()
            self.assertEqual((sms.status, sms.attempts), (SmsStatus.EN_ATTENTE, attempt))
            self.assertEqual(sms.next_attempt_at, self.now + timedelta(seconds=delay))
            self.advance(delay - 1)
            self.assertEqual(dispatch.dispatch_due(), 0, "retry must wait for its delay")
            self.advance(1)
        dispatch.dispatch_due()
        sms.refresh_from_db()
        self.assertEqual((sms.status, sms.attempts), (SmsStatus.ECHOUE, 4))
        self.assertEqual(len(self.operator.sent), 4)

    def test_crashing_operator_client_is_handled_as_unavailable(self):
        sms = self.submit()
        with mock.patch.object(self.operator, "send", side_effect=RuntimeError("boom")), \
                self.assertLogs("gateway.dispatch", level="ERROR"):
            self.assertEqual(dispatch.dispatch_due(), 1)
        sms.refresh_from_db()
        self.assertEqual(sms.status, SmsStatus.EN_ATTENTE)
        self.assertIn(SmsEvent.Kind.OPERATEUR_INDISPONIBLE, self.kinds(sms))

    def test_otp_text_reaches_operator_but_not_logs(self):
        self.submit(category="OTP", text="Code 135790")
        with self.assertLogs("gateway", level="INFO") as logs:
            dispatch.dispatch_due()
        self.assertEqual(self.operator.sent[0].text, "Code 135790")
        self.assertNotIn("135790", "\n".join(logs.output))
        self.assertNotIn("135790", repr(self.operator.sent[0]))


class ReportTests(GatewayTestCase):
    def transmitted(self, **overrides) -> Sms:
        sms = self.submit(**overrides)
        dispatch.dispatch_due()
        sms.refresh_from_db()
        return sms

    def test_delivered_report(self):
        sms = self.transmitted()
        response = self.post_report(sms.operator, sms.operator_message_id, "DELIVRE")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["applied"], True)
        sms.refresh_from_db()
        self.assertEqual(sms.status, SmsStatus.DELIVRE)

    def test_definitive_failure_report_has_no_retry(self):
        sms = self.transmitted()
        self.post_report(sms.operator, sms.operator_message_id, "ECHEC_DEFINITIF")
        sms.refresh_from_db()
        self.assertEqual(sms.status, SmsStatus.ECHOUE)
        self.assertEqual(dispatch.dispatch_due(), 0)

    def test_temporary_failure_report_schedules_retry(self):
        sms = self.transmitted()
        self.post_report(sms.operator, sms.operator_message_id, "ECHEC_TEMPORAIRE")
        sms.refresh_from_db()
        self.assertEqual(sms.status, SmsStatus.EN_ATTENTE)
        self.assertEqual(sms.next_attempt_at, self.now + timedelta(seconds=2))
        self.assertEqual(
            self.kinds(sms)[-2:], [SmsEvent.Kind.COMPTE_RENDU, SmsEvent.Kind.NOUVELLE_TENTATIVE]
        )

    def test_duplicate_report_is_ignored(self):
        sms = self.transmitted()
        self.post_report(sms.operator, sms.operator_message_id, "DELIVRE")
        response = self.post_report(sms.operator, sms.operator_message_id, "ECHEC_DEFINITIF")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["applied"], False)
        sms.refresh_from_db()
        self.assertEqual(sms.status, SmsStatus.DELIVRE)
        self.assertEqual(self.kinds(sms)[-1], SmsEvent.Kind.COMPTE_RENDU_IGNORE)

    def test_bad_or_missing_signature_is_rejected_without_effect(self):
        sms = self.transmitted()
        for kwargs in [{"bad_signature": True}, {"signature": ""}]:
            with self.subTest(**kwargs):
                response = self.post_report(sms.operator, sms.operator_message_id, "DELIVRE", **kwargs)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "invalid_signature")
                sms.refresh_from_db()
                self.assertEqual(sms.status, SmsStatus.TRANSMIS)
        self.assertIn(SmsEvent.Kind.COMPTE_RENDU_REJETE, self.kinds(sms))

    def test_report_signed_by_another_operator_is_rejected(self):
        sms = self.transmitted()  # MOOV
        body, _ = simulator.build_report("MOOV", sms.operator_message_id, "DELIVRE")
        response = APIClient().post(
            "/api/operators/MOOV/reports", data=body, content_type="application/json",
            HTTP_X_SIGNATURE=sign("MTN", body),
        )
        self.assertEqual(response.status_code, 401)

    def test_report_from_previous_attempt_is_unknown(self):
        sms = self.transmitted()
        old_id = sms.operator_message_id
        self.post_report(sms.operator, old_id, "ECHEC_TEMPORAIRE")
        self.advance(2)
        dispatch.dispatch_due()
        self.assertEqual(self.post_report(sms.operator, old_id, "DELIVRE").status_code, 404)

    def test_unknown_operator_message_and_bad_body(self):
        self.assertEqual(self.post_report("MOOV", "MOOV-nope", "DELIVRE").status_code, 404)
        self.assertEqual(
            APIClient().post("/api/operators/ORANGE/reports", data=b"{}", content_type="application/json").status_code,
            404,
        )
        body = b'{"message_id": "x", "status": "PERDU"}'
        response = APIClient().post(
            "/api/operators/MOOV/reports", data=body, content_type="application/json",
            HTTP_X_SIGNATURE=sign("MOOV", body),
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.json()["error"]["details"])

    def test_history_is_exposed_on_detail(self):
        sms = self.transmitted()
        self.post_report(sms.operator, sms.operator_message_id, "DELIVRE")
        history = self.client.get(f"/api/sms/{sms.pk}").json()["history"]
        self.assertEqual([e["kind"] for e in history], ["TRANSMISSION", "COMPTE_RENDU"])


class SimulatorScenarioTests(GatewayTestCase):
    """The 8 simulator scenarios, end to end (worker + signed reports over the API)."""

    def test_all_scenarios(self):
        expected = {
            "0": (SmsStatus.DELIVRE, 1),
            "1": (SmsStatus.DELIVRE, 2),
            "2": (SmsStatus.ECHOUE, 1),
            "3": (SmsStatus.ECHOUE, 4),
            "4": (SmsStatus.DELIVRE, 1),
            "5": (SmsStatus.TRANSMIS, 1),
            "6": (SmsStatus.DELIVRE, 2),
            "7": (SmsStatus.TRANSMIS, 1),
        }
        sms_by_digit = {d: self.submit(to=f"019700000{d}", reference=f"scenario-{d}") for d in expected}
        self.run_until_settled()
        for digit, (status, attempts) in expected.items():
            with self.subTest(digit=digit):
                sms = sms_by_digit[digit]
                sms.refresh_from_db()
                self.assertEqual((sms.status, sms.attempts), (status, attempts))
        self.assertIn(SmsEvent.Kind.COMPTE_RENDU_IGNORE, self.kinds(sms_by_digit["4"]))
        self.assertIn(SmsEvent.Kind.COMPTE_RENDU_REJETE, self.kinds(sms_by_digit["5"]))
