"""Order check opened from the admin search (no ticket needed):

    GET /adminpanel/orders/check/?order=<merchant order id, aggregator order id or AWB>

A report organised around the order itself (checklist, the same status in every system, one timeline), built live each
time. It is a different report from the one built for a merchant issue. Merchant issues already raised about the order
are included in it.
"""

from django.db.models import Q
from rest_framework import status
from rest_framework.views import Response

from adminpanel.api.v1.customer_issue_views import customer_issues_for_order
from adminpanel.models import ContactMessage, MerchantIssue
from adminpanel.order_check import build_order_check
from adminpanel.permissions import AdminAPIView
from payments.models.orderinfo import OrderInfo


class AdminOrderCheckView(AdminAPIView):
    required_permission = "orders.view"

    def get(self, request):
        key = (request.GET.get("order") or "").strip()
        if not key:
            return Response({"error": "Give an order ID or AWB."}, status=status.HTTP_400_BAD_REQUEST)

        order = (
            OrderInfo.objects.select_related("merchant", "customer_info", "shipment_id")
            .filter(Q(merchant_order_id=key) | Q(pa_order_id=key) | Q(shipment_id__awb=key))
            .order_by("-order_date")
            .first()
        )
        if order is None:
            return Response({"error": "No order found for that ID."}, status=status.HTTP_404_NOT_FOUND)

        issue_filter = Q()
        for value in (order.merchant_order_id, order.pa_order_id):
            if value:
                issue_filter |= Q(orders__icontains=f'"{value}"')
        rows = list(
            MerchantIssue.objects.filter(issue_filter, merchant_id=order.merchant_id)
            .prefetch_related("messages__admin_user", "notes__admin_user")
            .order_by("-created_at")[:10]
        )
        issues = [
            {
                "id": row.id,
                "reference": row.reference,
                "issue_type_label": row.get_issue_type_display(),
                "status": row.status,
                "status_label": row.get_status_display(),
                "created_at": row.created_at.isoformat(),
                "description": row.description,
                "messages": [
                    {
                        "sender": m.sender,
                        "body": m.body,
                        "by": m.admin_user.username if m.admin_user else "",
                        "created_at": m.created_at.isoformat(),
                    }
                    for m in row.messages.all()
                ],
                "notes": [
                    {
                        "note": n.note,
                        "by": n.admin_user.username if n.admin_user else "",
                        "created_at": n.created_at.isoformat(),
                    }
                    for n in row.notes.all()
                ],
            }
            for row in rows
        ]
        contact_ids = [v for v in (order.merchant_order_id, order.pa_order_id) if v]
        contact_messages = [
            {
                "name": m.name,
                "source": m.source,
                "message": m.message,
                "status_label": m.get_status_display(),
                "internal_note": m.internal_note,
                "created_at": m.created_at.isoformat(),
            }
            for m in ContactMessage.objects.filter(order_id__in=contact_ids).order_by("-created_at")[:10]
        ]
        payload = build_order_check(order, issues)
        payload["contact_messages"] = contact_messages
        payload["customer_issues"] = customer_issues_for_order(order)
        return Response(payload)
