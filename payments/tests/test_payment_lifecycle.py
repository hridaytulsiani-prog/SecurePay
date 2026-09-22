import hashlib
import hmac
import json
from decimal import Decimal

from django.test import TestCase, override_settings

from payments.models import CustomerInfo, MerchantInfo, OrderInfo, PaymentEvent, PaymentLedgerEntry, PaymentNotification
from payments.services.payment_lifecycle import process_payment_webhook
from payments.services.settlements import apply_settlement_status


@override_settings(PAYMENT_WEBHOOK_SECRET="test-secret", PAYMENT_WEBHOOK_ALLOW_UNSIGNED_TEST_EVENTS=False)
class PaymentLifecycleTests(TestCase):
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
            customer_name="Test Customer",
            customer_email="customer@example.com",
            customer_phone="9000000001",
            customer_address="Test address",
        )
        self.order = OrderInfo.objects.create(
            merchant=self.merchant,
            merchant_order_id="TEST-LIFECYCLE-1",
            order_amount="1899.00",
            order_currency="INR",
            order_status="pending",
            customer_info=customer,
            payment_provider="test_provider",
        )

    def _post(self, payload, event_id="event-1"):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        return process_payment_webhook("test_provider", raw, signature, event_id)

    def test_success_is_idempotent_and_creates_notifications_and_ledger(self):
        payload = {"eventId": "event-1", "merchantOrderId": "TEST-LIFECYCLE-1", "status": "SUCCESS", "paymentId": "PAY-1"}
        result = self._post(payload)
        self.assertFalse(result["duplicate"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_state, "FUNDS_HELD")
        self.assertEqual(self.order.order_status, "paid")
        self.assertEqual(PaymentLedgerEntry.objects.filter(order=self.order).count(), 2)
        self.assertEqual(PaymentNotification.objects.filter(order=self.order).count(), 4)

        duplicate = self._post(payload)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(PaymentEvent.objects.filter(order=self.order).count(), 1)
        self.assertEqual(PaymentLedgerEntry.objects.filter(order=self.order).count(), 2)

    def test_invalid_signature_is_rejected(self):
        raw = b'{"merchantOrderId":"TEST-LIFECYCLE-1","status":"SUCCESS"}'
        with self.assertRaises(PermissionError):
            process_payment_webhook("test_provider", raw, "bad", "event-bad")

    def test_settlement_moves_order_to_settled(self):
        self._post({"merchantOrderId": "TEST-LIFECYCLE-1", "status": "SUCCESS"})
        settlement = self.order.settlement_record
        apply_settlement_status(settlement, {"status": "SETTLED", "settlementId": "SET-1", "feeAmount": "10.00"})
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_state, "SETTLED")
        settlement.refresh_from_db()
        self.assertEqual(settlement.net_amount, Decimal("1889.00"))
