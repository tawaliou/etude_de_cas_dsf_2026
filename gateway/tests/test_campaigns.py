from gateway import dispatch
from gateway.models import Campaign, Sms, SmsStatus

from .test_submit_sms import COURRIER_KEY
from .test_time_window import benin
from .test_transmission import GatewayTestCase


def numbers(count, start=0):
    return [f"0197{index:06d}" for index in range(start, start + count)]


class CampaignTests(GatewayTestCase):
    def post_campaign(self, **overrides):
        payload = {"reference": "camp-1", "text": "Information importante", "recipients": numbers(3)}
        payload.update(overrides)
        return self.client.post("/api/campaigns", payload, format="json")

    def test_campaign_creates_one_info_sms_per_recipient(self):
        response = self.post_campaign()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body["recipients_count"], body["accepted_count"], body["refused_count"]), (3, 3, 0))
        self.assertEqual([r["to"] for r in body["results"]], numbers(3))
        self.assertTrue(all(r["accepted"] and r["status"] == SmsStatus.EN_ATTENTE for r in body["results"]))
        sms = Sms.objects.filter(campaign_id=body["id"])
        self.assertEqual(sms.count(), 3)
        self.assertEqual(set(sms.values_list("category", flat=True)), {"INFO"})

    def test_each_recipient_is_validated_separately(self):
        recipients = ["0197000000", "0297000000", "0175000000", "0197000000", "0150000000"]
        body = self.post_campaign(recipients=recipients).json()
        accepted = [r["accepted"] for r in body["results"]]
        self.assertEqual(accepted, [True, False, False, False, True])
        self.assertEqual(body["results"][3]["error"]["code"], "duplicate_recipient")
        self.assertIn("01", body["results"][1]["error"]["message"])
        self.assertEqual(Sms.objects.count(), 2)

    def test_quota_accepts_recipients_until_exhausted(self):
        self.client.credentials(HTTP_X_API_KEY=COURRIER_KEY)  # quota 5 segments
        self.submit(reference="before", text="x" * 161)  # 2 segments already used
        body = self.post_campaign(recipients=numbers(5)).json()  # 1 segment each
        self.assertEqual([r["accepted"] for r in body["results"]], [True, True, True, False, False])
        self.assertEqual(body["results"][3]["error"]["code"], "quota_exceeded")
        self.assertEqual(self.client.get("/api/usage").json()["used"], 5)

    def test_invalid_text_refuses_whole_campaign(self):
        response = self.post_campaign(text="a" * 961)
        self.assertEqual(response.status_code, 400)
        self.assertIn("text", response.json()["error"]["details"])
        self.assertFalse(Campaign.objects.exists())

    def test_recipient_list_limits(self):
        self.assertEqual(self.post_campaign(recipients=[]).status_code, 400)
        self.assertEqual(self.post_campaign(recipients=numbers(501)).status_code, 400)
        self.assertEqual(self.post_campaign(recipients=numbers(500)).status_code, 201)

    def test_same_reference_same_content_returns_existing_campaign(self):
        first = self.post_campaign()
        second = self.post_campaign()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(Sms.objects.count(), 3)

    def test_same_reference_other_content_is_refused(self):
        self.post_campaign()
        response = self.post_campaign(recipients=numbers(2))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "reference_conflict")

    def test_campaign_follows_info_time_window(self):
        self.now = benin(4, 22)
        body = self.post_campaign().json()
        self.assertTrue(all(r["status"] == SmsStatus.PROGRAMME for r in body["results"]))
        self.assertEqual(dispatch.dispatch_due(), 0)

    def test_otp_submitted_after_campaign_is_sent_first(self):
        self.post_campaign(recipients=numbers(5))
        self.submit(category="OTP", reference="otp", to="0150000000")
        dispatch.dispatch_due()
        self.assertEqual(self.operator.sent[0].to, "0150000000")

    def test_detail_shows_current_status_of_each_sms(self):
        recipients = ["0197000000", "0197000002", "0297000000"]  # delivered, definitive failure, invalid
        campaign_id = self.post_campaign(recipients=recipients).json()["id"]
        self.run_until_settled()
        body = self.client.get(f"/api/campaigns/{campaign_id}").json()
        self.assertEqual([r.get("status") for r in body["results"]], [SmsStatus.DELIVRE, SmsStatus.ECHOUE, None])
        self.assertEqual(body["status_summary"], {SmsStatus.DELIVRE: 1, SmsStatus.ECHOUE: 1})

    def test_campaign_sms_are_visible_and_cancellable_individually(self):
        body = self.post_campaign().json()
        sms_id = body["results"][0]["sms_id"]
        self.assertEqual(self.client.get(f"/api/sms/{sms_id}").status_code, 200)
        self.assertEqual(self.client.post(f"/api/sms/{sms_id}/cancel").status_code, 200)
        detail = self.client.get(f"/api/campaigns/{body['id']}").json()
        self.assertEqual(detail["results"][0]["status"], SmsStatus.ANNULE)

    def test_campaigns_are_isolated_per_app(self):
        campaign_id = self.post_campaign().json()["id"]
        self.assertEqual(self.client.get("/api/campaigns").json()["count"], 1)
        self.client.credentials(HTTP_X_API_KEY=COURRIER_KEY)
        self.assertEqual(self.client.get(f"/api/campaigns/{campaign_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/campaigns").json()["count"], 0)
        self.assertEqual(self.client.get("/api/campaigns/not-a-uuid").status_code, 404)
