import hashlib
import hmac
import json
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from payments.adapters import omniware
from payments.models import OrderInfo, PaymentLedgerEntry, RefundRequest
from payments.services.audit import create_audit_log
from payments.services.decision_history import add_decision_history
from payments.services.payment_lifecycle import transition_payment_state
from tracking.models import PdfValidationRecord, TrackingSnapshot


def build_refund_verification(order):
    shipment = order.shipment_id
    identifiers = [order.merchant_order_id, order.pa_order_id]
    if shipment and shipment.awb:
        identifiers.append(shipment.awb)

    pdf_record = (
        PdfValidationRecord.objects.filter(order_id__in=[v for v in identifiers if v])
        .order_by("-uploaded_at")
        .first()
    )
    if not pdf_record and shipment and shipment.awb:
        pdf_record = PdfValidationRecord.objects.filter(awb=shipment.awb).order_by("-uploaded_at").first()

    pdf_passed = bool(pdf_record and pdf_record.status == "approved")
    pdf_awb = (pdf_record.awb if pdf_record else "") or ""
    shipment_awb = (shipment.awb if shipment else "") or ""
    awb_passed = bool(shipment_awb and (not pdf_awb or pdf_awb == shipment_awb))

    snapshot = TrackingSnapshot.objects.filter(shipment=shipment).first() if shipment else None
    delivered = bool(
        (snapshot and snapshot.normalized_status == "DELIVERED")
        or (shipment and shipment.status == "DELIVERED")
    )

    failures = []
    if not pdf_passed:
        failures.append("PDF failed")
    if not awb_passed:
        failures.append("AWB mismatch")
    if not delivered:
        failures.append("Delivery not delivered")

    return {
        "pdf": "PASSED" if pdf_passed else "FAILED",
        "awb": "PASSED" if awb_passed else "FAILED",
        "delivery": "DELIVERED" if delivered else "NOT_DELIVERED",
        "eligible": bool(failures),
        "failures": failures,
        "pdf_record_id": pdf_record.id if pdf_record else None,
        "pdf_awb": pdf_awb,
        "shipment_awb": shipment_awb,
        "tracking_snapshot_id": snapshot.id if snapshot else None,
    }


def _provider_from_order(order):
    provider = (order.payment_provider or "").strip().lower()
    if provider:
        return provider
    if order.phonepe_payment_id or order.phonepe_order_id:
        return "phonepe"
    return "omniware"


def _provider_payment_id(order):
    return order.pa_payment_id or order.phonepe_payment_id or order.pa_order_id or ""


def _refund_status(provider_payload):
    data = provider_payload.get("data") if isinstance(provider_payload.get("data"), dict) else {}
    details = data.get("refund_details") if isinstance(data.get("refund_details"), list) else []
    latest_detail = details[-1] if details and isinstance(details[-1], dict) else {}
    status = str(
        provider_payload.get("status")
        or provider_payload.get("refund_status")
        or provider_payload.get("state")
        or provider_payload.get("Status")
        or data.get("refund_status")
        or latest_detail.get("refund_status")
        or "PENDING"
    ).upper()
    if status in {"COMPLETED", "SUCCESS", "SUCCESSFUL", "PROCESSED", "REFUNDED", "CUSTOMER REFUNDED"}:
        return RefundRequest.STATUS_COMPLETED
    if status in {"FAILED", "FAILURE", "REJECTED", "DECLINED", "ERROR"}:
        return RefundRequest.STATUS_FAILED
    return RefundRequest.STATUS_PENDING


def _refund_id(provider_payload):
    data = provider_payload.get("data") if isinstance(provider_payload.get("data"), dict) else {}
    return str(
        provider_payload.get("id")
        or provider_payload.get("refundId")
        or provider_payload.get("refund_id")
        or provider_payload.get("_id")
        or data.get("refund_id")
        or ""
    )


