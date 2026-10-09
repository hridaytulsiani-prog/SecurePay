from django.db.models import Q
from django.utils import timezone
from rest_framework.views import Response

from adminpanel.permissions import AdminAPIView
from payments.models import OrderInfo, PAProtectedOrder
from payments.services.pa_control import (
    audit,
    auto_refund_overdue_missing_pdf,
    evaluate_decision,
    get_courier_verification,
    get_pdf_evidence,
    send_aggregator_request,
    serialize_order,
    sync_recent_orders_to_pa,
)
from tracking.models.pdf_validation import PdfValidationRecord


def _order_label(row):
    return row.merchant_order_id or row.pa_order_id or f"Order #{row.id}"


class AdminNeedsAttentionView(AdminAPIView):
    required_permission = "needs_attention.view"

    def get(self, request):
        q = (request.GET.get("q") or "").strip().lower()
        if request.GET.get("sync") == "1":
            sync_recent_orders_to_pa(limit=50)
        items = []

        missing_label_orders = (
            OrderInfo.objects.filter(shipment_id__isnull=True)
            .select_related("merchant", "customer_info")
            .order_by("-order_date")[:100]
        )
        for order in missing_label_orders:
            items.append(
                {
                    "id": f"missing-label-{order.id}",
                    "type": "missing_label",
                    "severity": "warning",
                    "title": "Shipment label missing",
                    "message": f"Merchant has not uploaded a shipment label for {_order_label(order)}.",
                    "merchant_name": order.merchant.merchant_name if order.merchant else "",
                    "merchant_email": order.merchant.merchant_email if order.merchant else "",
                    "order_id": order.merchant_order_id,
                    "pa_order_id": order.pa_order_id or "",
                    "customer_name": order.customer_info.customer_name if order.customer_info else "",
                    "amount": str(order.order_amount),
                    "currency": order.order_currency,
                    "created_at": (
                        order.shipment_label_last_reminded_at
                        or order.shipment_label_reminder_started_at
                        or order.order_date
                    ).isoformat(),
                    "action_status": "Waiting for merchant upload",
                    "can_decide": False,
                }
            )

        protected_orders = (
            PAProtectedOrder.objects.filter(order__isnull=False)
            .select_related("merchant", "order", "order__customer_info", "order__shipment_id")
            .prefetch_related("canonical_events", "pa_audit_entries")
            .filter(
                Q(decision_state=PAProtectedOrder.DECISION_MANUAL_REVIEW)
                | Q(metadata__aggregator_request__isnull=True)
            )
            .order_by("-updated_at")[:100]
        )
        for protected_order in protected_orders:
            pdf = get_pdf_evidence(protected_order)
            if auto_refund_overdue_missing_pdf(protected_order, pdf):
                protected_order.refresh_from_db()
                pdf = get_pdf_evidence(protected_order)
            courier = get_courier_verification(protected_order)
            pdf_status = pdf.get("status")
            courier_status = courier.get("status")
            needs_review = (
                pdf_status in {"NOT_UPLOADED", "NOT_FOUND", "NOT_APPROVED", "PROCESSING"}
                or courier_status in {"NOT_AVAILABLE", "PENDING", "NOT_VERIFIED"}
                or protected_order.decision_state == PAProtectedOrder.DECISION_MANUAL_REVIEW
            )
            if not needs_review:
                continue

            reasons = []
            if pdf_status in {"NOT_UPLOADED", "NOT_FOUND"}:
                reasons.append("PDF label is not uploaded")
            elif pdf_status == "NOT_APPROVED":
                reasons.append("PDF label is not approved")
            elif pdf_status == "PROCESSING":
                reasons.append("PDF label is still under review")
            if courier_status in {"NOT_AVAILABLE", "PENDING"}:
                reasons.append("Courier verification is pending")
            elif courier_status == "NOT_VERIFIED":
                reasons.append("Courier verification is not verified")

            serialized = serialize_order(protected_order)
            items.append(
                {
                    "id": f"pa-review-{protected_order.vaultpay_order_id}",
                    "type": "pa_review",
                    "severity": "danger" if "NOT_APPROVED" in {pdf_status, courier_status} else "warning",
                    "title": "PA order under review",
                    "message": "; ".join(reasons) or "Order needs admin financial decision.",
                    "merchant_name": protected_order.merchant.merchant_name if protected_order.merchant else "",
                    "merchant_email": protected_order.merchant.merchant_email if protected_order.merchant else "",
                    "order_id": protected_order.merchant_order_id,
                    "pa_order_id": protected_order.pa_order_id or "",
                    "vaultpay_order_id": protected_order.vaultpay_order_id,
                    "customer_name": serialized["customer"]["name"],
                    "amount": serialized["amount"],
                    "currency": serialized["currency"],
                    "pdf_status": pdf_status,
                    "courier_status": courier_status,
                    "decision_state": protected_order.decision_state,
                    "aggregator_request": serialized.get("aggregator_request"),
                    "created_at": protected_order.updated_at.isoformat(),
                    "action_status": "Needs release/refund decision",
                    "can_decide": True,
                }
            )

        items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        if q:
            items = [
                item
                for item in items
                if q
                in " ".join(
                    str(value)
                    for value in [
                        item.get("title"),
                        item.get("message"),
                        item.get("merchant_name"),
                        item.get("merchant_email"),
                        item.get("order_id"),
                        item.get("pa_order_id"),
                        item.get("vaultpay_order_id"),
                        item.get("customer_name"),
                        item.get("pdf_status"),
                        item.get("courier_status"),
                        item.get("decision_state"),
                    ]
                    if value
                ).lower()
            ]
        return Response({"results": items, "total": len(items), "generated_at": timezone.now().isoformat()})


