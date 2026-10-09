"""Detailed report for one order inside a merchant issue:

    GET /adminpanel/merchant-issues/<id>/report/?order=<order id or AWB>

The report is generated automatically when the merchant raises the ticket (adminpanel/order_report.py) and stored on
the issue, so the admin opens a finished report instead of checking each system by hand. It shows what the merchant
saw on their dashboard, what our system and the tracking source hold, what the customer says, and automatic findings
for the kind of issue raised. Issues created before reports existed get theirs built (and saved) the first time
they are opened.
"""

from django.db.models import Q
from rest_framework import status
from rest_framework.views import Response

from adminpanel.api.v1.customer_issue_views import customer_issues_for_order
from adminpanel.api.v1.merchant_issue_views import _issues_queryset
from adminpanel.models import MerchantIssueMessage
from adminpanel.order_report import build_order_report, pa_status_for
from adminpanel.permissions import AdminAPIView
from payments.models.orderinfo import OrderInfo


def _iso(value):
    return value.isoformat() if value else None


def find_order(issue, snapshot_row):
    return (
        OrderInfo.objects.select_related("merchant", "customer_info", "shipment_id")
        .filter(merchant_id=issue.merchant_id)
        .filter(
            Q(merchant_order_id=snapshot_row.get("merchant_order_id") or "__none__")
            | Q(pa_order_id=snapshot_row.get("pa_order_id") or "__none__")
        )
        .first()
    )


class AdminMerchantIssueOrderReportView(AdminAPIView):
    required_permission = "contact_messages.manage"

    def get(self, request, issue_id):
        issue = _issues_queryset().filter(id=issue_id).first()
        if issue is None:
            return Response({"error": "Issue not found."}, status=status.HTTP_404_NOT_FOUND)

        key = (request.GET.get("order") or "").strip()
        snapshot_row = next(
            (
                item
                for item in (issue.orders or [])
                if key and key in (item.get("order_id"), item.get("merchant_order_id"), item.get("pa_order_id"), item.get("awb"))
            ),
            None,
        )
        if snapshot_row is None:
            return Response({"error": "That order is not part of this issue."}, status=status.HTTP_404_NOT_FOUND)

        order = find_order(issue, snapshot_row)
        if order is None:
            return Response({"error": "The order no longer exists."}, status=status.HTTP_404_NOT_FOUND)

        order_key = snapshot_row.get("merchant_order_id") or snapshot_row.get("order_id")
        reports = issue.report if isinstance(issue.report, dict) else {}
        report = reports.get(order_key)
        if not report:
            # An issue raised before automatic reports existed: build it now and keep it.
            report = build_order_report(issue.issue_type, order, snapshot_row)
            reports = {**reports, order_key: report}
            issue.report = reports
            issue.save(update_fields=["report"])

        merchant_messages = []
        our_replies = []
        for message in issue.messages.all():
            if message.sender == MerchantIssueMessage.SENDER_MERCHANT:
                merchant_messages.append({"body": message.body, "created_at": _iso(message.created_at)})
            else:
                our_replies.append(
                    {
                        "body": message.body,
                        "admin_name": message.admin_user.username if message.admin_user else "",
                        "created_at": _iso(message.created_at),
                    }
                )

        # Every internal note the admins wrote about this order: on this ticket, on customer enquiries, on messages.
        admin_notes = [
            {"source": issue.reference, "note": n.note, "by": n.admin_user.username if n.admin_user else "", "created_at": _iso(n.created_at)}
            for n in issue.notes.all()
        ]
        for enquiry in (report.get("customer") or {}).get("enquiries", []):
            for note in enquiry.get("notes", []):
                admin_notes.append({"source": enquiry.get("enquiry_id", "Enquiry"), "note": note.get("note", ""), "by": note.get("by", ""), "created_at": note.get("created_at")})
        from adminpanel.models import ContactMessage

        order_ids = [v for v in (order.merchant_order_id, order.pa_order_id) if v]
        for m in ContactMessage.objects.filter(order_id__in=order_ids).exclude(internal_note=""):
            admin_notes.append({"source": "Message", "note": m.internal_note, "by": m.handled_by.username if m.handled_by else "", "created_at": _iso(m.created_at)})
        customer_issues = customer_issues_for_order(order)
        for ticket in customer_issues:
            for note in ticket["notes"]:
                admin_notes.append({"source": ticket["reference"], "note": note["note"], "by": note["by"], "created_at": note["created_at"]})
        admin_notes.sort(key=lambda item: item["created_at"] or "", reverse=True)

        shipment = order.shipment_id
        return Response(
            {
                "issue": {
                    "id": issue.id,
                    "reference": issue.reference,
                    "issue_type": issue.issue_type,
                    "issue_type_label": issue.get_issue_type_display(),
                    "status": issue.status,
                    "status_label": issue.get_status_display(),
                    "created_at": _iso(issue.created_at),
                    # Every order in the issue (a bulk issue has several), so the page can show one tab per order.
                    "order_keys": [
                        item.get("awb") or item.get("merchant_order_id") or item.get("order_id")
                        for item in (issue.orders or [])
                    ],
                },
                "generated_at": report.get("generated_at"),
                "verdict": report.get("verdict"),
                "findings": report.get("findings", []),
                "order": report.get("order", {}),
                "merchant_view": report.get("merchant_view", {}),
                # How the order looks right now, to compare with the report taken when the ticket was raised.
                "now": {
                    "delivery_status": getattr(shipment, "status", "") or "",
                    "payment_state": order.payment_state,
                    "order_status": order.order_status,
                    "pa_status": pa_status_for(order),
                },
                "merchant": {
                    "name": issue.merchant_name,
                    "email": issue.merchant_email,
                    "issue_type_label": issue.get_issue_type_display(),
                    "description": issue.description,
                    "messages": merchant_messages,
                    "our_replies": our_replies,
                },
                "admin_notes": admin_notes,
                "customer_issues": [{k: v for k, v in t.items() if k != "notes"} for t in customer_issues],
                "courier": report.get("courier", {}),
                "customer": report.get("customer", {}),
                "payment": report.get("payment", {}),
            }
        )
