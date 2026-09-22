from decimal import Decimal
from uuid import uuid4

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.conf import settings
from django.utils import timezone

from payments.adapters import get_pa_adapter, list_adapter_capabilities
from payments.models import (
    CustomerInfo,
    OrderInfo,
    PAAuditEntry,
    PACanonicalEvent,
    PAFinancialCommand,
    PaymentNotification,
    PAProviderCapability,
    PAProtectedOrder,
    PAReconciliationRecord,
)
from tracking.models import PdfValidationRecord
from tracking.models.delhivery_verification_decision import DelhiveryVerificationDecision


PDF_UPLOAD_REFUND_DEADLINE_DAYS = 21


def ensure_default_capabilities():
    for item in list_adapter_capabilities():
        PAProviderCapability.objects.update_or_create(provider=item["provider"], defaults=item)


def audit(protected_order, event_type, message, details=None):
    return PAAuditEntry.objects.create(
        protected_order=protected_order,
        event_type=event_type,
        message=message,
        details=details or {},
    )


def _requested_financial_decision(protected_order):
    request_info = (protected_order.metadata or {}).get("aggregator_request") or {}
    decision = request_info.get("decision")
    if decision in {PAProtectedOrder.DECISION_RELEASE, PAProtectedOrder.DECISION_REFUND}:
        return decision
    return ""


def send_aggregator_request(protected_order, decision, actor=None):
    decision = str(decision or "").upper()
    if decision not in {PAProtectedOrder.DECISION_RELEASE, PAProtectedOrder.DECISION_REFUND}:
        raise ValueError("decision must be RELEASE or REFUND")

    now = timezone.now()
    metadata = dict(protected_order.metadata or {})
    metadata["aggregator_request"] = {
        "decision": decision,
        "status": "SENT",
        "requested_at": now.isoformat(),
        "requested_by": getattr(actor, "username", "") if actor else "",
        "endpoint": f"/aggregator/{decision.lower()}/",
        "demo_only": True,
    }
    protected_order.metadata = metadata
    protected_order.decision_state = decision
    protected_order.save(update_fields=["metadata", "decision_state", "updated_at"])
    audit(
        protected_order,
        "AGGREGATOR_REQUEST_SENT",
        f"{decision} request sent to aggregator",
        metadata["aggregator_request"],
    )
    prepare_financial_command(protected_order)
    return protected_order


def _pdf_missing_refund_due(protected_order, pdf_evidence):
    if pdf_evidence.get("status") not in {"NOT_UPLOADED", "NOT_FOUND"}:
        return False
    order_date = getattr(protected_order.order, "order_date", None)
    if not order_date:
        return False
    return order_date + timezone.timedelta(days=PDF_UPLOAD_REFUND_DEADLINE_DAYS) <= timezone.now()


def auto_refund_overdue_missing_pdf(protected_order, pdf_evidence=None):
    pdf_evidence = pdf_evidence or get_pdf_evidence(protected_order)
    if not _pdf_missing_refund_due(protected_order, pdf_evidence):
        return False

    metadata = dict(protected_order.metadata or {})
    if metadata.get("auto_missing_pdf_refund", {}).get("status") == "REFUND_REQUEST_SENT":
        return False

    metadata["auto_missing_pdf_refund"] = {
        "status": "STARTED",
        "reason": f"Shipment label PDF missing for {PDF_UPLOAD_REFUND_DEADLINE_DAYS} days",
        "pdf_status": pdf_evidence.get("status"),
        "started_at": timezone.now().isoformat(),
    }
    protected_order.metadata = metadata
    protected_order.decision_state = PAProtectedOrder.DECISION_REFUND
    protected_order.save(update_fields=["metadata", "decision_state", "updated_at"])
    audit(
        protected_order,
        "AUTO_REFUND_MISSING_PDF",
        f"Auto refund triggered because PDF label was missing after {PDF_UPLOAD_REFUND_DEADLINE_DAYS} days",
        metadata["auto_missing_pdf_refund"],
    )

    try:
        send_aggregator_request(protected_order, PAProtectedOrder.DECISION_REFUND)
        metadata = dict(protected_order.metadata or {})
        metadata["auto_missing_pdf_refund"] = {
            **metadata.get("auto_missing_pdf_refund", {}),
            "status": "REFUND_REQUEST_SENT",
            "requested_at": timezone.now().isoformat(),
        }
        protected_order.metadata = metadata
        protected_order.save(update_fields=["metadata", "updated_at"])
    except ValueError as exc:
        metadata = dict(protected_order.metadata or {})
        metadata["auto_missing_pdf_refund"] = {
            **metadata.get("auto_missing_pdf_refund", {}),
            "status": "FAILED",
            "error": str(exc),
            "failed_at": timezone.now().isoformat(),
        }
        protected_order.metadata = metadata
        protected_order.decision_state = PAProtectedOrder.DECISION_MANUAL_REVIEW
        protected_order.save(update_fields=["metadata", "decision_state", "updated_at"])
        audit(protected_order, "AUTO_REFUND_MISSING_PDF_FAILED", str(exc), metadata["auto_missing_pdf_refund"])
        return False

    return True