class AdminNeedsAttentionDecisionView(AdminAPIView):
    required_permission = "pa_control.manage"

    def post(self, request, vaultpay_order_id):
        decision = request.data.get("decision")
        protected_order = PAProtectedOrder.objects.filter(vaultpay_order_id=vaultpay_order_id).first()
        if not protected_order:
            return Response({"error": "PA order not found."}, status=404)
        try:
            send_aggregator_request(protected_order, decision, actor=getattr(request, "admin_user", None))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        protected_order.refresh_from_db()
        return Response({"order": serialize_order(protected_order)})


class AdminNeedsAttentionPdfDecisionView(AdminAPIView):
    required_permission = "pa_control.manage"

    def post(self, request, vaultpay_order_id):
        decision = str(request.data.get("decision") or "").strip().upper()
        if decision not in {"APPROVED", "NOT_APPROVED"}:
            return Response({"error": "decision must be APPROVED or NOT_APPROVED"}, status=400)

        protected_order = PAProtectedOrder.objects.filter(vaultpay_order_id=vaultpay_order_id).first()
        if not protected_order:
            return Response({"error": "PA order not found."}, status=404)

        pdf = get_pdf_evidence(protected_order)
        record_id = pdf.get("record_id")
        record = None
        if record_id:
            record = PdfValidationRecord.objects.filter(id=record_id, merchant_id=protected_order.merchant_id).first()

        if not record:
            order = protected_order.order
            record = PdfValidationRecord.objects.create(
                merchant=protected_order.merchant,
                file_name=f"Manual PDF decision - {protected_order.merchant_order_id}",
                status="approved" if decision == "APPROVED" else "not_approved",
                verdict=f"MANUAL {decision}",
                delivery_partner=(order.shipment_id.courier if order and order.shipment_id else "") or "",
                awb=(order.shipment_id.awb if order and order.shipment_id else "") or "",
                order_id=protected_order.merchant_order_id or protected_order.pa_order_id,
                details={},
            )

        record.status = "approved" if decision == "APPROVED" else "not_approved"
        record.verdict = f"MANUAL {decision}"
        details = dict(record.details or {})
        details["manual_pdf_decision"] = {
            "decision": decision,
            "decided_at": timezone.now().isoformat(),
            "decided_by": getattr(getattr(request, "admin_user", None), "username", ""),
        }
        record.details = details
        record.save(update_fields=["status", "verdict", "details"])

        audit(
            protected_order,
            "PDF_DECISION_MANUAL",
            f"PDF verification manually marked {decision}",
            {"record_id": record.id, "file_name": record.file_name, "decision": decision},
        )
        evaluate_decision(protected_order)
        protected_order.refresh_from_db()

        return Response({"order": serialize_order(protected_order)})