def _ledger_refund_once(order, refund_request):
    reference = f"refund:{refund_request.provider_refund_id or refund_request.refund_reference}"
    if PaymentLedgerEntry.objects.filter(order=order, entry_type=PaymentLedgerEntry.ENTRY_REFUND, reference=reference).exists():
        return
    PaymentLedgerEntry.objects.create(
        order=order,
        entry_type=PaymentLedgerEntry.ENTRY_REFUND,
        amount=-Decimal(str(refund_request.amount)),
        currency=refund_request.currency,
        reference=reference,
        metadata={"provider": refund_request.provider, "refund_reference": refund_request.refund_reference},
    )


def create_common_refund(order, reason=None, submit=True, actor_display="", actor_role="", request=None):
    verification = build_refund_verification(order)
    if not verification["eligible"]:
        raise ValueError("Refund is not eligible: PDF/AWB/delivery checks passed")

    provider = _provider_from_order(order)
    reason = reason or (verification["failures"][0] if verification["failures"] else "Verification failed")
    refund_reference = f"RF-{order.id}-{order.merchant_order_id}"[:30]

    with transaction.atomic():
        refund_request, created = RefundRequest.objects.get_or_create(
            order=order,
            defaults={
                "provider": provider,
                "amount": order.order_amount,
                "currency": order.order_currency,
                "reason": reason,
                "refund_reference": refund_reference,
                "provider_payment_id": _provider_payment_id(order),
                "verification_snapshot": verification,
            },
        )
        if not created and refund_request.status == RefundRequest.STATUS_COMPLETED:
            return refund_request

        old_values = {
            "status": refund_request.status,
            "reason": refund_request.reason,
            "provider_payment_id": refund_request.provider_payment_id,
            "verification_snapshot": refund_request.verification_snapshot,
            "order_status": order.order_status,
            "payment_state": order.payment_state,
        }

        refund_request.reason = reason
        refund_request.verification_snapshot = verification
        refund_request.provider_payment_id = refund_request.provider_payment_id or _provider_payment_id(order)
        refund_request.save(update_fields=["reason", "verification_snapshot", "provider_payment_id", "updated_at"])

        if order.payment_state != "REFUND_PENDING":
            transition_payment_state(
                order,
                "REFUND_PENDING",
                "refund_request",
                {"provider": provider, "refund_reference": refund_request.refund_reference, "reason": reason},
            )
        order.order_status = "refund_pending"
        order.save(update_fields=["order_status"])

        create_audit_log(
            case_type="refund",
            case_id=refund_request.refund_reference,
            action="refund_requested" if created else "refund_request_updated",
            actor_display=actor_display,
            actor_role=actor_role,
            remarks=reason,
            old_values={} if created else old_values,
            new_values={
                "status": refund_request.status,
                "reason": refund_request.reason,
                "provider": refund_request.provider,
                "amount": refund_request.amount,
                "currency": refund_request.currency,
                "provider_payment_id": refund_request.provider_payment_id,
                "verification_snapshot": refund_request.verification_snapshot,
                "order_status": order.order_status,
                "payment_state": order.payment_state,
            },
            metadata={"order_id": order.id, "merchant_order_id": order.merchant_order_id},
            source="payments.refunds",
            request=request,
        )
        add_decision_history(
            case_type="refund",
            case_id=refund_request.refund_reference,
            order_id=order.merchant_order_id,
            status=refund_request.status.lower(),
            title="Refund Requested" if created else "Refund Request Updated",
            remarks=reason,
            actor_display=actor_display,
            actor_role=actor_role,
            metadata={
                "order_status": order.order_status,
                "payment_state": order.payment_state,
                "provider": provider,
            },
        )

    if submit:
        submit_refund_to_provider(refund_request)
        refund_request.refresh_from_db()
    return refund_request


