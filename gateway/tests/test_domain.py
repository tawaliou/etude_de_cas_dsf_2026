from datetime import datetime, timezone

from django.test import SimpleTestCase

from gateway import domain


class SegmentTests(SimpleTestCase):
    def test_ascii_text_uses_160_char_segments(self):
        self.assertEqual(domain.count_segments("a" * 160), 1)
        self.assertEqual(domain.count_segments("a" * 161), 2)
        self.assertEqual(domain.count_segments("a" * 960), 6)

    def test_non_ascii_text_uses_70_char_segments(self):
        self.assertEqual(domain.count_segments("é" * 70), 1)
        self.assertEqual(domain.count_segments("é" * 71), 2)

    def test_single_accent_switches_whole_text_to_70(self):
        self.assertEqual(domain.count_segments("a" * 100 + "é"), 2)

    def test_emoji_counts_as_non_ascii(self):
        self.assertEqual(domain.count_segments("Bonjour 👋"), 1)
        self.assertEqual(domain.count_segments("x" * 70 + "👋"), 2)

    def test_short_text_is_one_segment(self):
        self.assertEqual(domain.count_segments("ok"), 1)

    def test_check_segments_accepts_six_and_rejects_seven(self):
        self.assertEqual(domain.check_segments("a" * 960), 6)
        with self.assertRaises(domain.InvalidSms) as ctx:
            domain.check_segments("a" * 961)
        self.assertEqual(ctx.exception.field, "text")
        with self.assertRaises(domain.InvalidSms):
            domain.check_segments("é" * 421)


class OperatorTests(SimpleTestCase):
    def test_operator_ranges_boundaries(self):
        cases = {
            "0140000000": "CELTIIS",
            "0149999999": "CELTIIS",
            "0150000000": "MTN",
            "0169999999": "MTN",
            "0190000000": "MOOV",
            "0199999999": "MOOV",
        }
        for number, operator in cases.items():
            with self.subTest(number=number):
                self.assertEqual(domain.detect_operator(number), operator)

    def test_unassigned_prefixes_are_rejected(self):
        for number in ["0139999999", "0170000000", "0189999999", "0100000000"]:
            with self.subTest(number=number), self.assertRaises(domain.InvalidSms) as ctx:
                domain.detect_operator(number)
            self.assertIn("non attribué", ctx.exception.message)

    def test_malformed_numbers_are_rejected(self):
        for number in ["0297000000", "019700000", "01970000000", "01970000ab", "+22997000000", ""]:
            with self.subTest(number=number), self.assertRaises(domain.InvalidSms) as ctx:
                domain.detect_operator(number)
            self.assertEqual(ctx.exception.field, "to")


class ContentHashTests(SimpleTestCase):
    base = {"to": "0197000000", "text": "Bonjour", "category": "INFO", "send_at": None}

    def test_same_content_same_hash(self):
        self.assertEqual(domain.content_hash(**self.base), domain.content_hash(**self.base))

    def test_any_field_change_changes_hash(self):
        reference = domain.content_hash(**self.base)
        variants = [
            {"to": "0197000001"},
            {"text": "Bonsoir"},
            {"category": "OTP"},
            {"send_at": datetime(2026, 11, 5, 8, tzinfo=timezone.utc)},
        ]
        for change in variants:
            with self.subTest(change=change):
                self.assertNotEqual(domain.content_hash(**{**self.base, **change}), reference)