def prepare_financial_command(protected_order):
    requested_decision = _requested_financial_decision(protected_order) or protected_order.decision_state
    if requested_decision not in {PAProtectedOrder.DECISION_RELEASE, PAProtectedOrder.DECISION_REFUND}:
        raise ValueError("decision must be RELEASE or REFUND before evidence can be prepared")

    existing = getattr(protected_order, "financial_command", None)
    if existing:
        if existing.status == PAFinancialCommand.STATUS_NOT_SENT and existing.decision != requested_decision:
            existing.decision = requested_decision
            existing.reason_code = (
                "DELIVERY_FAILED"
                if requested_decision == PAProtectedOrder.DECISION_REFUND
                else "DELIVERY_CONFIRMED"
            )
            existing.idempotency_key = f"{protected_order.vaultpay_order_id}:{requested_decision}:v1"
            existing.save(update_fields=["decision", "reason_code", "idempotency_key", "updated_at"])
        return existing

    evidence_token = f"ev_{uuid4().hex}"
    evidence_base_url = getattr(settings, "PA_EVIDENCE_REPORT_BASE_URL", "http://127.0.0.1:5174").rstrip("/")
    evidence_report_url = f"{evidence_base_url}/pa-evidence/{evidence_token}"
    return PAFinancialCommand.objects.create(
        protected_order=protected_order,
        command_id=f"vp_cmd_{uuid4().hex[:14]}",
        decision=requested_decision,
        amount_minor=protected_order.amount_minor,
        currency=protected_order.currency,
        reason_code="DELIVERY_FAILED" if requested_decision == PAProtectedOrder.DECISION_REFUND else "DELIVERY_CONFIRMED",
        idempotency_key=f"{protected_order.vaultpay_order_id}:{requested_decision}:v1",
        provider_reference=protected_order.pa_payment_id or protected_order.pa_order_id,
        evidence_token=evidence_token,
        evidence_report_url=evidence_report_url,
        status=PAFinancialCommand.STATUS_NOT_SENT,
    )


def _minor_to_major(amount_minor):
    return Decimal(str(amount_minor)) / Decimal("100")


def _money_to_minor(amount):
    return int((Decimal(str(amount)) * Decimal("100")).quantize(Decimal("1")))


def serialize_capability(item):
    return {
        "provider": item.provider,
        "display_name": item.display_name,
        "supports_per_order_hold": item.supports_per_order_hold,
        "supports_release": item.supports_release,
        "supports_refund_before_settlement": item.supports_refund_before_settlement,
        "supports_refund_after_settlement": item.supports_refund_after_settlement,
        "supports_webhooks": item.supports_webhooks,
        "supports_idempotency": item.supports_idempotency,
        "supports_status_lookup": item.supports_status_lookup,
        "supports_reconciliation": item.supports_reconciliation,
        "max_hold_days": item.max_hold_days,
        "manual_review_required": item.manual_review_required,
    }


def get_capability(provider):
    ensure_default_capabilities()
    provider = provider or "mock_hold_pa"
    if not PAProviderCapability.objects.filter(provider=provider).exists():
        provider = "mock_hold_pa"
    return PAProviderCapability.objects.get(provider=provider)


def _safe_pa_provider(provider):
    ensure_default_capabilities()
    provider = provider or "mock_hold_pa"
    if PAProviderCapability.objects.filter(provider=provider).exists():
        return provider
    return "mock_hold_pa"


def _order_payment_state(order):
    state = (order.payment_state or "").upper()
    status_text = (order.order_status or "").lower()
    if state in {"FUNDS_HELD", "PAYMENT_HELD"}:
        return PAProtectedOrder.PAYMENT_HELD
    if state in {"PAYMENT_CAPTURED", "SETTLEMENT_ELIGIBLE", "SETTLEMENT_PROCESSING", "SETTLED"}:
        return PAProtectedOrder.PAYMENT_SUCCEEDED
    if state in {"PAYMENT_FAILED"} or status_text == "payment_failed":
        return PAProtectedOrder.PAYMENT_FAILED
    if state in {"REFUNDED"} or status_text == "refunded":
        return PAProtectedOrder.PAYMENT_SUCCEEDED
    return PAProtectedOrder.PAYMENT_CREATED


