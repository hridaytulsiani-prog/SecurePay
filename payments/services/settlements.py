import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from payments.models import PaymentLedgerEntry, PaymentNotification, SettlementRecord
from .payment_lifecycle import transition_payment_state

logger = logging.getLogger(__name__)


def _settlement_url(provider, settlement):
    templates = getattr(settings, "PAYMENT_SETTLEMENT_URLS", {}) or {}
    template = templates.get(provider, "")
    if not template:
        return ""
    return template.format(order_id=settlement.order.merchant_order_id, payment_id=settlement.order.pa_payment_id or "")


def fetch_settlement_status(settlement):
    url = _settlement_url(settlement.provider, settlement)
    if not url:
        return {"status": "PENDING", "configured": False}
    headers = getattr(settings, "PAYMENT_SETTLEMENT_HEADERS", {}) or {}
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


@transaction.atomic
def apply_settlement_status(settlement, payload, source="settlement_poll"):
    status_text = str(payload.get("status") or payload.get("state") or "PENDING").upper()
    settlement.last_checked_at = timezone.now()
    settlement.raw_response = payload
    if status_text in {"SETTLED", "SUCCESS", "COMPLETED", "PAID"}:
        order = settlement.order
        gross_amount = Decimal(str(payload.get("grossAmount") or order.order_amount))
        if gross_amount != order.order_amount:
            settlement.status = SettlementRecord.STATUS_MISMATCH
            settlement.gross_amount = gross_amount
            settlement.error = f"Gross settlement amount {gross_amount} does not match order amount {order.order_amount}"
            settlement.save()
            return settlement
        if order.payment_state == "FUNDS_HELD":
            transition_payment_state(order, "SETTLEMENT_ELIGIBLE", source, {"settlement": payload})
        if order.payment_state == "SETTLEMENT_ELIGIBLE":
            transition_payment_state(order, "SETTLEMENT_PROCESSING", source, {"settlement": payload})
        if order.payment_state == "SETTLEMENT_PROCESSING":
            transition_payment_state(order, "SETTLED", source, {"settlement": payload})
        settlement.status = SettlementRecord.STATUS_SETTLED
        settlement.settlement_id = str(payload.get("settlementId") or payload.get("id") or settlement.settlement_id)
        settlement.gross_amount = gross_amount
        settlement.fee_amount = Decimal(str(payload.get("feeAmount") or "0"))
        settlement.net_amount = Decimal(str(payload.get("netAmount") or settlement.gross_amount - settlement.fee_amount))
        settlement.settled_at = timezone.now()
        if not PaymentLedgerEntry.objects.filter(order=order, entry_type=PaymentLedgerEntry.ENTRY_SETTLEMENT, reference=settlement.settlement_id).exists():
            PaymentLedgerEntry.objects.create(order=order, entry_type=PaymentLedgerEntry.ENTRY_SETTLEMENT, amount=settlement.net_amount, currency=order.order_currency, reference=settlement.settlement_id, metadata=payload)
        PaymentNotification.objects.get_or_create(
            dedupe_key=f"DASHBOARD:settled:{order.id}:{settlement.settlement_id}",
            defaults={"merchant": order.merchant, "order": order, "event_type": "settled", "channel": PaymentNotification.CHANNEL_DASHBOARD, "title": "Payment settled", "message": f"Payment for order {order.merchant_order_id} has settled.", "status": PaymentNotification.STATUS_SENT, "sent_at": timezone.now(), "metadata": payload},
        )
        email_notification, created = PaymentNotification.objects.get_or_create(
            dedupe_key=f"EMAIL:settled:{order.id}:{settlement.settlement_id}",
            defaults={"merchant": order.merchant, "order": order, "event_type": "settled", "channel": PaymentNotification.CHANNEL_EMAIL, "title": "Payment settled", "message": f"Payment for order {order.merchant_order_id} has settled.", "status": PaymentNotification.STATUS_PENDING, "metadata": payload},
        )
        if created:
            try:
                send_mail(email_notification.title, email_notification.message, settings.DEFAULT_FROM_EMAIL, [order.merchant.merchant_email])
                email_notification.status = PaymentNotification.STATUS_SENT
                email_notification.sent_at = timezone.now()
                email_notification.save(update_fields=["status", "sent_at"])
            except Exception as exc:
                logger.exception("Settlement notification email failed")
                email_notification.status = PaymentNotification.STATUS_FAILED
                email_notification.metadata = {**(email_notification.metadata or {}), "error": str(exc)}
                email_notification.save(update_fields=["status", "metadata"])
    elif status_text in {"FAILED", "ERROR", "MISMATCH"}:
        settlement.status = SettlementRecord.STATUS_MISMATCH if status_text == "MISMATCH" else SettlementRecord.STATUS_FAILED
        settlement.error = str(payload.get("message") or payload.get("error") or "Settlement failed")
    else:
        settlement.status = SettlementRecord.STATUS_PENDING
    settlement.save()
    return settlement


def process_due_settlements(provider=None, merchant_id=None):
    records = SettlementRecord.objects.select_related("order", "order__merchant").filter(status__in=[SettlementRecord.STATUS_PENDING, SettlementRecord.STATUS_PROCESSING])
    if provider:
        records = records.filter(provider=provider)
    if merchant_id is not None:
        records = records.filter(order__merchant_id=merchant_id)
    results = []
    for settlement in records:
        try:
            settlement.status = SettlementRecord.STATUS_PROCESSING
            settlement.save(update_fields=["status", "updated_at"])
            results.append(apply_settlement_status(settlement, fetch_settlement_status(settlement)))
        except Exception as exc:
            logger.exception("Settlement check failed for order %s", settlement.order_id)
            settlement.status = SettlementRecord.STATUS_FAILED
            settlement.error = str(exc)
            settlement.last_checked_at = timezone.now()
            settlement.save(update_fields=["status", "error", "last_checked_at", "updated_at"])
    return results
