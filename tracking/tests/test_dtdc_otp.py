from django.test import SimpleTestCase

from tracking.api.v1.dtdc_otp import extract_dtdc_tracking_summary, parse_dtdc_otp_status


class DtdcOtpStatusTests(SimpleTestCase):
    def test_rto_current_status_is_not_overridden_by_delivered_history(self):
        payload = {
            "table_rows": [
                ["Courier Status", "RTO", "Previous Status", "DELIVERED"],
            ],
            "page_text": "Courier Status: RTO Previous event DELIVERED",
        }

        summary = extract_dtdc_tracking_summary(payload)
        parsed = parse_dtdc_otp_status(payload)

        self.assertEqual(summary["courier_status"], "RTO")
        self.assertEqual(summary["delivery_status"], "RTO")
        self.assertEqual(parsed["delivery_status"], "RTO")

    def test_page_text_status_fallback_does_not_raise(self):
        payload = {
            "page_text": "Shipment status: IN TRANSIT",
        }

        summary = extract_dtdc_tracking_summary(payload)

        self.assertEqual(summary["delivery_status"], "IN TRANSIT")

    def test_script_html_status_is_not_treated_as_tracking_status(self):
        payload = {
            "html": "<script>var previousStatus = 'RTO';</script>",
            "page_text": "DTDC tracking home page",
        }

        summary = extract_dtdc_tracking_summary(payload)

        self.assertEqual(summary["delivery_status"], "NOT_DELIVERED_OR_UNKNOWN")