@transaction.atomic
def ensure_protected_order_for_order(order):
    ensure_default_capabilities()
    provider = _safe_pa_provider(order.payment_provider)
    capability = get_capability(provider)
    amount_minor = _money_to_minor(order.order_amount)
    protected_order, created = PAProtectedOrder.objects.get_or_create(
        order=order,
        defaults={
            "vaultpay_order_id": f"vp_ord_{uuid4().hex[:14]}",
            "merchant": order.merchant,
            "merchant_order_id": order.merchant_order_id,
            "pa_provider": provider,
            "pa_account_id": "pa_account_live",
            "pa_order_id": order.pa_order_id or "",
            "pa_payment_id": order.pa_payment_id or "",
            "amount_minor": amount_minor,
            "currency": order.order_currency,
            "protection_policy": "DELIVERY_CONFIRMATION",
            "payment_state": _order_payment_state(order),
            "protection_state": (
                PAProtectedOrder.PROTECTION_PROTECTED
                if _order_payment_state(order) in {PAProtectedOrder.PAYMENT_HELD, PAProtectedOrder.PAYMENT_SUCCEEDED}
                else PAProtectedOrder.PROTECTION_PENDING
            ),
            "expires_at": timezone.now() + timezone.timedelta(days=max(capability.max_hold_days or 1, 1)),
            "metadata": {
                "source": "admin_order",
                "capability": serialize_capability(capability),
            },
        },
    )
    if created:
        PAReconciliationRecord.objects.create(protected_order=protected_order)
        audit(protected_order, "ORDER_LINKED", "Real order linked to PA control", {"order_id": order.id})
    else:
        changed = []
        next_payment_state = _order_payment_state(order)
        if protected_order.payment_state != next_payment_state:
            protected_order.payment_state = next_payment_state
            changed.append("payment_state")
        if protected_order.pa_order_id != (order.pa_order_id or ""):
            protected_order.pa_order_id = order.pa_order_id or ""
            changed.append("pa_order_id")
        if protected_order.pa_payment_id != (order.pa_payment_id or ""):
            protected_order.pa_payment_id = order.pa_payment_id or ""
            changed.append("pa_payment_id")
        if changed:
            changed.append("updated_at")
            protected_order.save(update_fields=changed)
    evaluate_decision(protected_order)
    return protected_order


def sync_recent_orders_to_pa(limit=100):
    rows = (
        OrderInfo.objects.select_related("merchant", "customer_info", "shipment_id")
        .order_by("-order_date")[:limit]
    )
    return [ensure_protected_order_for_order(order) for order in rows]


@transaction.atomic
def register_protected_order(payload):
    ensure_default_capabilities()
    provider = payload.get("pa_provider") or payload.get("provider") or "mock_hold_pa"
    adapter = get_pa_adapter(provider)
    capability = get_capability(provider)
    merchant = payload["merchant"]
    merchant_order_id = str(payload.get("merchant_order_id") or "").strip()
    if not merchant_order_id:
        merchant_order_id = f"shop_{uuid4().hex[:10]}"

    amount_minor = payload.get("amount_minor")
    if amount_minor is None:
        amount_minor = _money_to_minor(payload.get("amount") or "0")
    if amount_minor <= 0:
        raise ValueError("amount must be greater than 0")

    provider_reference = adapter.create_or_register_protected_order(
        {
            **payload,
            "merchant_order_id": merchant_order_id,
            "currency": payload.get("currency") or "INR",
            "amount_minor": amount_minor,
        }
    )

    order = OrderInfo.objects.filter(merchant=merchant, merchant_order_id=merchant_order_id).first()
    if not order:
        customer, _ = CustomerInfo.objects.update_or_create(
            customer_phone=str(payload.get("customer_phone") or "9999999999"),
            defaults={
                "customer_name": str(payload.get("customer_name") or "Demo Customer"),
                "customer_email": str(payload.get("customer_email") or ""),
                "customer_address": str(payload.get("customer_address") or "Not provided"),
            },
        )
        order = OrderInfo.objects.create(
            merchant=merchant,
            merchant_order_id=merchant_order_id,
            pa_order_id=provider_reference.pa_order_id,
            pa_payment_id=provider_reference.pa_payment_id,
            order_amount=_minor_to_major(amount_minor),
            order_currency=str(payload.get("currency") or "INR"),
            order_status="created",
            customer_info=customer,
            payment_provider=provider,
        )

    protected_order, created = PAProtectedOrder.objects.get_or_create(
        merchant=merchant,
        merchant_order_id=merchant_order_id,
        defaults={
            "vaultpay_order_id": f"vp_ord_{uuid4().hex[:14]}",
            "order": order,
            "pa_provider": provider,
            "pa_account_id": provider_reference.pa_account_id,
            "pa_order_id": order.pa_order_id or provider_reference.pa_order_id,
            "pa_payment_id": order.pa_payment_id or provider_reference.pa_payment_id,
            "amount_minor": amount_minor,
            "currency": order.order_currency,
            "protection_policy": str(payload.get("protection_policy") or "DELIVERY_CONFIRMATION"),
            "protection_state": (
                PAProtectedOrder.PROTECTION_PENDING
                if capability.supports_per_order_hold
                else PAProtectedOrder.PROTECTION_UNSUPPORTED
            ),
            "expires_at": timezone.now() + timezone.timedelta(days=max(capability.max_hold_days or 1, 1)),
            "metadata": {"capability": serialize_capability(capability)},
        },
    )
    if not created:
        return protected_order

    PAReconciliationRecord.objects.create(protected_order=protected_order)
    audit(
        protected_order,
        "ORDER_REGISTERED",
        "Protected order registered",
        {"provider": provider, "capability": serialize_capability(capability)},
    )
    if not capability.supports_per_order_hold:
        audit(
            protected_order,
            "CAPABILITY_BLOCK",
            "Provider cannot confirm per-order hold; route to manual review",
            {"provider": provider},
        )
        evaluate_decision(protected_order)
    return protected_order


