import hashlib
import hmac
import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from payments.models import CustomerInfo, MerchantInfo, OrderInfo, PaymentLedgerEntry, RefundRequest
from payments.services.payment_lifecycle import transition_payment_state
from tracking.models import PdfValidationRecord


@override_settings(
    OMNIWARE_API_KEY="test-key",
    OMNIWARE_BASE_URL="https://omniware.test",
    OMNIWARE_SALT="test-salt",
    OMNIWARE_WEBHOOK_SECRET="refund-secret",
    PAYMENT_WEBHOOK_ALLOW_UNSIGNED_TEST_EVENTS=False,
)
class RefundFlowTests(TestCase):
    def setUp(self):
        self.merchant = MerchantInfo.objects.create(
            merchant_key="test-key",
            merchant_salt="test-salt",
            merchant_name="Test Merchant",
            merchant_email="merchant@example.com",
            merchant_phone="9000000000",
            merchant_address="Test address",
            password="test-password",
        )
        customer = CustomerInfo.objects.create(
            customer_name="Rahul",
            customer_email="rahul@example.com",
            customer_phone="9000000001",
            customer_address="Test address",
        )
        self.order = OrderInfo.objects.create(
            merchant=self.merchant,
            merchant_order_id="ORDER-1001",
            pa_order_id="ORDER-1001",
            pa_payment_id="PP-9001",
            order_amount="1899.00",
            order_currency="INR",
            order_status="paid",
            customer_info=customer,
            payment_provider="omniware",
        )
        transition_payment_state(self.order, "PAYMENT_CAPTURED")

    @patch("payments.adapters.omniware.requests.post")
    def test_common_refund_request_moves_to_pending_without_customer_bank_details(self, post):
        post.return_value.ok = True
        post.return_value.json.return_value = {"data": {"transaction_id": "PP-9001", "refund_id": 4351, "merchant_refund_id": "RF-1-ORDER-1001"}}
        post.return_value.text = '{"data":{"transaction_id":"PP-9001","refund_id":4351}}'

        PdfValidationRecord.objects.create(
            merchant=self.merchant,
            file_name="bad-label.pdf",
            status="not_approved",
            order_id="ORDER-1001",
            awb="WRONG-AWB",
        )

        from payments.services.refunds import create_common_refund

        refund = create_common_refund(self.order, "AWB mismatch")
        self.order.refresh_from_db()

        self.assertEqual(refund.status, RefundRequest.STATUS_PENDING)
        self.assertEqual(refund.provider_refund_id, "4351")
        self.assertEqual(self.order.payment_state, "REFUND_PENDING")
        self.assertEqual(self.order.order_status, "refund_pending")
        sent_payload = post.call_args.kwargs["data"]
        self.assertEqual(sent_payload["transaction_id"], "PP-9001")
        self.assertEqual(sent_payload["merchant_refund_id"], "RF-1-ORDER-1001")
        self.assertIn("hash", sent_payload)
        self.assertNotIn("bank", json.dumps(sent_payload).lower())
        self.assertNotIn("upi", json.dumps(sent_payload).lower())
        self.assertNotIn("card", json.dumps(sent_payload).lower())

    @patch("payments.adapters.omniware.requests.post")
    def test_refund_completed_webhook_marks_order_refunded(self, post):
        post.return_value.ok = True
        post.return_value.json.return_value = {"data": {"transaction_id": "PP-9001", "refund_id": 4351, "merchant_refund_id": "RF-1-ORDER-1001"}}
        post.return_value.text = '{"data":{"transaction_id":"PP-9001","refund_id":4351}}'

        from payments.services.refunds import create_common_refund, process_refund_webhook

        create_common_refund(self.order, "AWB mismatch")
        payload = {
            "event": {"id": "evt-1", "type": "refund.completed"},
            "originalResponse": {"id": "4351", "status": "Completed", "referenceId": "RF-1-ORDER-1001"},
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(b"refund-secret", raw, hashlib.sha256).hexdigest()

        result = process_refund_webhook("omniware", raw, signature)
        self.order.refresh_from_db()

        self.assertTrue(result["ok"])
        self.assertEqual(self.order.payment_state, "REFUNDED")
        self.assertEqual(self.order.order_status, "refunded")
        self.assertEqual(PaymentLedgerEntry.objects.filter(order=self.order, entry_type=PaymentLedgerEntry.ENTRY_REFUND).count(), 1)
