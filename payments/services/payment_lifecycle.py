import base64
import hashlib
import hmac
import json
import logging
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from payments.models import (
    OrderInfo,
    PaymentEvent,
    PaymentLedgerEntry,
    PaymentNotification,
    PaymentStateTransition,
    SettlementRecord,
)

logger = logging.getLogger(__name__)

PAYMENT_STATES = {
    "CREATED", "PAYMENT_PENDING", "PAYMENT_CAPTURED", "FUNDS_HELD",
    "SETTLEMENT_ELIGIBLE", "SETTLEMENT_PROCESSING", "SETTLED",
    "PAYMENT_FAILED", "REFUND_PENDING", "REFUNDED", "DISPUTED",
}

ALLOWED_TRANSITIONS = {
    "": {"CREATED", "PAYMENT_PENDING", "PAYMENT_FAILED"},
    "CREATED": {"PAYMENT_PENDING", "PAYMENT_CAPTURED", "PAYMENT_FAILED"},
    "PAYMENT_PENDING": {"PAYMENT_CAPTURED", "PAYMENT_FAILED", "REFUND_PENDING"},
    "PAYMENT_CAPTURED": {"FUNDS_HELD", "REFUND_PENDING", "DISPUTED"},
    "FUNDS_HELD": {"SETTLEMENT_ELIGIBLE", "REFUND_PENDING", "DISPUTED"},
    "SETTLEMENT_ELIGIBLE": {"SETTLEMENT_PROCESSING", "REFUND_PENDING", "DISPUTED"},
    "SETTLEMENT_PROCESSING": {"SETTLED", "DISPUTED"},
    "SETTLED": {"REFUND_PENDING", "DISPUTED"},
    "PAYMENT_FAILED": {"PAYMENT_PENDING", "CREATED"},
    "REFUND_PENDING": {"REFUNDED", "DISPUTED"},
    "REFUNDED": set(),
    "DISPUTED": {"REFUND_PENDING", "SETTLEMENT_PROCESSING"},
}


def _status_from_payload(payload):
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return str(data.get("state") or data.get("status") or payload.get("state") or payload.get("status") or payload.get("paymentStatus") or "PENDING").upper()


def _payment_state(status):
    if status in {"SUCCESS", "SUCCESSFUL", "COMPLETED", "PAID", "CAPTURED", "PAYMENT_SUCCESS"}:
        return "PAYMENT_CAPTURED"
    if status in {"FAILED", "FAILURE", "DECLINED", "ERROR", "PAYMENT_ERROR"}:
        return "PAYMENT_FAILED"
    if status in {"REFUNDED", "REFUND_SUCCESS"}:
        return "REFUNDED"
    return "PAYMENT_PENDING"


def _signature_secret(provider):
    return getattr(settings, f"{provider.upper()}_WEBHOOK_SECRET", "") or getattr(settings, "PAYMENT_WEBHOOK_SECRET", "")


def verify_webhook_signature(provider, raw_body, signature):
    secret = _signature_secret(provider)
    if not secret:
        return bool(getattr(settings, "PAYMENT_WEBHOOK_ALLOW_UNSIGNED_TEST_EVENTS", False))
    if not signature:
        return False
    supplied = signature.strip()
    if supplied.lower().startswith("sha256="):
        supplied = supplied.split("=", 1)[1]
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256)
    return hmac.compare_digest(supplied, digest.hexdigest()) or hmac.compare_digest(supplied, base64.b64encode(digest.digest()).decode())


def _find_order(payload):
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    order_id = payload.get("merchantOrderId") or payload.get("merchantTransactionId") or data.get("merchantOrderId") or data.get("merchantTransactionId") or payload.get("orderId") or data.get("orderId")
    payment_id = payload.get("paymentId") or payload.get("transactionId") or payload.get("txnId") or data.get("paymentId") or data.get("transactionId") or data.get("txnId")
    order = None
    if order_id:
        order = OrderInfo.objects.filter(merchant_order_id=order_id).first() or OrderInfo.objects.filter(pa_order_id=order_id).first()
    if not order and payment_id:
        order = OrderInfo.objects.filter(pa_payment_id=payment_id).first()
    return order, order_id, payment_id


def transition_payment_state(order, new_state, source="system", metadata=None, event=None):
    if new_state not in PAYMENT_STATES:
        raise ValueError(f"Unknown payment state: {new_state}")
    metadata = metadata or {}
    old_state = order.payment_state or ""
    if old_state == new_state:
        return order
    if new_state not in ALLOWED_TRANSITIONS.get(old_state, set()):
        raise ValueError(f"Invalid payment transition {old_state or 'EMPTY'} -> {new_state}")
    now = timezone.now()
    order.payment_state = new_state
    order.payment_state_updated_at = now
    order.payment_state_metadata = metadata
    order.save(update_fields=["payment_state", "payment_state_updated_at", "payment_state_metadata"])
    PaymentStateTransition.objects.create(order=order, from_state=old_state, to_state=new_state, source=source, event=event, metadata=metadata)
    return order


def _ledger(order, entry_type, amount, reference, metadata=None):
    if PaymentLedgerEntry.objects.filter(order=order, entry_type=entry_type, reference=reference).exists():
        return
    PaymentLedgerEntry.objects.create(order=order, entry_type=entry_type, amount=Decimal(str(amount)), currency=order.order_currency, reference=reference, metadata=metadata or {})