def ingest_mock_event(protected_order, event_type, signature_valid=True, pa_event_id=None):
    adapter_provider = _safe_pa_provider(protected_order.pa_provider)
    adapter = get_pa_adapter(adapter_provider)
    normalized = adapter.normalize_webhook(
        {
            "provider": adapter_provider,
            "event_type": event_type,
            "pa_event_id": pa_event_id,
            "pa_order_id": protected_order.pa_order_id,
            "pa_payment_id": protected_order.pa_payment_id,
            "amount_minor": protected_order.amount_minor,
            "currency": protected_order.currency,
            "signature_valid": signature_valid,
        }
    )[0]
    try:
        event = PACanonicalEvent.objects.create(
            protected_order=protected_order,
            event_id=f"vp_evt_{uuid4().hex[:14]}",
            pa_provider=protected_order.pa_provider,
            pa_event_id=normalized.pa_event_id,
            event_type=normalized.event_type,
            amount_minor=normalized.amount_minor,
            currency=normalized.currency,
            raw_payload=normalized.raw_payload,
            signature_valid=normalized.signature_valid,
            status=PACanonicalEvent.STATUS_PROCESSED if normalized.signature_valid else PACanonicalEvent.STATUS_REJECTED,
            error=normalized.error,
        )
    except IntegrityError:
        audit(protected_order, "WEBHOOK_DUPLICATE", f"Duplicate {event_type} ignored", {"pa_event_id": normalized.pa_event_id})
        return protected_order

    if not normalized.signature_valid:
        audit(protected_order, "WEBHOOK_REJECTED", f"{event_type} rejected", {"reason": "Invalid signature"})
        return protected_order

    if event_type == "PAYMENT_CREATED":
        protected_order.payment_state = PAProtectedOrder.PAYMENT_CREATED
    elif event_type == "PAYMENT_SUCCEEDED":
        protected_order.payment_state = PAProtectedOrder.PAYMENT_SUCCEEDED
        if protected_order.order:
            protected_order.order.order_status = "paid"
            protected_order.order.payment_state = "PAYMENT_CAPTURED"
            protected_order.order.save(update_fields=["order_status", "payment_state"])
    elif event_type == "PAYMENT_FAILED":
        protected_order.payment_state = PAProtectedOrder.PAYMENT_FAILED
        if protected_order.order:
            protected_order.order.order_status = "payment_failed"
            protected_order.order.payment_state = "PAYMENT_FAILED"
            protected_order.order.save(update_fields=["order_status", "payment_state"])
    elif event_type == "PAYMENT_HELD":
        protected_order.payment_state = PAProtectedOrder.PAYMENT_HELD
        protected_order.protection_state = PAProtectedOrder.PROTECTION_PROTECTED
        if protected_order.order:
            protected_order.order.payment_state = "FUNDS_HELD"
            protected_order.order.save(update_fields=["payment_state"])
    elif event_type == "PAYMENT_EXPIRED":
        protected_order.payment_state = PAProtectedOrder.PAYMENT_EXPIRED
        protected_order.protection_state = PAProtectedOrder.PROTECTION_EXPIRED
    elif event_type in {"RELEASE_ACCEPTED", "REFUND_ACCEPTED"}:
        protected_order.pa_command_state = PAProtectedOrder.COMMAND_ACCEPTED
    elif event_type == "RELEASE_COMPLETED":
        protected_order.pa_command_state = PAProtectedOrder.COMMAND_COMPLETED
        if protected_order.order:
            protected_order.order.payment_state = "SETTLED"
            protected_order.order.order_status = "settled"
            protected_order.order.save(update_fields=["payment_state", "order_status"])
    elif event_type == "REFUND_COMPLETED":
        protected_order.pa_command_state = PAProtectedOrder.COMMAND_COMPLETED
        if protected_order.order:
            protected_order.order.payment_state = "REFUNDED"
            protected_order.order.order_status = "refunded"
            protected_order.order.save(update_fields=["payment_state", "order_status"])
    elif event_type in {"REFUND_FAILED", "RELEASE_FAILED"}:
        protected_order.pa_command_state = PAProtectedOrder.COMMAND_REJECTED

    protected_order.save()
    audit(protected_order, "WEBHOOK_PROCESSED", f"{event_type} processed", {"event_id": event.event_id})
    evaluate_decision(protected_order)
    return protected_order