def submit_refund_to_provider(refund_request):
    adapter = getattr(settings, "PAYMENT_REFUND_ADAPTER", "omniware")
    if adapter != "omniware":
        raise ValueError(f"No refund adapter configured for {adapter}")

    try:
        provider_request, provider_response = omniware.create_refund(refund_request)
    except Exception as exc:
        old_values = {"status": refund_request.status, "error": refund_request.error}
        refund_request.status = RefundRequest.STATUS_FAILED
        refund_request.error = str(exc)
        refund_request.save(update_fields=["status", "error", "updated_at"])
        create_audit_log(
            case_type="refund",
            case_id=refund_request.refund_reference,
            action="refund_submit_failed",
            actor_display="system",
            actor_role="System",
            remarks=str(exc),
            old_values=old_values,
            new_values={"status": refund_request.status, "error": refund_request.error},
            metadata={"order_id": refund_request.order_id},
            source="payments.refunds",
        )
        add_decision_history(
            case_type="refund",
            case_id=refund_request.refund_reference,
            order_id=refund_request.order.merchant_order_id,
            status=refund_request.status.lower(),
            title="Refund Failed",
            remarks=str(exc),
            actor_display="system",
            actor_role="System",
            metadata={"provider": refund_request.provider},
        )
        raise

    old_values = {
        "status": refund_request.status,
        "provider_refund_id": refund_request.provider_refund_id,
        "provider_request": refund_request.provider_request,
        "provider_response": refund_request.provider_response,
        "error": refund_request.error,
    }
    refund_request.provider_request = provider_request
    refund_request.provider_response = provider_response
    refund_request.provider_refund_id = _refund_id(provider_response)
    refund_request.status = _refund_status(provider_response)
    refund_request.provider_submitted_at = timezone.now()
    refund_request.error = ""
    update_fields = [
        "provider_request",
        "provider_response",
        "provider_refund_id",
        "status",
        "provider_submitted_at",
        "error",
        "updated_at",
    ]
    if refund_request.status == RefundRequest.STATUS_COMPLETED:
        refund_request.completed_at = timezone.now()
        update_fields.append("completed_at")
    refund_request.save(update_fields=update_fields)
    create_audit_log(
        case_type="refund",
        case_id=refund_request.refund_reference,
        action="refund_submitted_to_provider",
        actor_display="system",
        actor_role="System",
        remarks=f"Submitted to {refund_request.provider}",
        old_values=old_values,
        new_values={
            "status": refund_request.status,
            "provider_refund_id": refund_request.provider_refund_id,
            "provider_request": refund_request.provider_request,
            "provider_response": refund_request.provider_response,
            "error": refund_request.error,
            "provider_submitted_at": refund_request.provider_submitted_at,
            "completed_at": refund_request.completed_at,
        },
        metadata={"order_id": refund_request.order_id},
        source="payments.refunds",
    )
    add_decision_history(
        case_type="refund",
        case_id=refund_request.refund_reference,
        order_id=refund_request.order.merchant_order_id,
        status=refund_request.status.lower(),
        title="Refund Submitted To Provider",
        remarks=f"Submitted to {refund_request.provider}",
        actor_display="system",
        actor_role="System",
        metadata={
            "provider": refund_request.provider,
            "provider_refund_id": refund_request.provider_refund_id,
        },
    )

    if refund_request.status == RefundRequest.STATUS_COMPLETED:
        complete_refund(refund_request, provider_response)


def verify_omniware_webhook(raw_body, signature):
    secret = getattr(settings, "OMNIWARE_WEBHOOK_SECRET", "")
    if not secret:
        return bool(getattr(settings, "PAYMENT_WEBHOOK_ALLOW_UNSIGNED_TEST_EVENTS", False))
    supplied = (signature or "").strip()
    if supplied.lower().startswith("sha256="):
        supplied = supplied.split("=", 1)[1]
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, digest)


