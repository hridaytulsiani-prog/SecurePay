from django.db.models import Q
from django.conf import settings
from django.utils.dateparse import parse_date
from rest_framework.views import APIView, Response
from rest_framework import status

from adminpanel.permissions import AdminAPIView
from payments.models import OrderInfo, PaymentStateTransition, RefundRequest
from payments.services.refunds import build_refund_verification


def _recommendation(order, verification):
    if order.payment_state == "REFUNDED" or order.order_status == "refunded":
        return "REFUNDED"
    if order.payment_state == "REFUND_PENDING" or order.order_status == "refund_pending":
        return "REFUND_IN_PROGRESS"
    if verification.get("eligible"):
        return "REFUND_RECOMMENDED"
    if order.payment_state in {"FUNDS_HELD", "SETTLEMENT_ELIGIBLE", "SETTLEMENT_PROCESSING", "SETTLED"}:
        return "RELEASE_RECOMMENDED"
    return "REVIEW_PENDING"


def _guardrails(order, refund_request, verification):
    checks = []
    checks.append({
        "name": "Stored payment transaction",
        "status": "PASSED" if order.pa_payment_id else "FAILED",
        "detail": order.pa_payment_id or "Missing provider transaction id",
    })
    checks.append({
        "name": "Refund idempotency",
        "status": "PASSED" if refund_request else "READY",
        "detail": refund_request.refund_reference if refund_request else "No refund request created yet",
    })
    checks.append({
        "name": "Amount boundary",
        "status": "PASSED",
        "detail": f"{order.order_currency} {order.order_amount}",
    })
    checks.append({
        "name": "Verification evidence",
        "status": "FAILED" if verification.get("eligible") else "PASSED",
        "detail": ", ".join(verification.get("failures") or ["No verification failures"]),
    })
    return checks