def update_delivery_state(protected_order, fulfilment_state=None, confirmation_state=None):
    if fulfilment_state:
        protected_order.fulfilment_state = fulfilment_state
        audit(protected_order, "FULFILMENT_UPDATED", f"Fulfilment moved to {fulfilment_state}")
    if confirmation_state:
        protected_order.confirmation_state = confirmation_state
        audit(protected_order, "CUSTOMER_CONFIRMATION", f"Confirmation moved to {confirmation_state}")
    protected_order.save()
    evaluate_decision(protected_order)
    return protected_order


def evaluate_decision(protected_order):
    previous = protected_order.decision_state
    capability = get_capability(protected_order.pa_provider)
    pdf_evidence = get_pdf_evidence(protected_order)
    courier_decision = _latest_courier_decision(protected_order)
    requested_decision = _requested_financial_decision(protected_order)

    if requested_decision:
        protected_order.decision_state = requested_decision
    elif auto_refund_overdue_missing_pdf(protected_order, pdf_evidence):
        protected_order.refresh_from_db()
    elif pdf_evidence.get("status") in {"NOT_APPROVED", "NOT_UPLOADED", "NOT_FOUND", "PROCESSING"}:
        protected_order.decision_state = PAProtectedOrder.DECISION_MANUAL_REVIEW
    elif courier_decision in {"", DelhiveryVerificationDecision.NOT_VERIFIED}:
        protected_order.decision_state = PAProtectedOrder.DECISION_MANUAL_REVIEW
    elif (
        pdf_evidence.get("status") == "APPROVED"
        and courier_decision == DelhiveryVerificationDecision.VERIFIED
    ):
        protected_order.decision_state = PAProtectedOrder.DECISION_RELEASE
    elif protected_order.payment_state in {PAProtectedOrder.PAYMENT_FAILED, PAProtectedOrder.PAYMENT_EXPIRED}:
        protected_order.decision_state = PAProtectedOrder.DECISION_MANUAL_REVIEW
    elif not capability.supports_per_order_hold:
        protected_order.decision_state = PAProtectedOrder.DECISION_MANUAL_REVIEW
    elif protected_order.fulfilment_state in {
        PAProtectedOrder.FULFILMENT_FAILED,
        PAProtectedOrder.FULFILMENT_RETURNED,
    }:
        protected_order.decision_state = PAProtectedOrder.DECISION_REFUND
    elif (
        protected_order.fulfilment_state == PAProtectedOrder.FULFILMENT_DELIVERED
        and protected_order.confirmation_state
        in {PAProtectedOrder.CONFIRMATION_CONFIRMED, PAProtectedOrder.CONFIRMATION_EXPIRED}
    ):
        protected_order.decision_state = PAProtectedOrder.DECISION_RELEASE
    elif protected_order.payment_state == PAProtectedOrder.PAYMENT_HELD:
        protected_order.decision_state = PAProtectedOrder.DECISION_PENDING
    else:
        protected_order.decision_state = PAProtectedOrder.DECISION_PENDING

    protected_order.save(update_fields=["decision_state", "updated_at"])
    if previous != protected_order.decision_state:
        audit(
            protected_order,
            "DECISION_EVALUATED",
            f"Decision moved to {protected_order.decision_state}",
            {
                "previous": previous,
                "payment_state": protected_order.payment_state,
                "fulfilment_state": protected_order.fulfilment_state,
                "confirmation_state": protected_order.confirmation_state,
                "pdf_verification": pdf_evidence.get("status"),
                "courier_verification": courier_decision,
            },
        )
    return protected_order


def _latest_courier_decision(protected_order):
    order = protected_order.order
    if not order or not order.shipment_id or not order.shipment_id.awb:
        return ""
    decision = DelhiveryVerificationDecision.objects.filter(awb=order.shipment_id.awb).first()
    return decision.decision if decision else ""


