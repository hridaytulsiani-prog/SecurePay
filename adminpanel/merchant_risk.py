"""Delivery-score calculation shared by the merchant-risk API and auth checks."""

from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from adminpanel.models import MerchantRiskProfile
from payments.models import MerchantInfo, OrderInfo


SETTLED_STATES = {"settled"}
UNSUCCESSFUL_STATES = {"refunded", "refund", "refund_completed", "cancelled", "canceled"}
DELIVERED_STATES = {"delivered", "completed"}
FLAG_THRESHOLD = Decimal("80.00")


def _state(value):
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def calculate_merchant_score(merchant):
    """Score finalized payment outcomes: settled succeeds; refunded/cancelled fails."""
    orders = OrderInfo.objects.filter(merchant=merchant).select_related("shipment_id", "settlement_record")
    total = orders.count()
    successful = 0
    unsuccessful = 0
    delivered = 0
    pending = 0
    excluded = 0
    delivered_amount = Decimal("0")
    settled_amount = Decimal("0")
    for order in orders:
        delivery = _state(getattr(order.shipment_id, "status", ""))
        if delivery in DELIVERED_STATES:
            delivered_amount += order.order_amount
            delivered += 1
        try:
            settlement = order.settlement_record
        except ObjectDoesNotExist:
            settlement = None
        payment_state = _state(order.payment_state)
        order_status = _state(order.order_status)
        if payment_state in SETTLED_STATES:
            successful += 1
            settled_amount += (
                settlement.net_amount
                if settlement and settlement.net_amount is not None
                else order.order_amount
            )
        elif payment_state in UNSUCCESSFUL_STATES or order_status in {"cancelled", "canceled"}:
            unsuccessful += 1
        else:
            pending += 1

    scored = successful + unsuccessful
    excluded = total - scored
    rate = Decimal("0.00") if scored == 0 else (
        Decimal(successful * 100) / Decimal(scored)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    profile, _ = MerchantRiskProfile.objects.get_or_create(merchant=merchant)
    profile.success_rate = rate
    profile.total_orders = total
    profile.scored_orders = scored
    profile.delivered_orders = delivered
    profile.successful_orders = successful
    profile.unsuccessful_orders = unsuccessful
    profile.pending_orders = pending
    profile.excluded_orders = excluded
    profile.delivered_amount = delivered_amount
    profile.settled_amount = settled_amount
    profile.score_updated_at = timezone.now()
    profile.save(update_fields=[
        "success_rate", "total_orders", "scored_orders", "delivered_orders", "successful_orders",
        "unsuccessful_orders", "pending_orders", "excluded_orders", "delivered_amount", "settled_amount",
        "score_updated_at", "updated_at",
    ])
    profile.currently_blocked()
    return profile


def refresh_all_merchant_scores():
    return [calculate_merchant_score(merchant) for merchant in MerchantInfo.objects.order_by("id")]


def is_flagged(profile):
    return profile.scored_orders > 0 and profile.success_rate < FLAG_THRESHOLD
