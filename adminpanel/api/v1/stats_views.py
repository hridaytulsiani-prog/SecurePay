# Powers the admin panel's Overview page (securepay-admin's OverviewPage.jsx
# calls GET /adminpanel/stats/ and renders this response's fields directly
# into its stat cards / breakdown lists — see that component for the exact
# field names it reads).
from django.db.models import Count, Sum

from rest_framework.views import Response

from adminpanel.permissions import AdminAPIView
from payments.models.orderinfo import OrderInfo
from payments.models.enquirydata import EnquiryData
from payments.models.merchantinfo import MerchantInfo


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
        total_enquiries = EnquiryData.objects.count()
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

        return Response(
            {
                "total_orders": total_orders,
                "total_merchants": total_merchants,
                "total_enquiries": total_enquiries,
                "total_amount": total_amount,
                "orders_by_status": orders_by_status,
                "enquiries_by_status": enquiries_by_status,
            }
        )