def dispatch_financial_command(protected_order):
    evaluate_decision(protected_order)
    requested_decision = _requested_financial_decision(protected_order)
    if requested_decision:
        protected_order.decision_state = requested_decision
        protected_order.save(update_fields=["decision_state", "updated_at"])
    if protected_order.decision_state not in {
        PAProtectedOrder.DECISION_RELEASE,
        PAProtectedOrder.DECISION_REFUND,
    }:
        raise ValueError("decision must be RELEASE or REFUND before dispatch")

    capability = get_capability(protected_order.pa_provider)
    if protected_order.decision_state == PAProtectedOrder.DECISION_RELEASE and not capability.supports_release:
        protected_order.decision_state = PAProtectedOrder.DECISION_MANUAL_REVIEW
        protected_order.save(update_fields=["decision_state", "updated_at"])
        audit(protected_order, "COMMAND_BLOCKED", "Release blocked by provider capability")
        raise ValueError("provider does not support release")

    if (
        protected_order.decision_state == PAProtectedOrder.DECISION_REFUND
        and protected_order.payment_state == PAProtectedOrder.PAYMENT_HELD
        and not capability.supports_refund_before_settlement
    ):
        protected_order.decision_state = PAProtectedOrder.DECISION_MANUAL_REVIEW
        protected_order.save(update_fields=["decision_state", "updated_at"])
        audit(protected_order, "COMMAND_BLOCKED", "Refund before settlement blocked by provider capability")
        raise ValueError("provider does not support refund before settlement")

    existing = getattr(protected_order, "financial_command", None)
    if existing and existing.status != PAFinancialCommand.STATUS_NOT_SENT:
        return existing

    decision = protected_order.decision_state
    command = existing or prepare_financial_command(protected_order)
    adapter = get_pa_adapter(_safe_pa_provider(protected_order.pa_provider))
    command_payload = {
        "decision": decision,
        "amount_minor": protected_order.amount_minor,
        "currency": protected_order.currency,
        "idempotency_key": f"{protected_order.vaultpay_order_id}:{decision}:v1",
        "provider_reference": protected_order.pa_payment_id or protected_order.pa_order_id,
        "evidence_report_url": command.evidence_report_url,
    }
    provider_result = (
        adapter.execute_refund(command_payload)
        if decision == PAProtectedOrder.DECISION_REFUND
        else adapter.execute_release(command_payload)
    )
    command.status = provider_result.status
    command.provider_request = provider_result.provider_request
    command.provider_response = provider_result.provider_response
    command.attempts = [{"at": timezone.now().isoformat(), "status": provider_result.status, "transport": provider_result.transport}]
    command.save(update_fields=["status", "provider_request", "provider_response", "attempts", "updated_at"])
    protected_order.pa_command_state = PAProtectedOrder.COMMAND_PENDING
    protected_order.save(update_fields=["pa_command_state", "updated_at"])
    audit(protected_order, "COMMAND_DISPATCHED", f"{decision} command dispatched", {"command_id": command.command_id})
    return command


def apply_provider_command_result(protected_order, result):
    command = getattr(protected_order, "financial_command", None)
    if not command:
        raise ValueError("dispatch a command before applying provider result")

    attempts = list(command.attempts or [])
    attempts.insert(0, {"at": timezone.now().isoformat(), "status": result, "transport": "mock-provider"})
    command.attempts = attempts
    command.provider_response = {"status": result, "provider": protected_order.pa_provider}
    adapter = get_pa_adapter(_safe_pa_provider(protected_order.pa_provider))

    if result == PAFinancialCommand.STATUS_ACCEPTED:
        command.status = PAFinancialCommand.STATUS_ACCEPTED
        command.save()
        event_type = adapter.provider_result_event(command, result)
        ingest_mock_event(protected_order, event_type)
        notify_merchant_financial_command(command)
    elif result == PAFinancialCommand.STATUS_COMPLETED:
        command.status = PAFinancialCommand.STATUS_COMPLETED
        command.completed_at = timezone.now()
        command.save()
        event_type = adapter.provider_result_event(command, result)
        ingest_mock_event(protected_order, event_type)
    elif result == PAFinancialCommand.STATUS_REJECTED:
        command.status = PAFinancialCommand.STATUS_REJECTED
        command.save()
        protected_order.pa_command_state = PAProtectedOrder.COMMAND_REJECTED
        protected_order.save(update_fields=["pa_command_state", "updated_at"])
        audit(protected_order, "COMMAND_REJECTED", "Provider rejected command", {"command_id": command.command_id})
    else:
        command.status = PAFinancialCommand.STATUS_UNKNOWN
        command.save()
        protected_order.pa_command_state = PAProtectedOrder.COMMAND_UNKNOWN
        protected_order.save(update_fields=["pa_command_state", "updated_at"])
        audit(protected_order, "COMMAND_UNKNOWN", "Provider outcome unknown; reconciliation required")
    return command


def notify_merchant_financial_command(command):
    protected_order = command.protected_order
    order = protected_order.order
    if not order or not order.merchant:
        return None

    if command.decision == PAFinancialCommand.DECISION_RELEASE:
        event_type = "pa_release_accepted"
        title = "Release amount credited"
        message = (
            f"Release accepted for order {order.merchant_order_id}. "
            f"{order.order_currency} {order.order_amount} will be credited to your merchant settlement."
        )
    else:
        event_type = "pa_refund_accepted"
        title = "Order cancelled and refund accepted"
        message = (
            f"Refund accepted for order {order.merchant_order_id}. "
            "The order was cancelled because verification checks did not pass."
        )

    return PaymentNotification.objects.get_or_create(
        dedupe_key=f"DASHBOARD:{event_type}:{command.command_id}",
        defaults={
            "merchant": order.merchant,
            "order": order,
            "event_type": event_type,
            "channel": PaymentNotification.CHANNEL_DASHBOARD,
            "title": title,
            "message": message,
            "status": PaymentNotification.STATUS_SENT,
            "sent_at": timezone.now(),
            "metadata": {
                "command_id": command.command_id,
                "decision": command.decision,
                "reason": decision_reason(protected_order),
            },
        },
    )[0]


