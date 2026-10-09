"""Unified admin search: one box that finds an order, AWB, customer, merchant, enquiry, issue or message.

    GET /adminpanel/search/?q=<text>[&detail=1][&limit=<n>]

Searching an order id or an AWB returns the order together with everything the admin app knows about it: the
shipment and its tracking history, the payment state changes and ledger, the shipping-label checks, the delivery
verification decision, the customer enquiries about it and the merchant issues raised about it. Other sections
(merchants, enquiries, issues, customer messages, label checks) are matched on their own fields.

Each section is only returned when the admin has that section's permission, the same permissions the sidebar
uses, so the search never shows a role more than its pages do.
"""

from django.db.models import Q
from rest_framework.views import Response

from adminpanel.models import ContactMessage, MerchantIssue
from adminpanel.permissions import AdminAPIView, user_has_permission
from payments.models.enquirydata import EnquiryData
from payments.models.merchantinfo import MerchantInfo
from payments.models.orderinfo import OrderInfo
from payments.models.payment_lifecycle import PaymentLedgerEntry, PaymentStateTransition
from tracking.models.delhivery_verification_decision import DelhiveryVerificationDecision
from tracking.models.pdf_validation import PdfValidationRecord

MIN_QUERY_LENGTH = 2
DEFAULT_LIMIT = 8


def _iso(value):
    return value.isoformat() if value else None


def _order_summary(order):
    shipment = order.shipment_id
    customer = order.customer_info
    return {
        "id": order.id,
        "merchant_order_id": order.merchant_order_id,
        "pa_order_id": order.pa_order_id or "",
        "pa_payment_id": order.pa_payment_id or "",
        "amount": str(order.order_amount),
        "currency": order.order_currency,
        "order_status": order.order_status,
        "payment_state": order.payment_state,
        "payment_state_updated_at": _iso(order.payment_state_updated_at),
        "payment_provider": order.payment_provider or "",
        "order_date": _iso(order.order_date),
        "merchant_id": order.merchant_id,
        "merchant_name": order.merchant.merchant_name,
        "merchant_email": order.merchant.merchant_email,
        "customer_name": customer.customer_name,
        "customer_email": customer.customer_email,
        "customer_phone": customer.customer_phone,
        "customer_address": customer.customer_address,
        "awb": getattr(shipment, "awb", "") or "",
        "courier": getattr(shipment, "courier", "") or "",
        "delivery_status": getattr(shipment, "status", "") or "",
    }


def _order_detail(order, summary):
    """Everything related to one order (only built when ?detail=1)."""
    keys = [key for key in (order.merchant_order_id, order.pa_order_id) if key]
    awb = summary["awb"]
    shipment = order.shipment_id

    history = []
    if shipment is not None and isinstance(shipment.history, list):
        history = [
            {"ts": item.get("ts"), "status": item.get("status"), "note": item.get("note", "")}
            for item in shipment.history[-12:]
            if isinstance(item, dict)
        ]

    transitions = [
        {
            "from_state": row.from_state,
            "to_state": row.to_state,
            "source": row.source,
            "created_at": _iso(row.created_at),
        }
        for row in PaymentStateTransition.objects.filter(order=order).order_by("-created_at")[:12]
    ]
    ledger = [
        {
            "entry_type": row.entry_type,
            "amount": str(row.amount),
            "currency": row.currency,
            "reference": row.reference,
            "created_at": _iso(row.created_at),
        }
        for row in PaymentLedgerEntry.objects.filter(order=order).order_by("-created_at")[:12]
    ]

    label_filter = Q(order_id__in=keys)
    if awb:
        label_filter |= Q(awb=awb)
    labels = [
        {
            "id": row.id,
            "file_name": row.file_name,
            "status": row.status,
            "verdict": row.verdict or "",
            "risk_verdict": row.risk_verdict or "",
            "score": row.score,
            "courier": row.courier_partner or row.delivery_partner or "",
            "awb": row.awb or "",
            "uploaded_at": _iso(row.uploaded_at),
        }
        for row in PdfValidationRecord.objects.filter(label_filter, merchant_id=order.merchant_id).order_by("-uploaded_at")[:6]
    ]

    decision = None
    if awb:
        found = DelhiveryVerificationDecision.objects.filter(awb=awb).select_related("decided_by").first()
        if found:
            decision = {
                "decision": found.decision,
                "decided_by": found.decided_by.username if found.decided_by else "",
                "decided_at": _iso(found.decided_at),
            }

    enquiries = [
        {
            "id": row.id,
            "enquiry_id": row.enquiry_id,
            "receipt_status": row.receipt_status,
            "status": row.status,
            "text": row.enquiry_text,
            "resolution_status": row.resolution_status or "",
            "created_at": _iso(row.created_at),
        }
        for row in EnquiryData.objects.filter(order_id__in=keys).order_by("-created_at")[:6]
    ]

    issue_filter = Q()
    for key in keys:
        issue_filter |= Q(orders__icontains=f'"{key}"')
    issues = []
    if keys:
        issues = [
            _issue_summary(row)
            for row in MerchantIssue.objects.filter(issue_filter, merchant_id=order.merchant_id).order_by("-created_at")[:6]
        ]

    return {
        "tracking_history": history,
        "payment_transitions": transitions,
        "ledger": ledger,
        "labels": labels,
        "verification": decision,
        "enquiries": enquiries,
        "issues": issues,
    }


