from datetime import datetime, timedelta
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from gateway import clock
from gateway.models import Sms, SmsStatus

ETATCIVIL_KEY = "etatcivil-test-key"
COURRIER_KEY = "courrier-test-key"

# Wednesday 2026-11-04 10:00 in Benin.
FIXED_NOW = datetime(2026, 11, 4, 10, 0, tzinfo=clock.BENIN_TZ)


def sms_payload(**overrides):
    payload = {"to": "0197000000", "text": "Bonjour", "category": "INFO", "reference": "ref-1"}
    payload.update(overrides)
    return payload


@mock.patch("gateway.clock.now", return_value=FIXED_NOW)
class SubmitSmsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY=ETATCIVIL_KEY)

    def post(self, **overrides):
        return self.client.post("/api/sms", sms_payload(**overrides), format="json")

    def test_valid_sms_is_accepted(self, _now):
        response = self.post()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["operator"], "MOOV")
        self.assertEqual(body["segments"], 1)
        self.assertEqual(body["status"], SmsStatus.EN_ATTENTE)
        self.assertEqual(body["reference"], "ref-1")
        self.assertIn("id", body)
        sms = Sms.objects.get(pk=body["id"])
        self.assertEqual(sms.app_code, "ETATCIVIL")
        self.assertEqual(sms.quota_day, FIXED_NOW.date())

    def test_future_send_at_is_scheduled(self, _now):
        send_at = FIXED_NOW + timedelta(hours=2)
        response = self.post(send_at=send_at.isoformat())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], SmsStatus.PROGRAMME)
        self.assertIsNotNone(response.json()["scheduled_at"])

    def test_past_send_at_is_sent_immediately(self, _now):
        response = self.post(send_at=(FIXED_NOW - timedelta(hours=1)).isoformat())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], SmsStatus.EN_ATTENTE)
        self.assertIsNone(response.json()["scheduled_at"])

    def test_otp_text_is_masked_in_response(self, _now):
        response = self.post(category="OTP", text="Votre code : 123456")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["text"], "****")
        self.assertNotIn("123456", response.content.decode())

    def test_otp_text_is_not_logged(self, _now):
        with self.assertLogs("gateway", level="INFO") as logs:
            self.post(category="OTP", text="Votre code : 987654")
        self.assertNotIn("987654", "\n".join(logs.output))

    def test_info_text_is_returned(self, _now):
        self.assertEqual(self.post(text="Rappel RDV").json()["text"], "Rappel RDV")

    # --- validation -------------------------------------------------------

    def test_invalid_numbers_are_rejected_and_not_stored(self, _now):
        for number in ["0297000000", "019700000", "0175000000"]:
            with self.subTest(number=number):
                response = self.post(to=number, reference=f"ref-{number}")
                self.assertEqual(response.status_code, 400)
                error = response.json()["error"]
                self.assertEqual(error["code"], "validation_error")
                self.assertIn("to", error["details"])
        self.assertEqual(Sms.objects.count(), 0)

    def test_text_over_six_segments_is_rejected(self, _now):
        response = self.post(text="a" * 961)
        self.assertEqual(response.status_code, 400)
        self.assertIn("text", response.json()["error"]["details"])
        self.assertEqual(Sms.objects.count(), 0)

    def test_missing_fields_and_bad_category_are_rejected(self, _now):
        response = self.client.post("/api/sms", {"category": "PROMO"}, format="json")
        self.assertEqual(response.status_code, 400)
        details = response.json()["error"]["details"]
        for field in ["to", "text", "reference", "category"]:
            self.assertIn(field, details)

    def test_request_without_api_key_is_rejected(self, _now):
        response = APIClient().post("/api/sms", sms_payload(), format="json")
        self.assertEqual(response.status_code, 401)

    # --- idempotency ------------------------------------------------------

    def test_same_reference_same_content_returns_existing_sms(self, _now):
        first = self.post()
        second = self.post()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(Sms.objects.count(), 1)

    def test_same_reference_different_content_is_refused(self, _now):
        self.post()
        response = self.post(text="Autre texte")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "reference_conflict")
        self.assertEqual(Sms.objects.count(), 1)

    def test_same_reference_in_two_apps_is_independent(self, _now):
        self.post()
        other = APIClient()
        other.credentials(HTTP_X_API_KEY=COURRIER_KEY)
        response = other.post("/api/sms", sms_payload(text="Autre texte"), format="json")
        self.assertEqual(response.status_code, 201)

    # --- quota ------------------------------------------------------------

    def test_quota_refuses_sms_that_would_exceed_it(self, _now):
        self.client.credentials(HTTP_X_API_KEY=COURRIER_KEY)  # quota: 5 segments
        self.assertEqual(self.post(reference="a", text="x" * 640).status_code, 201)  # 4 segments
        response = self.post(reference="b", text="x" * 161)  # 2 segments -> 6 > 5
        self.assertEqual(response.status_code, 429)
        error = response.json()["error"]
        self.assertEqual(error["code"], "quota_exceeded")
        self.assertEqual(error["details"], {"used": 4, "quota": 5, "requested": 2})
        self.assertEqual(self.post(reference="c", text="x").status_code, 201)  # 1 segment -> 5

    def test_cancelled_sms_do_not_count_in_quota(self, _now):
        self.client.credentials(HTTP_X_API_KEY=COURRIER_KEY)
        first = self.post(reference="a", text="x" * 800)  # 5 segments
        Sms.objects.filter(pk=first.json()["id"]).update(status=SmsStatus.ANNULE)
        self.assertEqual(self.post(reference="b", text="x" * 800).status_code, 201)

    def test_quota_resets_on_next_benin_day(self, _now):
        self.client.credentials(HTTP_X_API_KEY=COURRIER_KEY)
        self.assertEqual(self.post(reference="a", text="x" * 800).status_code, 201)
        self.assertEqual(self.post(reference="b").status_code, 429)
        _now.return_value = datetime(2026, 11, 5, 0, 0, 1, tzinfo=clock.BENIN_TZ)
        self.assertEqual(self.post(reference="b").status_code, 201)


class ClockTests(TestCase):
    def test_benin_day_changes_at_local_midnight(self):
        # 23:30 UTC on Nov 4 is already 00:30 on Nov 5 in Benin (GMT+1).
        from datetime import timezone as dt_tz

        instant = datetime(2026, 11, 4, 23, 30, tzinfo=dt_tz.utc)
        self.assertEqual(str(clock.local_date(instant)), "2026-11-05")