def _notification(order, event_type, title, message, channel=PaymentNotification.CHANNEL_DASHBOARD, metadata=None):
    dedupe_key = f"{channel}:{event_type}:{order.id}:{order.payment_state}"
    notification, _ = PaymentNotification.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults={"merchant": order.merchant, "order": order, "event_type": event_type, "channel": channel, "title": title, "message": message, "metadata": metadata or {}, "status": PaymentNotification.STATUS_SENT if channel == PaymentNotification.CHANNEL_DASHBOARD else PaymentNotification.STATUS_PENDING, "sent_at": timezone.now() if channel == PaymentNotification.CHANNEL_DASHBOARD else None},
    )
    if channel == PaymentNotification.CHANNEL_EMAIL and notification.status == PaymentNotification.STATUS_PENDING:
        try:
            send_mail(notification.title, notification.message, settings.DEFAULT_FROM_EMAIL, [order.merchant.merchant_email])
            notification.status = PaymentNotification.STATUS_SENT
            notification.sent_at = timezone.now()
            notification.save(update_fields=["status", "sent_at"])
        except Exception as exc:
            logger.exception("Payment notification email failed")
            notification.status = PaymentNotification.STATUS_FAILED
            notification.metadata = {**(notification.metadata or {}), "error": str(exc)}
            notification.save(update_fields=["status", "metadata"])
    return notification


def _apply_success(order, event):
    metadata = {"provider": event.provider, "event_type": event.event_type}
    if order.payment_state in {"", "CREATED", "PAYMENT_PENDING"}:
        transition_payment_state(order, "PAYMENT_CAPTURED", "webhook", metadata, event)
        _ledger(order, PaymentLedgerEntry.ENTRY_CAPTURED, order.order_amount, f"capture:{event.id}", metadata)
        _notification(order, "payment_captured", "Payment captured", f"Payment captured for order {order.merchant_order_id}.", metadata=metadata)
        _notification(order, "payment_captured", "Payment captured", f"Payment captured for order {order.merchant_order_id}.", PaymentNotification.CHANNEL_EMAIL, metadata)
    if order.payment_state == "PAYMENT_CAPTURED":
        transition_payment_state(order, "FUNDS_HELD", "webhook", metadata, event)
        _ledger(order, PaymentLedgerEntry.ENTRY_HELD, order.order_amount, f"held:{event.id}", metadata)
        _notification(order, "funds_held", "Funds held", f"Funds for order {order.merchant_order_id} are held by the payment aggregator.", metadata=metadata)
        _notification(order, "funds_held", "Funds held", f"Funds for order {order.merchant_order_id} are held by the payment aggregator.", PaymentNotification.CHANNEL_EMAIL, metadata)
    if order.payment_state == "FUNDS_HELD":
        transition_payment_state(order, "SETTLEMENT_ELIGIBLE", "webhook", metadata, event)
    SettlementRecord.objects.get_or_create(order=order, defaults={"provider": event.provider, "gross_amount": order.order_amount, "eligible_at": timezone.now()})
    order.order_status = "paid"
    order.save(update_fields=["order_status"])


def process_payment_webhook(provider, raw_body, signature="", event_id="", headers=None):
    if not verify_webhook_signature(provider, raw_body, signature):
        raise PermissionError("Invalid payment webhook signature")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid payment webhook JSON") from exc
    event_hash = hashlib.sha256(raw_body).hexdigest()
    event_type = str(payload.get("event") or payload.get("eventType") or payload.get("type") or "payment.update")
    try:
        with transaction.atomic():
            event = PaymentEvent.objects.create(provider=provider, event_id=str(event_id or payload.get("eventId") or payload.get("id") or ""), event_hash=event_hash, event_type=event_type, payload=payload, signature=signature or "")
            order, order_id, payment_id = _find_order(payload)
            event.order = order
            if not order:
                event.status = PaymentEvent.STATUS_REJECTED
                event.error = "Unknown order"
                event.processed_at = timezone.now()
                event.save(update_fields=["order", "status", "error", "processed_at"])
                return {"ok": True, "duplicate": False, "unknown_order": True}
            event.save(update_fields=["order"])
            state = _payment_state(_status_from_payload(payload))
            if state == "PAYMENT_CAPTURED":
                _apply_success(order, event)
            elif state == "PAYMENT_FAILED":
                if order.payment_state in {"", "CREATED", "PAYMENT_PENDING"}:
                    transition_payment_state(order, state, "webhook", {"provider": provider}, event)
                order.order_status = "payment_failed"
                order.save(update_fields=["order_status"])
            elif state == "REFUNDED":
                if order.payment_state != "REFUNDED":
                    if order.payment_state != "REFUND_PENDING":
                        transition_payment_state(order, "REFUND_PENDING", "webhook", {"provider": provider}, event)
                    transition_payment_state(order, "REFUNDED", "webhook", {"provider": provider}, event)
                _ledger(order, PaymentLedgerEntry.ENTRY_REFUND, -order.order_amount, f"refund:{event.id}", {"provider": provider})
                order.order_status = "refunded"
                order.save(update_fields=["order_status"])
            else:
                if order.payment_state in {"", "CREATED"}:
                    transition_payment_state(order, "PAYMENT_PENDING", "webhook", {"provider": provider}, event)
                order.order_status = "pending"
                order.save(update_fields=["order_status"])
            event.status = PaymentEvent.STATUS_PROCESSED
            event.processed_at = timezone.now()
            event.save(update_fields=["status", "processed_at"])
    except IntegrityError:
        existing = PaymentEvent.objects.filter(provider=provider, event_hash=event_hash).first()
        if existing:
            return {"ok": True, "duplicate": True, "event_id": existing.id}
        raise
    return {"ok": True, "duplicate": False, "event_id": event.id, "order_id": order_id, "payment_id": payment_id}