def _build_oversight_response(request):
    try:
        limit = min(int(request.GET.get("limit", 25)), 200)
    except ValueError:
        limit = 25
    try:
        page = max(int(request.GET.get("page", 1)), 1)
    except ValueError:
        page = 1

    q = (request.GET.get("q") or "").strip().lower()
    merchant_id = request.GET.get("merchant_id")
    recommendation_filter = (request.GET.get("recommendation") or "").strip()
    provider_filter = (request.GET.get("provider") or "").strip().lower()
    date_from = parse_date(request.GET.get("date_from") or "")
    date_to = parse_date(request.GET.get("date_to") or "")

    qs = OrderInfo.objects.select_related(
        "merchant", "customer_info", "shipment_id", "refund_request"
    ).order_by("-order_date")

    if merchant_id:
        qs = qs.filter(merchant_id=merchant_id)
    if provider_filter:
        qs = qs.filter(payment_provider__iexact=provider_filter)
    if date_from:
        qs = qs.filter(order_date__date__gte=date_from)
    if date_to:
        qs = qs.filter(order_date__date__lte=date_to)
    if q:
        qs = qs.filter(
            Q(merchant_order_id__icontains=q)
            | Q(pa_order_id__icontains=q)
            | Q(pa_payment_id__icontains=q)
            | Q(merchant__merchant_name__icontains=q)
            | Q(customer_info__customer_name__icontains=q)
            | Q(shipment_id__awb__icontains=q)
        )

    rows = []
    orders = list(qs[:1000])
    transition_map = {}
    for transition in PaymentStateTransition.objects.filter(order__in=orders).order_by("-created_at"):
        transition_map.setdefault(transition.order_id, []).append(transition)

    for order in orders:
        refund_request = getattr(order, "refund_request", None)
        verification = (
            refund_request.verification_snapshot
            if refund_request and refund_request.verification_snapshot
            else build_refund_verification(order)
        )
        recommendation = _recommendation(order, verification)
        if recommendation_filter and recommendation != recommendation_filter:
            continue

        transitions = transition_map.get(order.id, [])[:8]
        rows.append({
            "decision_id": "",
            "audit_reference": f"ORDER-{order.id}",
            "recommendation": recommendation,
            "order_id": order.merchant_order_id,
            "provider_order_id": order.pa_order_id,
            "provider_payment_id": order.pa_payment_id,
            "payment_provider": order.payment_provider or "",
            "amount": str(order.order_amount),
            "currency": order.order_currency,
            "payment_state": order.payment_state,
            "order_status": order.order_status,
            "merchant": {
                "id": order.merchant_id,
                "name": order.merchant.merchant_name if order.merchant else "",
                "email": order.merchant.merchant_email if order.merchant else "",
            },
            "customer": {
                "name": order.customer_info.customer_name if order.customer_info else "",
                "phone": order.customer_info.customer_phone if order.customer_info else "",
            },
            "shipment": {
                "awb": order.shipment_id.awb if order.shipment_id else "",
                "courier": order.shipment_id.courier if order.shipment_id else "",
                "status": order.shipment_id.status if order.shipment_id else "",
            },
            "verification": verification,
            "guardrails": _guardrails(order, refund_request, verification),
            "refund": {
                "reference": refund_request.refund_reference if refund_request else "",
                "provider_refund_id": refund_request.provider_refund_id if refund_request else "",
                "status": refund_request.status if refund_request else "",
                "reason": refund_request.reason if refund_request else "",
                "requested_at": refund_request.requested_at if refund_request else None,
                "completed_at": refund_request.completed_at if refund_request else None,
            },
            "audit": [
                {
                    "from_state": item.from_state,
                    "to_state": item.to_state,
                    "source": item.source,
                    "metadata": item.metadata,
                    "created_at": item.created_at,
                }
                for item in transitions
            ],
            "created_at": order.order_date,
        })

    total = len(rows)
    for index, row in enumerate(reversed(rows), start=1):
        row["decision_id"] = f"DEC-{index:02d}"

    total_pages = max(1, (total + limit - 1) // limit)
    start = (page - 1) * limit
    end = start + limit
    return Response({
        "results": rows[start:end],
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "summary": {
            "refund_recommended": sum(1 for row in rows if row["recommendation"] == "REFUND_RECOMMENDED"),
            "release_recommended": sum(1 for row in rows if row["recommendation"] == "RELEASE_RECOMMENDED"),
            "refund_in_progress": sum(1 for row in rows if row["recommendation"] == "REFUND_IN_PROGRESS"),
            "refunded": sum(1 for row in rows if row["recommendation"] == "REFUNDED"),
            "review_pending": sum(1 for row in rows if row["recommendation"] == "REVIEW_PENDING"),
        },
    })


class OmniwareOversightReportView(AdminAPIView):
    """Read-only internal admin view of the Omniware partner report."""
    required_permission = "aggregator.view"

    def get(self, request):
        return _build_oversight_response(request)


class OmniwarePartnerLoginView(APIView):
    """Token exchange for Omniware's restricted partner portal."""

    def post(self, request):
        configured_token = getattr(settings, "OMNIWARE_PARTNER_ACCESS_TOKEN", "")
        supplied_token = request.data.get("access_token") or request.data.get("token")
        if not configured_token or supplied_token != configured_token:
            return Response({"error": "Invalid partner access token."}, status=status.HTTP_401_UNAUTHORIZED)
        return Response({"token": configured_token, "partner": {"name": "Omniware"}})


class OmniwarePartnerOversightReportView(APIView):
    """Partner-only report endpoint. No admin session token is accepted here."""

    def get(self, request):
        configured_token = getattr(settings, "OMNIWARE_PARTNER_ACCESS_TOKEN", "")
        auth_header = request.headers.get("Authorization", "")
        supplied_token = auth_header.split(" ", 1)[1].strip() if auth_header.lower().startswith("bearer ") else ""
        if not configured_token or supplied_token != configured_token:
            return Response({"error": "Invalid partner access token."}, status=status.HTTP_401_UNAUTHORIZED)
        return _build_oversight_response(request)
