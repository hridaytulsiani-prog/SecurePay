from django.db.models import Q
from django.utils import timezone
from rest_framework.views import Response

from adminpanel.permissions import AdminAPIView
from payments.models import OrderInfo, PAProtectedOrder
from payments.services.pa_control import (
    audit,
    auto_refund_overdue_missing_pdf,
    evaluate_decision,
    ensure_protected_order_for_order,
    get_courier_verification,
    get_pdf_evidence,
    send_aggregator_request,
    serialize_order,
    sync_recent_orders_to_pa,
)
from tracking.models.pdf_validation import PdfValidationRecord
from adminpanel.api.v1.payment_ids import (
    payment_id_for_order,
    payment_id_for_protected_order,
)


def _order_label(row):
    return row.merchant_order_id or row.pa_order_id or f"Order #{row.id}"


def _demo_payment_id(protected_order):
    return payment_id_for_protected_order(protected_order)


class AdminNeedsAttentionView(AdminAPIView):
    required_permission = "needs_attention.view"

    def get(self, request):
        q = (request.GET.get("q") or "").strip().lower()
        sort_order = (request.GET.get("sort") or "newest").strip().lower()
        if request.GET.get("sync") == "1":
            sync_recent_orders_to_pa(limit=50)
        items = []

        all_orders = list(
            OrderInfo.objects.select_related(
                "merchant", "customer_info", "shipment_id", "pa_protection"
            ).order_by("-order_date")
        )
        label_records = PdfValidationRecord.objects.values_list(
            "merchant_id", "order_id", "awb"
        )
        label_order_keys = {
            (merchant_id, str(order_id).strip().lower())
            for merchant_id, order_id, _awb in label_records
            if order_id
        }
        label_awb_keys = {
            (merchant_id, str(awb).strip().lower())
            for merchant_id, _order_id, awb in label_records
            if awb
        }
        missing_label_orders = []
        for order in all_orders:
            order_references = [order.merchant_order_id, order.pa_order_id]
            has_label_by_order = any(
                (order.merchant_id, str(reference).strip().lower()) in label_order_keys
                for reference in order_references
                if reference
            )
            shipment_awb = order.shipment_id.awb if order.shipment_id else ""
            has_label_by_awb = bool(
                shipment_awb
                and (order.merchant_id, str(shipment_awb).strip().lower()) in label_awb_keys
            )
            # In the normal workflow an AWB is linked only after the label has
            # been approved. Older/imported shipments may no longer have their
            # original PDF record, but they are not waiting for a new upload.
            if not has_label_by_order and not has_label_by_awb and not order.shipment_id_id:
                missing_label_orders.append(order)

        missing_label_order_ids = {order.id for order in missing_label_orders}
        for order in missing_label_orders:
            protected_order = getattr(order, "pa_protection", None)
            pdf_status = get_pdf_evidence(protected_order).get("status") if protected_order else "NOT_FOUND"
            if pdf_status in {None, "", "NOT_UPLOADED"}:
                pdf_status = "NOT_FOUND"
            shipment = order.shipment_id
            courier_status = (
                get_courier_verification(protected_order).get("status")
                if protected_order
                else ("PENDING" if shipment and shipment.awb else "NOT_AVAILABLE")
            )
            items.append(
                {
                    "id": f"missing-label-{order.id}",
                    "type": "missing_label",
                    "severity": "warning",
                    "title": "Shipment label missing",
                    "message": "Merchant has not uploaded a shipment label",
                    "merchant_id": order.merchant_id,
                    "merchant_name": order.merchant.merchant_name if order.merchant else "",
                    "merchant_email": order.merchant.merchant_email if order.merchant else "",
                    "order_id": order.merchant_order_id,
                    "pa_order_id": order.pa_order_id or "",
                    "pa_payment_id": payment_id_for_order(order),
                    "vaultpay_order_id": protected_order.vaultpay_order_id if protected_order else "",
                    "customer_name": order.customer_info.customer_name if order.customer_info else "",
                    "customer_email": order.customer_info.customer_email if order.customer_info else "",
                    "customer_phone": order.customer_info.customer_phone if order.customer_info else "",
                    "awb": shipment.awb if shipment else "",
                    "courier": shipment.courier if shipment else "",
                    "shipment_status": shipment.status if shipment else "",
                    "pdf_status": pdf_status,
                    "courier_status": courier_status,
                    "amount": str(order.order_amount),
                    "currency": order.order_currency,
                    "created_at": (
                        order.shipment_label_last_reminded_at
                        or order.shipment_label_reminder_started_at
                        or order.order_date
                    ).isoformat(),
                    "action_status": "Waiting for upload",
                    "can_decide": True,
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
            .order_by("-updated_at")
        )
        for protected_order in protected_orders:
            if protected_order.order_id in missing_label_order_ids:
                continue
            pdf = get_pdf_evidence(protected_order)
            if auto_refund_overdue_missing_pdf(protected_order, pdf):
                protected_order.refresh_from_db()
                pdf = get_pdf_evidence(protected_order)
            courier = get_courier_verification(protected_order)
            pdf_status = pdf.get("status")
            courier_status = courier.get("status")
            aggregator_request = (protected_order.metadata or {}).get("aggregator_request")
            unresolved = (
                protected_order.decision_state == PAProtectedOrder.DECISION_MANUAL_REVIEW
                or not aggregator_request
            )
            needs_review = (
                unresolved
                and (
                    pdf_status in {"NOT_UPLOADED", "NOT_FOUND", "NOT_APPROVED", "PROCESSING"}
                    or courier_status in {"NOT_AVAILABLE", "PENDING", "NOT_VERIFIED"}
                    or protected_order.decision_state == PAProtectedOrder.DECISION_MANUAL_REVIEW
                )
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
                    "merchant_id": protected_order.merchant_id,
                    "merchant_name": protected_order.merchant.merchant_name if protected_order.merchant else "",
                    "merchant_email": protected_order.merchant.merchant_email if protected_order.merchant else "",
                    "order_id": protected_order.merchant_order_id,
                    "pa_order_id": protected_order.pa_order_id or "",
                    "pa_payment_id": _demo_payment_id(protected_order),
                    "vaultpay_order_id": protected_order.vaultpay_order_id,
                    "customer_name": serialized["customer"]["name"],
                    "customer_email": (
                        protected_order.order.customer_info.customer_email
                        if protected_order.order and protected_order.order.customer_info
                        else ""
                    ),
                    "customer_phone": serialized["customer"]["phone"],
                    "awb": (
                        protected_order.order.shipment_id.awb
                        if protected_order.order and protected_order.order.shipment_id
                        else ""
                    ),
                    "courier": (
                        protected_order.order.shipment_id.courier
                        if protected_order.order and protected_order.order.shipment_id
                        else ""
                    ),
                    "shipment_status": (
                        protected_order.order.shipment_id.status
                        if protected_order.order and protected_order.order.shipment_id
                        else ""
                    ),
                    "amount": serialized["amount"],
                    "currency": serialized["currency"],
                    "pdf_status": pdf_status,
                    "courier_status": courier_status,
                    "decision_state": protected_order.decision_state,
                    "aggregator_request": serialized.get("aggregator_request"),
                    "created_at": (
                        protected_order.order.order_date
                        if protected_order.order and protected_order.order.order_date
                        else protected_order.created_at
                    ).isoformat(),
                    "action_status": "Needs release/refund decision",
                    "can_decide": True,
                }
            )

        items.sort(key=lambda item: item.get("created_at") or "", reverse=sort_order != "oldest")
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
            order = OrderInfo.objects.filter(
                Q(merchant_order_id=vaultpay_order_id) | Q(pa_order_id=vaultpay_order_id)
            ).first()
            if order:
                protected_order = ensure_protected_order_for_order(order)
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
