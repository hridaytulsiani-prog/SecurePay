from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from payments.adapters.omniware import generate_hash, verify_response_hash
from payments.api.v1.omniware import process_omniware_callback
from payments.models import CheckoutSession, CustomerInfo, MerchantInfo, OrderInfo, PaymentAttempt


@override_settings(
    DEBUG=True,
    PAYMENT_PROVIDER="mock_omniware",
    OMNIWARE_SALT="test-omniware-salt",
    SECUREPAY_CHECKOUT_BASE_URL="http://localhost:5173/checkout",
)
class MockOmniwareFlowTests(TestCase):
    def setUp(self):
        self.merchant = MerchantInfo.objects.create(
            merchant_key="merchant-key",
            merchant_salt="merchant-salt",
            merchant_name="Test Store",
            merchant_email="store@example.com",
            merchant_phone="9999999999",
            merchant_address="Mumbai",
            password="x" * 128,
        )
        customer = CustomerInfo.objects.create(
            customer_name="Test Customer",
            customer_email="customer@example.com",
            customer_phone="8888888888",
            customer_address="Pune",
        )
        self.order = OrderInfo.objects.create(
            merchant=self.merchant,
            merchant_order_id="ORDER-1001",
            order_amount=Decimal("499.00"),
            order_currency="INR",
            order_status="pending",
            payment_state="CREATED",
            customer_info=customer,
        )
        self.session = CheckoutSession.objects.create(
            merchant=self.merchant,
            merchant_key=self.merchant.merchant_key,
            customer_snapshot={
                "name": customer.customer_name,
                "email": customer.customer_email,
                "phone": customer.customer_phone,
                "address": customer.customer_address,
            },
            order_snapshot={"order_id": self.order.merchant_order_id, "amount": "499.00", "currency": "INR"},
            status=CheckoutSession.STATUS_CREATED,
        )

    def test_hash_rejects_tampering(self):
        payload = {"order_id": "ABC", "amount": "499.00"}
        payload["hash"] = generate_hash(payload, "test-omniware-salt")
        self.assertTrue(verify_response_hash(payload, "test-omniware-salt"))
        payload["amount"] = "1.00"
        self.assertFalse(verify_response_hash(payload, "test-omniware-salt"))
        payload.pop("hash")
        self.assertFalse(verify_response_hash(payload, "test-omniware-salt"))

    def test_mock_success_updates_attempt_and_order(self):
        start = self.client.post(reverse("checkout-session-payment", args=[self.session.session_id]))
        self.assertEqual(start.status_code, 200)
        self.assertTrue(start.json()["mock"])
        self.assertEqual(start.json()["payment_mode"], "UPI_QR")
        self.assertTrue(start.json()["mock_control_url"])
        attempt = PaymentAttempt.objects.get(id=start.json()["attempt_id"])

        result = self.client.post(
            reverse("mock-omniware-payment", args=[attempt.provider_uuid]),
            {"outcome": "success"},
        )
        self.assertEqual(result.status_code, 302)
        attempt.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.STATUS_SUCCESS)
        self.assertEqual(self.order.payment_state, "PAYMENT_CAPTURED")
        self.assertEqual(self.order.order_status, "paid")
        self.assertEqual(self.order.ledger_entries.count(), 1)

        status = self.client.get(reverse("checkout-session-payment-status", args=[self.session.session_id]))
        self.assertEqual(status.json()["status"], PaymentAttempt.STATUS_SUCCESS)

        second_start = self.client.post(reverse("checkout-session-payment", args=[self.session.session_id]))
        self.assertEqual(second_start.status_code, 409)

    def test_callback_rejects_amount_mismatch(self):
        start = self.client.post(reverse("checkout-session-payment", args=[self.session.session_id]))
        attempt = PaymentAttempt.objects.get(id=start.json()["attempt_id"])
        payload = {
            "order_id": attempt.provider_order_id,
            "transaction_id": "TXN1",
            "amount": "1.00",
            "currency": "INR",
            "response_code": "0",
        }
        payload["hash"] = generate_hash(payload, "test-omniware-salt")
        with self.assertRaisesMessage(ValueError, "amount does not match"):
            process_omniware_callback(payload)
