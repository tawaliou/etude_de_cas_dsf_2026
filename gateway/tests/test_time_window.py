from datetime import datetime, timedelta

from django.test import SimpleTestCase

from gateway import dispatch, domain
from gateway.domain import BENIN_TZ
from gateway.models import SmsEvent, SmsStatus

from .test_transmission import GatewayTestCase


def benin(day, hour, minute=0, second=0):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=BENIN_TZ)


class EarliestSendTimeTests(SimpleTestCase):
    def test_info_inside_window_leaves_immediately(self):
        for instant in [benin(4, 7), benin(4, 12), benin(4, 20, 59, 59)]:
            with self.subTest(instant=instant):
                self.assertEqual(domain.earliest_send_time("INFO", instant), instant)

    def test_info_before_7_is_deferred_to_7_same_day(self):
        self.assertEqual(domain.earliest_send_time("INFO", benin(4, 2)), benin(4, 7))
        self.assertEqual(domain.earliest_send_time("INFO", benin(4, 6, 59, 59)), benin(4, 7))

    def test_info_from_21_is_deferred_to_7_next_day(self):
        self.assertEqual(domain.earliest_send_time("INFO", benin(4, 21)), benin(5, 7))
        self.assertEqual(domain.earliest_send_time("INFO", benin(4, 23, 30)), benin(5, 7))

    def test_window_uses_benin_time_not_utc(self):
        from datetime import timezone

        # 06:30 UTC = 07:30 in Benin: inside the window.
        instant = datetime(2026, 11, 4, 6, 30, tzinfo=timezone.utc)
        self.assertEqual(domain.earliest_send_time("INFO", instant), instant)
        # 20:30 UTC = 21:30 in Benin: outside.
        self.assertEqual(
            domain.earliest_send_time("INFO", datetime(2026, 11, 4, 20, 30, tzinfo=timezone.utc)), benin(5, 7)
        )

    def test_otp_is_never_deferred(self):
        for instant in [benin(4, 2), benin(4, 23, 30)]:
            self.assertEqual(domain.earliest_send_time("OTP", instant), instant)


class TimeWindowSubmitTests(GatewayTestCase):
    def test_info_submitted_at_night_is_deferred_to_7(self):
        self.now = benin(4, 23, 30)
        sms = self.submit(category="INFO")
        self.assertEqual(sms.status, SmsStatus.PROGRAMME)
        self.assertEqual(sms.next_attempt_at, benin(5, 7))
        self.assertIsNone(sms.scheduled_at)
        self.assertEqual(self.kinds(sms), [SmsEvent.Kind.DIFFERE])

    def test_otp_submitted_at_night_leaves_immediately(self):
        self.now = benin(4, 23, 30)
        sms = self.submit(category="OTP")
        self.assertEqual(sms.status, SmsStatus.EN_ATTENTE)
        self.assertEqual(dispatch.dispatch_due(), 1)

    def test_info_scheduled_at_night_is_deferred_to_next_morning(self):
        sms = self.submit(category="INFO", send_at=benin(4, 22).isoformat())
        self.assertEqual(sms.status, SmsStatus.PROGRAMME)
        self.assertEqual(sms.scheduled_at, benin(4, 22))
        self.assertEqual(sms.next_attempt_at, benin(5, 7))

    def test_deferred_info_is_sent_at_7_not_before(self):
        self.now = benin(4, 23, 30)
        sms = self.submit(category="INFO")
        self.now = benin(5, 6, 59, 59)
        self.assertEqual(dispatch.dispatch_due(), 0)
        self.now = benin(5, 7)
        self.assertEqual(dispatch.dispatch_due(), 1)
        sms.refresh_from_db()
        self.assertEqual(sms.status, SmsStatus.TRANSMIS)

    def test_at_7_otp_pass_before_waiting_info(self):
        self.now = benin(4, 23, 30)
        self.submit(category="INFO", reference="info-1")
        self.submit(category="INFO", reference="info-2")
        self.now = benin(5, 7)
        self.submit(category="OTP", reference="otp", to="0197000010")
        dispatch.dispatch_due()
        self.assertEqual([s.to for s in self.operator.sent][0], "0197000010")
        self.assertEqual(len(self.operator.sent), 3)

    def test_info_retry_crossing_21_is_deferred_by_worker(self):
        self.now = benin(4, 20, 59, 55)
        sms = self.submit(category="INFO", to="0197000003")  # operator always unavailable
        dispatch.dispatch_due()  # attempt 1 fails, retry at 20:59:57
        self.now += timedelta(seconds=5)  # 21:00:00
        self.assertEqual(dispatch.dispatch_due(), 0)
        sms.refresh_from_db()
        self.assertEqual(sms.status, SmsStatus.EN_ATTENTE)
        self.assertEqual(sms.attempts, 1)
        self.assertEqual(sms.next_attempt_at, benin(5, 7))
        self.assertEqual(self.kinds(sms)[-1], SmsEvent.Kind.DIFFERE)

    def test_deferred_info_can_be_cancelled_and_refunds_quota(self):
        self.now = benin(4, 23, 30)
        sms = self.submit(category="INFO")
        response = self.client.post(f"/api/sms/{sms.pk}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/usage").json()["used"], 0)
