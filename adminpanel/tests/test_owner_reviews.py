from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from adminpanel.api.v1.owner_review_views import (
    OwnerDecisionReviewActionView,
    OwnerDecisionReviewListCreateView,
)
from adminpanel.models import AdminUserRole, HighValueDecisionReview
from payments.models import CustomerInfo, MerchantInfo, OrderInfo, PAProtectedOrder


User = get_user_model()


class OwnerDecisionReviewWorkflowTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user("manager", password="password", is_staff=True)
        AdminUserRole.objects.create(user=self.manager, role=AdminUserRole.ROLE_OPERATIONS)
        self.owner = User.objects.create_user("owner", password="password", is_staff=True, is_superuser=True)
        AdminUserRole.objects.create(user=self.owner, role=AdminUserRole.ROLE_OWNER)
        merchant = MerchantInfo.objects.create(
            merchant_key="merchant-key",
            merchant_salt="merchant-salt",
            merchant_name="Test Merchant",
            merchant_email="merchant@example.com",
            merchant_phone="9000000000",
            merchant_address="Kolkata",
            password="hashed",
        )
        customer = CustomerInfo.objects.create(
            customer_name="Customer",
            customer_email="customer@example.com",
            customer_phone="9111111111",
            customer_address="Kolkata",
        )
        order = OrderInfo.objects.create(
            merchant=merchant,
            merchant_order_id="ORD-HIGH-1",
            order_amount="5000.00",
            order_currency="INR",
            order_status="HELD",
            customer_info=customer,
        )
        self.protected = PAProtectedOrder.objects.create(
            vaultpay_order_id="VP-HIGH-1",
            order=order,
            merchant=merchant,
            merchant_order_id=order.merchant_order_id,
            pa_provider="omniware",
            amount_minor=500000,
            currency="INR",
        )

    def create_review(self, decision="REFUND"):
        request = SimpleNamespace(
            admin_user=self.manager,
            data={
                "vaultpay_order_id": self.protected.vaultpay_order_id,
                "decision": decision,
                "owner_id": self.owner.id,
            },
        )
        return OwnerDecisionReviewListCreateView().post(request)

    def test_owner_approval_starts_ten_minute_window(self):
        created = self.create_review("REFUND")
        self.assertEqual(created.status_code, 201)
        review = HighValueDecisionReview.objects.get()
        self.assertEqual(review.status, HighValueDecisionReview.STATUS_PENDING)

        request = SimpleNamespace(admin_user=self.owner, data={"action": "APPROVE", "note": "Evidence checked."})
        with patch("payments.services.pa_control.threading.Timer"):
            response = OwnerDecisionReviewActionView().post(request, review.id)

        self.assertEqual(response.status_code, 200)
        review.refresh_from_db()
        self.protected.refresh_from_db()
        self.assertEqual(review.status, HighValueDecisionReview.STATUS_APPROVED)
        self.assertEqual(self.protected.metadata["decision_window"]["decision"], "REFUND")
        self.assertEqual(self.protected.metadata["decision_window"]["status"], "PENDING")

    def test_owner_rejection_does_not_stage_request(self):
        self.create_review("RELEASE")
        review = HighValueDecisionReview.objects.get()
        request = SimpleNamespace(admin_user=self.owner, data={"action": "REJECT", "note": "Check delivery proof."})

        response = OwnerDecisionReviewActionView().post(request, review.id)

        self.assertEqual(response.status_code, 200)
        review.refresh_from_db()
        self.protected.refresh_from_db()
        self.assertEqual(review.status, HighValueDecisionReview.STATUS_REJECTED)
        self.assertNotIn("decision_window", self.protected.metadata)

    def test_second_pending_review_is_blocked(self):
        self.assertEqual(self.create_review().status_code, 201)
        response = self.create_review("RELEASE")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(HighValueDecisionReview.objects.count(), 1)