def run_reconciliation(protected_order, force_mismatch=False):
    adapter = get_pa_adapter(_safe_pa_provider(protected_order.pa_provider))
    command = getattr(protected_order, "financial_command", None)
    result = adapter.reconcile(protected_order, command=command, force_mismatch=force_mismatch)

    record, _ = PAReconciliationRecord.objects.get_or_create(protected_order=protected_order)
    record.status = result.status
    record.mismatches = result.mismatches
    record.provider_report = result.provider_report
    record.last_run_at = timezone.now()
    record.save()
    audit(protected_order, "RECONCILIATION_RUN", f"Reconciliation {record.status}", {"mismatches": result.mismatches})
    return record


def serialize_order(protected_order):
    capability = get_capability(protected_order.pa_provider)
    command = getattr(protected_order, "financial_command", None)
    reconciliation = getattr(protected_order, "pa_reconciliation", None)
    pdf_evidence = get_pdf_evidence(protected_order)
    courier_decision = get_courier_verification(protected_order)
    return {
        "vaultpay_order_id": protected_order.vaultpay_order_id,
        "merchant_id": protected_order.merchant_id,
        "merchant_name": protected_order.merchant.merchant_name if protected_order.merchant else "",
        "merchant_order_id": protected_order.merchant_order_id,
        "customer": {
            "name": protected_order.order.customer_info.customer_name
            if protected_order.order and protected_order.order.customer_info
            else "",
            "phone": protected_order.order.customer_info.customer_phone
            if protected_order.order and protected_order.order.customer_info
            else "",
        },
        "pa_provider": protected_order.pa_provider,
        "pa_display_name": capability.display_name,
        "pa_account_id": protected_order.pa_account_id,
        "pa_order_id": protected_order.pa_order_id,
        "pa_payment_id": protected_order.pa_payment_id,
        "amount_minor": protected_order.amount_minor,
        "amount": str(_minor_to_major(protected_order.amount_minor)),
        "currency": protected_order.currency,
        "protection_policy": protected_order.protection_policy,
        "payment_state": protected_order.payment_state,
        "fulfilment_state": protected_order.fulfilment_state,
        "confirmation_state": protected_order.confirmation_state,
        "decision_state": protected_order.decision_state,
        "pa_command_state": protected_order.pa_command_state,
        "protection_state": protected_order.protection_state,
        "pdf_verification": pdf_evidence,
        "courier_verification": courier_decision,
        "aggregator_request": (protected_order.metadata or {}).get("aggregator_request"),
        "capability": serialize_capability(capability),
        "command": None
        if not command
        else {
            "command_id": command.command_id,
            "decision": command.decision,
            "amount_minor": command.amount_minor,
            "currency": command.currency,
            "reason_code": command.reason_code,
            "idempotency_key": command.idempotency_key,
            "provider_reference": command.provider_reference,
            "evidence_token": command.evidence_token,
            "evidence_report_url": command.evidence_report_url,
            "status": command.status,
            "provider_request": command.provider_request,
            "provider_response": command.provider_response,
            "attempts": command.attempts,
            "issued_at": command.issued_at,
            "completed_at": command.completed_at,
        },
        "reconciliation": None
        if not reconciliation
        else {
            "status": reconciliation.status,
            "mismatches": reconciliation.mismatches,
            "provider_report": reconciliation.provider_report,
            "last_run_at": reconciliation.last_run_at,
        },
        "events": [
            {
                "event_id": item.event_id,
                "pa_event_id": item.pa_event_id,
                "event_type": item.event_type,
                "signature_valid": item.signature_valid,
                "status": item.status,
                "error": item.error,
                "occurred_at": item.occurred_at,
            }
            for item in protected_order.canonical_events.all()[:10]
        ],
        "audit": [
            {
                "event_type": item.event_type,
                "message": item.message,
                "details": item.details,
                "created_at": item.created_at,
            }
            for item in protected_order.pa_audit_entries.all()[:20]
        ],
        "created_at": protected_order.created_at,
    }


def decision_reason(protected_order):
    if protected_order.decision_state == PAProtectedOrder.DECISION_RELEASE:
        if protected_order.confirmation_state == PAProtectedOrder.CONFIRMATION_CONFIRMED:
            return "Delivery completed and customer confirmed."
        return "Delivery completed and confirmation window expired."
    if protected_order.decision_state == PAProtectedOrder.DECISION_REFUND:
        if protected_order.fulfilment_state == PAProtectedOrder.FULFILMENT_RETURNED:
            return "Shipment returned to merchant."
        return "Delivery failed or customer rejected delivery."
    if protected_order.decision_state == PAProtectedOrder.DECISION_MANUAL_REVIEW:
        return "Order needs manual review because the available signals or PA capability are not sufficient for automatic movement."
    return "Waiting for enough payment, delivery, and customer confirmation signals."


