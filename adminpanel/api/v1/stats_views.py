# Powers the admin panel's Overview page (securepay-admin's OverviewPage.jsx
# calls GET /adminpanel/stats/ and renders this response's fields directly
# into its stat cards / breakdown lists — see that component for the exact
# field names it reads).
from django.contrib.auth import get_user_model
from django.db.models import Count, Sum

from rest_framework.views import Response

from adminpanel.models import ContactMessage, CustomerIssue, MerchantIssue
from adminpanel.permissions import AdminAPIView
from payments.models.orderinfo import OrderInfo
from payments.models.enquirydata import EnquiryData
from payments.models.merchantinfo import MerchantInfo
from tracking.models.pdf_validation import PdfValidationRecord

User = get_user_model()


class AdminStatsView(AdminAPIView):
    required_permission = "dashboard.view"
    """Headline numbers for the admin Overview page: totals plus a status
    breakdown for orders and enquiries, computed with DB aggregates
    (.count()/.aggregate()/.annotate()) rather than pulling every row into
    Python and counting in a loop — matters once there are enough orders
    that this endpoint would otherwise get noticeably slower over time."""

    def get(self, request):
        total_orders = OrderInfo.objects.count()
        total_merchants = MerchantInfo.objects.count()
        total_admin_users = User.objects.filter(is_staff=True, is_active=True).count()
        total_enquiries = EnquiryData.objects.count()
        total_customer_enquiries = ContactMessage.objects.filter(source=ContactMessage.SOURCE_CUSTOMER).count()
        total_merchant_enquiries = ContactMessage.objects.filter(source=ContactMessage.SOURCE_MERCHANT).count()
        total_customer_issues = CustomerIssue.objects.count()
        total_merchant_issues = MerchantIssue.objects.count()
        # NOTE: sums order_amount across EVERY order regardless of
        # order_status — pending and payment_failed orders count toward this
        # total exactly the same as paid ones. This is "total value of orders
        # placed", not "revenue actually collected". If the admin UI's "Total
        # Order Value" card is ever reported as looking too high compared to
        # actual settled revenue, this is why — the fix would be adding
        # `.filter(order_status="paid")` before `.aggregate(...)`, which
        # isn't done here since that changes what number gets reported
        # (a behavior change, not a comment).
        total_amount = OrderInfo.objects.aggregate(total=Sum("order_amount"))["total"] or 0

        # .values("order_status") + .annotate(count=Count("id")) is Django's
        # ORM idiom for "GROUP BY order_status, COUNT(*)" — produces one dict
        # per distinct status value, e.g. [{"order_status": "paid", "count": 42}, ...].
        orders_by_status = list(
            OrderInfo.objects.values("order_status").annotate(count=Count("id")).order_by("-count")
        )
        enquiries_by_status = list(
            EnquiryData.objects.values("status").annotate(count=Count("id")).order_by("-count")
        )
        customer_enquiries_by_status = list(
            ContactMessage.objects.filter(source=ContactMessage.SOURCE_CUSTOMER)
            .values("status")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        merchant_enquiries_by_status = list(
            ContactMessage.objects.filter(source=ContactMessage.SOURCE_MERCHANT)
            .values("status")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        customer_issues_by_status = list(
            CustomerIssue.objects.values("status").annotate(count=Count("id")).order_by("-count")
        )
        merchant_issues_by_status = list(
            MerchantIssue.objects.values("status").annotate(count=Count("id")).order_by("-count")
        )
        pdf_verification_by_status = list(
            PdfValidationRecord.objects.values("status").annotate(count=Count("id")).order_by("-count")
        )
        pdf_verification_by_status.append(
            {
                "status": "tracking_linked",
                "count": PdfValidationRecord.objects.exclude(awb__isnull=True).exclude(awb="").count(),
            }
        )

        return Response(
            {
                "total_orders": total_orders,
                "total_merchants": total_merchants,
                "total_admin_users": total_admin_users,
                "total_enquiries": total_enquiries,
                "total_customer_enquiries": total_customer_enquiries,
                "total_merchant_enquiries": total_merchant_enquiries,
                "total_customer_issues": total_customer_issues,
                "total_merchant_issues": total_merchant_issues,
                "total_amount": total_amount,
                "orders_by_status": orders_by_status,
                "enquiries_by_status": enquiries_by_status,
                "customer_enquiries_by_status": customer_enquiries_by_status,
                "merchant_enquiries_by_status": merchant_enquiries_by_status,
                "customer_issues_by_status": customer_issues_by_status,
                "merchant_issues_by_status": merchant_issues_by_status,
                "pdf_verification_by_status": pdf_verification_by_status,
            }
        )
