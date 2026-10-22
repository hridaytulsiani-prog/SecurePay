"""A complete, read-only history for one merchant in the admin app."""

from decimal import Decimal

from django.db.models import Q, Sum
from rest_framework import status
from rest_framework.views import Response

from adminpanel.models import ContactMessage, CustomerIssue, MerchantIssue
from adminpanel.permissions import AdminAPIView, user_has_permission
from adminpanel.api.v1.payment_ids import payment_id_for_order
from payments.models.enquirydata import EnquiryData
from payments.models.merchantinfo import MerchantInfo
from payments.models.orderinfo import OrderInfo
from tracking.models.pdf_validation import PdfValidationRecord


def _iso(value):
    return value.isoformat() if value else None


class AdminMerchantHistoryView(AdminAPIView):
    """Return profile, orders, tickets, enquiries, labels and messages for a merchant."""

    required_permission = "orders.view"

    def get(self, request, merchant_id):
        merchant = MerchantInfo.objects.filter(id=merchant_id).first()
        if merchant is None:
            return Response({"error": "Merchant not found."}, status=status.HTTP_404_NOT_FOUND)

        orders_qs = (
            OrderInfo.objects.filter(merchant_id=merchant.id)
            .select_related("customer_info", "shipment_id")
            .order_by("-order_date", "-id")
        )
        orders = list(orders_qs)
        oldest_order_date = orders[-1].order_date if orders else None
        order_keys = {
            key
            for order in orders
            for key in (order.merchant_order_id, order.pa_order_id)
            if key
        }
        awbs = {
            order.shipment_id.awb
            for order in orders
            if order.shipment_id and order.shipment_id.awb
        }

        order_rows = []
        for order in orders:
            shipment = order.shipment_id
            customer = order.customer_info
            order_rows.append(
                {
                    "id": order.id,
                    "merchant_order_id": order.merchant_order_id,
                    "pa_order_id": order.pa_order_id or "",
                    "pa_payment_id": order.pa_payment_id or order.phonepe_payment_id or "",
                    "display_payment_id": payment_id_for_order(order),
                    "amount": str(order.order_amount),
                    "currency": order.order_currency,
                    "order_status": order.order_status,
                    "payment_state": order.payment_state,
                    "payment_provider": order.payment_provider or "",
                    "order_date": _iso(order.order_date),
                    "customer_name": customer.customer_name,
                    "customer_email": customer.customer_email,
                    "customer_phone": customer.customer_phone,
                    "awb": getattr(shipment, "awb", "") or "",
                    "courier": getattr(shipment, "courier", "") or "",
                    "delivery_status": getattr(shipment, "status", "") or "",
                }
            )

        can_manage_issues = user_has_permission(request.admin_user, "contact_messages.manage")
        can_view_enquiries = user_has_permission(request.admin_user, "enquiries.view")
        can_view_labels = user_has_permission(request.admin_user, "suspicious_pdfs.view")

        issues = [
            {
                "id": issue.id,
                "reference": issue.reference,
                "issue_type": issue.issue_type,
                "issue_type_label": issue.get_issue_type_display(),
                "status": issue.status,
                "status_label": issue.get_status_display(),
                "description": issue.description,
                "orders": issue.orders or [],
                "message_count": issue.messages.count(),
                "note_count": issue.notes.count(),
                "created_at": _iso(issue.created_at),
                "updated_at": _iso(issue.updated_at),
            }
            for issue in MerchantIssue.objects.filter(merchant_id=merchant.id)
            .prefetch_related("messages", "notes")
            .order_by("-created_at", "-id")
        ] if can_manage_issues else []

        enquiry_rows = []
        if order_keys and can_view_enquiries:
            for enquiry in EnquiryData.objects.filter(order_id__in=order_keys).order_by("-created_at", "-id"):
                enquiry_rows.append(
                    {
                        "id": enquiry.id,
                        "enquiry_id": enquiry.enquiry_id,
                        "order_id": enquiry.order_id,
                        "receipt_status": enquiry.receipt_status,
                        "status": enquiry.status,
                        "resolution_status": enquiry.resolution_status,
                        "text": enquiry.enquiry_text,
                        "created_at": _iso(enquiry.created_at),
                    }
                )

        pdf_rows = [
            {
                "id": record.id,
                "file_name": record.file_name,
                "status": record.status,
                "verdict": record.verdict or "",
                "risk_verdict": record.risk_verdict or "",
                "courier": record.delivery_partner or record.courier_partner or "",
                "awb": record.awb or "",
                "order_id": record.order_id or "",
                "uploaded_at": _iso(record.uploaded_at),
            }
            for record in PdfValidationRecord.objects.filter(merchant_id=merchant.id).order_by("-uploaded_at", "-id")
        ] if can_view_labels else []

        messages = [
            {
                "id": message.id,
                "topic": message.topic,
                "order_id": message.order_id,
                "message": message.message,
                "status": message.status,
                "status_label": message.get_status_display(),
                "created_at": _iso(message.created_at),
            }
            for message in ContactMessage.objects.filter(
                source=ContactMessage.SOURCE_MERCHANT,
                email__iexact=merchant.merchant_email,
            ).order_by("-created_at", "-id")
        ] if can_manage_issues else []

        customer_issue_query = Q()
        if order_keys:
            customer_issue_query |= Q(order_ref__in=order_keys)
        if awbs:
            customer_issue_query |= Q(order_ref__in=awbs)
        customer_issues = []
        if customer_issue_query and can_manage_issues:
            customer_issues = [
                {
                    "id": issue.id,
                    "reference": issue.reference,
                    "customer_name": issue.customer_name,
                    "customer_phone": issue.customer_phone,
                    "customer_email": issue.customer_email,
                    "order_ref": issue.order_ref,
                    "order": issue.order or {},
                    "issue_type": issue.issue_type,
                    "issue_type_label": issue.get_issue_type_display(),
                    "status": issue.status,
                    "status_label": issue.get_status_display(),
                    "description": issue.description,
                    "details": issue.details or [],
                    "attachments": [
                        {
                            "id": attachment.id,
                            "name": attachment.original_name or attachment.file.name.rsplit("/", 1)[-1],
                            "url": request.build_absolute_uri(attachment.file.url),
                        }
                        for attachment in issue.attachments.all()
                    ],
                    "created_at": _iso(issue.created_at),
                    "updated_at": _iso(issue.updated_at),
                }
                for issue in CustomerIssue.objects.filter(customer_issue_query).prefetch_related("attachments").order_by("-created_at", "-id")
            ]

        totals = orders_qs.aggregate(amount=Sum("order_amount"))
        total_amount = totals["amount"] or Decimal("0")
        open_issue_count = sum(1 for issue in issues if issue["status"] != MerchantIssue.STATUS_RESOLVED)
        suspicious_label_count = sum(1 for row in pdf_rows if row["status"] == "not_approved")

        return Response(
            {
                "merchant": {
                    "id": merchant.id,
                    "merchant_name": merchant.merchant_name,
                    "merchant_email": merchant.merchant_email,
                    "merchant_phone": merchant.merchant_phone,
                    "merchant_address": merchant.merchant_address,
                    # Older imported merchant rows may not have a recorded signup timestamp.
                    # Their first order is the earliest reliable membership activity we have.
                    "created_at": _iso(merchant.created_at or oldest_order_date),
                    "courier_setup_completed": merchant.courier_setup_completed,
                    "courier_preferences": merchant.courier_preferences or {},
                },
                "summary": {
                    "order_count": len(order_rows),
                    "total_order_amount": str(total_amount),
                    "open_issue_count": open_issue_count,
                    "customer_issue_count": len(customer_issues),
                    "enquiry_count": len(enquiry_rows),
                    "suspicious_label_count": suspicious_label_count,
                },
                "orders": order_rows,
                "issues": issues,
                "customer_issues": customer_issues,
                "enquiries": enquiry_rows,
                "labels": pdf_rows,
                "messages": messages,
            }
        )