def process_refund_webhook(provider, raw_body, signature=""):
    if provider != "omniware":
        raise ValueError(f"Unsupported refund webhook provider: {provider}")
    if not verify_omniware_webhook(raw_body, signature):
        raise PermissionError("Invalid refund webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except ValueError as exc:
        raise ValueError("Invalid refund webhook JSON") from exc

    original = payload.get("originalResponse") if isinstance(payload.get("originalResponse"), dict) else payload
    provider_refund_id = _refund_id(original)
    reference = str(original.get("referenceId") or original.get("refund_reference") or "")

    refund_request = None
    if provider_refund_id:
        refund_request = RefundRequest.objects.filter(provider_refund_id=provider_refund_id).first()
    if not refund_request and reference:
        refund_request = RefundRequest.objects.filter(refund_reference=reference).first()
    if not refund_request:
        return {"ok": True, "unknown_refund": True}

    status = _refund_status(original)
    old_values = {
        "status": refund_request.status,
        "provider_refund_id": refund_request.provider_refund_id,
        "provider_response": refund_request.provider_response,
        "completed_at": refund_request.completed_at,
    }
    refund_request.provider_response = payload
    refund_request.status = status
    if provider_refund_id:
        refund_request.provider_refund_id = provider_refund_id
    update_fields = ["provider_response", "status", "provider_refund_id", "updated_at"]
    if status == RefundRequest.STATUS_COMPLETED:
        refund_request.completed_at = timezone.now()
        update_fields.append("completed_at")
    refund_request.save(update_fields=update_fields)
    create_audit_log(
        case_type="refund",
        case_id=refund_request.refund_reference,
        action="refund_webhook_received",
        actor_display=f"{provider} webhook",
        actor_role="System",
        remarks=f"Provider refund status: {status}",
        old_values=old_values,
        new_values={
            "status": refund_request.status,
            "provider_refund_id": refund_request.provider_refund_id,
            "provider_response": refund_request.provider_response,
            "completed_at": refund_request.completed_at,
        },
        metadata={"order_id": refund_request.order_id, "provider": provider},
        source="payments.refund_webhook",
    )
    add_decision_history(
        case_type="refund",
        case_id=refund_request.refund_reference,
        order_id=refund_request.order.merchant_order_id,
        status=refund_request.status.lower(),
        title="Refund Status Updated",
        remarks=f"Provider refund status: {status}",
        actor_display=f"{provider} webhook",
        actor_role="System",
        metadata={
            "provider": provider,
            "provider_refund_id": refund_request.provider_refund_id,
        },
    )

    if status == RefundRequest.STATUS_COMPLETED:
        complete_refund(refund_request, payload)

    return {"ok": True, "refund_reference": refund_request.refund_reference, "status": refund_request.status}


def complete_refund(refund_request, metadata=None):
    order = refund_request.order
    old_values = {
        "order_status": order.order_status,
        "payment_state": order.payment_state,
    }
    if order.payment_state != "REFUNDED":
        if order.payment_state != "REFUND_PENDING":
            transition_payment_state(order, "REFUND_PENDING", "refund_webhook", {"provider": refund_request.provider})
        transition_payment_state(
            order,
            "REFUNDED",
            "refund_webhook",
            {
                "provider": refund_request.provider,
                "refund_reference": refund_request.refund_reference,
                "provider_refund_id": refund_request.provider_refund_id,
            },
        )
    order.order_status = "refunded"
    order.save(update_fields=["order_status"])
    _ledger_refund_once(order, refund_request)
    create_audit_log(
        case_type="refund",
        case_id=refund_request.refund_reference,
        action="refund_completed",
        actor_display="system",
        actor_role="System",
        remarks="Refund marked completed and ledger updated",
        old_values=old_values,
        new_values={
            "order_status": order.order_status,
            "payment_state": order.payment_state,
            "provider_refund_id": refund_request.provider_refund_id,
        },
        metadata={"order_id": order.id, "provider_metadata": metadata or {}},
        source="payments.refunds",
    )
    add_decision_history(
        case_type="refund",
        case_id=refund_request.refund_reference,
        order_id=order.merchant_order_id,
        status="refunded",
        title="Refund Completed",
        remarks="Refund marked completed and ledger updated.",
        actor_display="system",
        actor_role="System",
        metadata={
            "order_status": order.order_status,
            "payment_state": order.payment_state,
            "provider_refund_id": refund_request.provider_refund_id,
        },
    )
