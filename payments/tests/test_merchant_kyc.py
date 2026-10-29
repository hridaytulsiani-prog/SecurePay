from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from payments.models import MerchantInfo, MerchantProviderProfile


@override_settings(DEBUG=True, PAYMENT_PROVIDER="mock_omniware")
class MerchantKycTests(TestCase):
    def setUp(self):
        self.merchant = MerchantInfo.objects.create(
            merchant_key="merchant-kyc-key",
            merchant_salt="merchant-kyc-salt",
            merchant_name="Acme Store",
            merchant_email="owner@example.com",
            merchant_phone="9876543210",
            merchant_address="Mumbai, Maharashtra",
            password="x" * 128,
        )
        self.raw_token = "kyc-session-token"
        cache.set(f"merchant_{self.merchant.id}:{self.raw_token}", True, 600)
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.merchant.id}-{self.raw_token}"}
        self.url = reverse("merchant_kyc")

    def _bank_payload(self):
        return {
            "vendor_name": "Acme Traders",
            "vendor_contact_email": "accounts@acme.test",
            "vendor_contact_num": "9876543210",
            "vendor_contact_address": "Mumbai",
            "vendor_pan": "ABCDE1234F",
            "settlement_method": "bank",
            "account_name": "Acme Traders",
            "account_number": "123456789012",
            "ifsc_code": "HDFC0001234",
            "bank_name": "HDFC Bank",
            "bank_branch": "Mumbai",
            "activate_bharat_qr": True,
            "aadhar_number": "123412341234",
        }

    def test_get_returns_merchant_defaults(self):
        response = self.client.get(self.url, **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "NOT_SUBMITTED")
        self.assertEqual(response.json()["defaults"]["vendor_name"], "Acme Store")

    def test_submit_masks_sensitive_values_and_mock_approve(self):
        response = self.client.post(self.url, self._bank_payload(), content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["status"], "PENDING")
        self.assertEqual(body["details"]["account_number_masked"], "********9012")
        self.assertEqual(body["details"]["vendor_pan_masked"], "******234F")

        profile = MerchantProviderProfile.objects.get(merchant=self.merchant)
        stored = str(profile.provider_response)
        self.assertNotIn("123456789012", stored)
        self.assertNotIn("123412341234", stored)
        self.assertNotIn("ABCDE1234F", stored)

        approved = self.client.post(self.url, {"action": "mock_approve"}, content_type="application/json", **self.headers)
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["status"], "APPROVED")

    def test_rejected_submission_can_be_resubmitted_with_upi(self):
        self.client.post(self.url, self._bank_payload(), content_type="application/json", **self.headers)
        rejected = self.client.post(self.url, {"action": "mock_reject"}, content_type="application/json", **self.headers)
        self.assertEqual(rejected.json()["status"], "REJECTED")
        payload = {
            "vendor_name": "Acme Traders",
            "vendor_contact_email": "accounts@acme.test",
            "vendor_contact_num": "9876543210",
            "vendor_contact_address": "Mumbai",
            "vendor_pan": "ABCDE1234F",
            "settlement_method": "upi",
            "upi_id": "acmetraders@hdfcbank",
            "activate_bharat_qr": False,
        }
        response = self.client.post(self.url, payload, content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "PENDING")
        self.assertEqual(response.json()["details"]["upi_id_masked"], "ac*********@hdfcbank")

    def test_approved_submission_can_be_modified(self):
        self.client.post(self.url, self._bank_payload(), content_type="application/json", **self.headers)
        self.client.post(self.url, {"action": "mock_approve"}, content_type="application/json", **self.headers)

        payload = self._bank_payload()
        payload.update({
            "action": "modify",
            "account_number": "987654321098",
            "bank_branch": "Bengaluru",
        })
        response = self.client.post(self.url, payload, content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "PENDING")
        self.assertEqual(response.json()["details"]["account_number_masked"], "********1098")
        self.assertEqual(response.json()["details"]["bank_branch"], "Bengaluru")

    def test_invalid_pan_is_rejected(self):
        payload = self._bank_payload()
        payload["vendor_pan"] = "NOT-A-PAN"
        response = self.client.post(self.url, payload, content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("PAN", response.json()["error"])

    def test_pan_and_registered_address_are_required(self):
        payload = self._bank_payload()
        payload["vendor_pan"] = ""
        response = self.client.post(self.url, payload, content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("PAN", response.json()["error"])

        payload = self._bank_payload()
        payload["vendor_contact_address"] = ""
        response = self.client.post(self.url, payload, content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("address", response.json()["error"])