def _issue_summary(issue):
    return {
        "id": issue.id,
        "reference": issue.reference,
        "merchant_name": issue.merchant_name,
        "issue_type_label": issue.get_issue_type_display(),
        "status": issue.status,
        "status_label": issue.get_status_display(),
        "order_count": len(issue.orders or []),
        "description": issue.description,
        "created_at": _iso(issue.created_at),
    }


class AdminSearchView(AdminAPIView):
    required_permission = "dashboard.view"

    def get(self, request):
        q = (request.GET.get("q") or "").strip()
        detail = request.GET.get("detail") == "1"
        try:
            limit = min(max(int(request.GET.get("limit", DEFAULT_LIMIT)), 1), 25)
        except ValueError:
            limit = DEFAULT_LIMIT

        result = {
            "query": q,
            "orders": [],
            "merchants": [],
            "enquiries": [],
            "issues": [],
            "messages": [],
            "labels": [],
        }
        if len(q) < MIN_QUERY_LENGTH:
            result["counts"] = {key: 0 for key in result if key not in ("query", "counts")}
            return Response(result)

        user = request.admin_user

        if user_has_permission(user, "orders.view"):
            orders = (
                OrderInfo.objects.select_related("merchant", "customer_info", "shipment_id")
                .filter(
                    Q(merchant_order_id__icontains=q)
                    | Q(pa_order_id__icontains=q)
                    | Q(pa_payment_id__icontains=q)
                    | Q(shipment_id__awb__icontains=q)
                    | Q(customer_info__customer_name__icontains=q)
                    | Q(customer_info__customer_email__icontains=q)
                    | Q(customer_info__customer_phone__icontains=q)
                    | Q(merchant__merchant_name__icontains=q)
                )
                .order_by("-order_date")[:limit]
            )
            for order in orders:
                summary = _order_summary(order)
                if detail:
                    summary["detail"] = _order_detail(order, summary)
                result["orders"].append(summary)

            result["merchants"] = [
                {
                    "id": row.id,
                    "merchant_name": row.merchant_name,
                    "merchant_email": row.merchant_email,
                    "merchant_phone": row.merchant_phone,
                    "created_at": _iso(row.created_at),
                }
                for row in MerchantInfo.objects.filter(
                    Q(merchant_name__icontains=q) | Q(merchant_email__icontains=q) | Q(merchant_phone__icontains=q)
                ).order_by("-id")[:limit]
            ]

        if user_has_permission(user, "enquiries.view"):
            result["enquiries"] = [
                {
                    "id": row.id,
                    "enquiry_id": row.enquiry_id,
                    "order_id": row.order_id,
                    "receipt_status": row.receipt_status,
                    "status": row.status,
                    "text": row.enquiry_text,
                    "created_at": _iso(row.created_at),
                }
                for row in EnquiryData.objects.filter(
                    Q(enquiry_id__icontains=q) | Q(order_id__icontains=q) | Q(enquiry_text__icontains=q)
                ).order_by("-created_at")[:limit]
            ]

        if user_has_permission(user, "contact_messages.manage"):
            ref = q.upper().removeprefix("ISS-").lstrip("0")
            issue_query = (
                Q(merchant_name__icontains=q)
                | Q(merchant_email__icontains=q)
                | Q(description__icontains=q)
                | Q(orders__icontains=q)
            )
            if ref.isdigit() and q.upper().startswith("ISS-"):
                issue_query |= Q(id=int(ref))
            result["issues"] = [_issue_summary(row) for row in MerchantIssue.objects.filter(issue_query).order_by("-created_at")[:limit]]

            result["messages"] = [
                {
                    "id": row.id,
                    "source": row.source,
                    "name": row.name,
                    "email": row.email,
                    "order_id": row.order_id,
                    "message": row.message,
                    "status": row.status,
                    "status_label": row.get_status_display(),
                    "created_at": _iso(row.created_at),
                }
                for row in ContactMessage.objects.filter(
                    Q(name__icontains=q) | Q(email__icontains=q) | Q(order_id__icontains=q) | Q(message__icontains=q)
                ).order_by("-created_at")[:limit]
            ]

        if user_has_permission(user, "suspicious_pdfs.view"):
            result["labels"] = [
                {
                    "id": row.id,
                    "file_name": row.file_name,
                    "status": row.status,
                    "verdict": row.verdict or "",
                    "courier": row.courier_partner or row.delivery_partner or "",
                    "awb": row.awb or "",
                    "order_id": row.order_id or "",
                    "uploaded_at": _iso(row.uploaded_at),
                }
                for row in PdfValidationRecord.objects.filter(
                    Q(awb__icontains=q) | Q(order_id__icontains=q) | Q(file_name__icontains=q)
                ).order_by("-uploaded_at")[:limit]
            ]

        result["counts"] = {key: len(value) for key, value in result.items() if isinstance(value, list)}
        return Response(result)
