import json

from django.test import TestCase
from django.urls import reverse

from payments.models import CheckoutSession, MerchantInfo, OrderInfo, PaymentAttempt


class CheckoutTestModeTests(TestCase):
    def setUp(self):
        self.merchant = MerchantInfo.objects.create(
            merchant_key="merchant-test-key",
            merchant_salt="merchant-test-salt",
            merchant_name="Custom Test Store",
            merchant_email="store@example.com",
            merchant_phone="9999999999",
            merchant_address="Mumbai",
            password="x" * 128,
        )
        self.payload = {
            "merchant_key": self.merchant.merchant_key,
            "test_mode": True,
            "customer": {
                "name": "Test Customer",
                "email": "customer@example.com",
                "phone": "8888888888",
                "address": "Pune",
            },
            "order": {
                "order_id": "TEST-ORDER-1001",
                "amount": "499.00",
                "currency": "INR",
            },
            "page": {"title": "Merchant test checkout"},
            "source_origin": "https://merchant.example",
            "source_url": "https://merchant.example/test-checkout",
        }

    def create_session(self, **overrides):
        payload = {**self.payload, **overrides}
        return self.client.post(
            reverse("checkout-session-create"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_test_session_never_creates_financial_records(self):
        response = self.create_session()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["test_mode"])
        self.assertEqual(OrderInfo.objects.count(), 0)
        self.assertEqual(PaymentAttempt.objects.count(), 0)

        session = CheckoutSession.objects.get(session_id=response.json()["session_id"])
        self.assertTrue(session.order_snapshot["_escrosafe_test_mode"])
        self.assertEqual(session.order_snapshot["_escrosafe_test_status"], "NOT_STARTED")

        detail = self.client.get(reverse("checkout-session-detail", args=[session.session_id]))
        self.assertTrue(detail.json()["test_mode"])
        self.assertEqual(detail.json()["test_status"], "NOT_STARTED")

        outcome = self.client.post(
            reverse("checkout-session-test-outcome", args=[session.session_id]),
            data=json.dumps({"outcome": "success"}),
            content_type="application/json",
        )
        self.assertEqual(outcome.status_code, 200)
        self.assertEqual(outcome.json()["status"], "SUCCESS")
        self.assertEqual(OrderInfo.objects.count(), 0)
        self.assertEqual(PaymentAttempt.objects.count(), 0)

        status = self.client.get(reverse("checkout-session-payment-status", args=[session.session_id]))
        self.assertTrue(status.json()["test_mode"])
        self.assertEqual(status.json()["status"], "SUCCESS")
        self.assertEqual(status.json()["return_url"], self.payload["source_url"])

    def test_live_session_behavior_remains_separate(self):
        response = self.create_session(test_mode=False)
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["test_mode"])
        self.assertEqual(OrderInfo.objects.count(), 1)

        session = CheckoutSession.objects.get(session_id=response.json()["session_id"])
        outcome = self.client.post(
            reverse("checkout-session-test-outcome", args=[session.session_id]),
            data=json.dumps({"outcome": "success"}),
            content_type="application/json",
        )
        self.assertEqual(outcome.status_code, 409)