def get_pdf_evidence(protected_order):
    order = protected_order.order
    filters = Q()
    order_ids = [
        protected_order.merchant_order_id,
        protected_order.pa_order_id,
        protected_order.pa_payment_id,
    ]
    if order:
        order_ids.extend([order.merchant_order_id, order.pa_order_id, order.pa_payment_id])
        if order.shipment_id and order.shipment_id.awb:
            filters |= Q(awb=order.shipment_id.awb)

    clean_ids = [item for item in {str(value or "").strip() for value in order_ids} if item]
    if clean_ids:
        filters |= Q(order_id__in=clean_ids)

    if not filters:
        return {
            "available": False,
            "status": "NOT_UPLOADED",
            "summary": "No shipment label PDF has been uploaded for this order yet.",
        }

    record = (
        PdfValidationRecord.objects.filter(merchant_id=protected_order.merchant_id)
        .filter(filters)
        .order_by("-uploaded_at")
        .first()
    )
    if not record:
        return {
            "available": False,
            "status": "NOT_FOUND",
            "summary": "No matching PDF verification record found for this order.",
        }

    awb_match = "UNKNOWN"
    shipment_awb = order.shipment_id.awb if order and order.shipment_id else ""
    if shipment_awb and record.awb:
        awb_match = "PASSED" if shipment_awb == record.awb else "FAILED"

    return {
        "available": True,
        "record_id": record.id,
        "file_name": record.file_name,
        "status": record.status.upper(),
        "verdict": record.verdict or "",
        "risk_verdict": record.risk_verdict or "",
        "score": record.score,
        "risk_score": record.risk_score,
        "delivery_partner": record.delivery_partner or "",
        "courier_partner": record.courier_partner or "",
        "awb": record.awb or "",
        "order_id": record.order_id or "",
        "awb_match": awb_match,
        "uploaded_at": record.uploaded_at,
        "summary": (
            "PDF verification approved."
            if record.status == "approved"
            else "PDF verification did not approve the label."
            if record.status == "not_approved"
            else "PDF verification is still processing."
        ),
    }


def get_courier_verification(protected_order):
    order = protected_order.order
    awb = order.shipment_id.awb if order and order.shipment_id else ""
    if not awb:
        return {
            "available": False,
            "status": "NOT_AVAILABLE",
            "summary": "No AWB is linked with this order.",
        }
    decision = DelhiveryVerificationDecision.objects.filter(awb=awb).first()
    if not decision:
        return {
            "available": False,
            "status": "PENDING",
            "awb": awb,
            "summary": "Courier verification decision is pending.",
        }
    return {
        "available": True,
        "status": decision.decision,
        "awb": awb,
        "decided_at": decision.decided_at,
        "decided_by": getattr(decision.decided_by, "username", None),
        "summary": "Courier verification passed." if decision.decision == "VERIFIED" else "Courier verification failed.",
    }


def serialize_evidence_report(command):
    protected_order = command.protected_order
    order = protected_order.order
    capability = get_capability(protected_order.pa_provider)
    reconciliation = getattr(protected_order, "pa_reconciliation", None)
    pdf_evidence = get_pdf_evidence(protected_order)
    return {
        "report_type": "VaultPay PA Evidence Report",
        "vaultpay_order_id": protected_order.vaultpay_order_id,
        "merchant_order_id": protected_order.merchant_order_id,
        "merchant": {
            "id": protected_order.merchant_id,
            "name": protected_order.merchant.merchant_name if protected_order.merchant else "",
            "email": protected_order.merchant.merchant_email if protected_order.merchant else "",
        },
        "customer": {
            "name": order.customer_info.customer_name if order and order.customer_info else "",
            "phone": order.customer_info.customer_phone if order and order.customer_info else "",
        },
        "payment_aggregator": {
            "provider": protected_order.pa_provider,
            "display_name": capability.display_name,
            "pa_account_id": protected_order.pa_account_id,
            "pa_order_id": protected_order.pa_order_id,
            "pa_payment_id": protected_order.pa_payment_id,
        },
        "amount": {
            "minor": protected_order.amount_minor,
            "display": str(_minor_to_major(protected_order.amount_minor)),
            "currency": protected_order.currency,
        },
        "decision": {
            "action": command.decision,
            "reason_code": command.reason_code,
            "reason": decision_reason(protected_order),
            "decision_state": protected_order.decision_state,
        },
        "evidence": {
            "payment_state": protected_order.payment_state,
            "protection_state": protected_order.protection_state,
            "fulfilment_state": protected_order.fulfilment_state,
            "customer_confirmation": protected_order.confirmation_state,
            "pdf_verification": pdf_evidence,
            "pa_command_state": protected_order.pa_command_state,
            "reconciliation": reconciliation.status if reconciliation else "NOT_RUN",
        },
        "command": {
            "command_id": command.command_id,
            "status": command.status,
            "provider_reference": command.provider_reference,
            "idempotency_key": command.idempotency_key,
            "evidence_report_url": command.evidence_report_url,
            "issued_at": command.issued_at,
            "completed_at": command.completed_at,
        },
        "audit": [
            {
                "event_type": item.event_type,
                "message": item.message,
                "created_at": item.created_at,
            }
            for item in protected_order.pa_audit_entries.all()[:20]
        ],
    }
