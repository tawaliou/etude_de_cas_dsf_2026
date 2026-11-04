import uuid
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from gateway import services
from gateway.models import Sms, SmsStatus

from .test_submit_sms import COURRIER_KEY, ETATCIVIL_KEY, FIXED_NOW, sms_payload


def client_for(key):
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=key)
    return client


@mock.patch("gateway.clock.now", return_value=FIXED_NOW)
class ConsultSmsTests(TestCase):
    def setUp(self):
        self.etatcivil = client_for(ETATCIVIL_KEY)
        self.courrier = client_for(COURRIER_KEY)

    def submit(self, client, **overrides):
        response = client.post("/api/sms", sms_payload(**overrides), format="json")
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()

    def test_get_own_sms(self, _now):
        sms = self.submit(self.etatcivil)
        response = self.etatcivil.get(f"/api/sms/{sms['id']}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual({key: body[key] for key in sms}, sms)
        self.assertEqual(body["history"], [])

    def test_other_app_sms_is_not_found(self, _now):
        sms = self.submit(self.etatcivil)
        response = self.courrier.get(f"/api/sms/{sms['id']}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "sms_not_found")

    def test_unknown_and_malformed_ids_are_not_found(self, _now):
        for sms_id in [uuid.uuid4(), "not-a-uuid"]:
            with self.subTest(sms_id=sms_id):
                self.assertEqual(self.etatcivil.get(f"/api/sms/{sms_id}").status_code, 404)

    def test_otp_is_masked_on_consultation(self, _now):
        sms = self.submit(self.etatcivil, category="OTP", text="Code 424242")
        detail = self.etatcivil.get(f"/api/sms/{sms['id']}")
        listing = self.etatcivil.get("/api/sms")
        for response in (detail, listing):
            self.assertNotIn("424242", response.content.decode())

    def test_list_only_returns_own_sms(self, _now):
        self.submit(self.etatcivil, reference="e1")
        self.submit(self.etatcivil, reference="e2")
        self.submit(self.courrier, reference="c1")
        body = self.etatcivil.get("/api/sms").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual({sms["reference"] for sms in body["results"]}, {"e1", "e2"})

    def test_list_filters(self, _now):
        self.submit(self.etatcivil, reference="info", category="INFO")
        self.submit(self.etatcivil, reference="otp", category="OTP")
        by_category = self.etatcivil.get("/api/sms", {"category": "OTP"}).json()
        self.assertEqual([s["reference"] for s in by_category["results"]], ["otp"])
        by_reference = self.etatcivil.get("/api/sms", {"reference": "info"}).json()
        self.assertEqual(by_reference["count"], 1)
        by_status = self.etatcivil.get("/api/sms", {"status": "DELIVRE"}).json()
        self.assertEqual(by_status["count"], 0)

    def test_invalid_filter_is_rejected(self, _now):
        response = self.etatcivil.get("/api/sms", {"status": "INCONNU"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.json()["error"]["details"])


@mock.patch("gateway.clock.now", return_value=FIXED_NOW)
class UsageTests(TestCase):
    def test_usage_reflects_segments_and_cancellations(self, _now):
        client = client_for(COURRIER_KEY)
        self.assertEqual(
            client.get("/api/usage").json(), {"date": "2026-11-04", "quota": 5, "used": 0, "remaining": 5}
        )
        sms = client.post("/api/sms", sms_payload(text="x" * 300), format="json").json()  # 2 segments
        self.assertEqual(client.get("/api/usage").json()["used"], 2)
        self.assertEqual(client.get("/api/usage").json()["remaining"], 3)
        client.post(f"/api/sms/{sms['id']}/cancel")
        self.assertEqual(client.get("/api/usage").json()["used"], 0)

    def test_usage_requires_api_key(self, _now):
        self.assertEqual(APIClient().get("/api/usage").status_code, 401)


@mock.patch("gateway.clock.now", return_value=FIXED_NOW)
class CancelSmsTests(TestCase):
    def setUp(self):
        self.client = client_for(ETATCIVIL_KEY)

    def submit(self, **overrides):
        return self.client.post("/api/sms", sms_payload(**overrides), format="json").json()

    def test_cancel_pending_sms(self, _now):
        sms = self.submit()
        response = self.client.post(f"/api/sms/{sms['id']}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], SmsStatus.ANNULE)

    def test_cancel_scheduled_sms(self, _now):
        sms = self.submit(send_at="2026-11-05T09:00:00+01:00")
        self.assertEqual(sms["status"], SmsStatus.PROGRAMME)
        self.assertEqual(self.client.post(f"/api/sms/{sms['id']}/cancel").status_code, 200)

    def test_cannot_cancel_transmitted_or_final_sms(self, _now):
        for index, current in enumerate(
            [SmsStatus.TRANSMIS, SmsStatus.DELIVRE, SmsStatus.ECHOUE, SmsStatus.ANNULE]
        ):
            with self.subTest(status=current):
                sms = self.submit(reference=f"r{index}")
                Sms.objects.filter(pk=sms["id"]).update(status=current)
                response = self.client.post(f"/api/sms/{sms['id']}/cancel")
                self.assertEqual(response.status_code, 409)
                error = response.json()["error"]
                self.assertEqual(error["code"], "not_cancellable")
                self.assertEqual(error["details"], {"status": current})
                self.assertEqual(Sms.objects.get(pk=sms["id"]).status, current)

    def test_cannot_cancel_other_app_sms(self, _now):
        sms = self.submit()
        response = client_for(COURRIER_KEY).post(f"/api/sms/{sms['id']}/cancel")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Sms.objects.get(pk=sms["id"]).status, SmsStatus.EN_ATTENTE)


class TransitionTests(TestCase):
    def make_sms(self, status):
        return Sms.objects.create(
            app_code="ETATCIVIL", reference=str(uuid.uuid4()), to="0197000000", operator="MOOV",
            category="INFO", text="t", content_hash="h", segments=1, status=status, quota_day=FIXED_NOW.date(),
        )

    def test_final_statuses_never_change(self):
        for final in [SmsStatus.DELIVRE, SmsStatus.ECHOUE, SmsStatus.ANNULE]:
            for target in SmsStatus.values:
                with self.subTest(final=final, target=target):
                    sms = self.make_sms(final)
                    self.assertFalse(services.transition(sms.pk, target))
                    sms.refresh_from_db()
                    self.assertEqual(sms.status, final)

    def test_normal_lifecycle(self):
        sms = self.make_sms(SmsStatus.EN_ATTENTE)
        self.assertTrue(services.transition(sms.pk, SmsStatus.TRANSMIS))
        self.assertFalse(services.transition(sms.pk, SmsStatus.ANNULE))
        self.assertTrue(services.transition(sms.pk, SmsStatus.DELIVRE))
        sms.refresh_from_db()
        self.assertEqual(sms.status, SmsStatus.DELIVRE)
