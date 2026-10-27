from collections import Counter
from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from adminpanel.merchant_risk import FLAG_THRESHOLD, calculate_merchant_score, is_flagged, refresh_all_merchant_scores
from adminpanel.models import MerchantRiskProfile
from adminpanel.permissions import AdminAPIView
from payments.models import AuditLog, MerchantInfo, OrderInfo, PaymentNotification
from payments.services.audit import create_audit_log


def _iso(value):
    return value.isoformat() if value else None


def _settlement_status(order):
    try:
        return order.settlement_record.status or ""
    except ObjectDoesNotExist:
        return ""


def _profile_payload(profile, *, include_merchant=True):
    merchant = profile.merchant
    blocked = profile.currently_blocked()
    has_score = profile.scored_orders > 0
    payload = {
        "merchant_id": merchant.id,
        "success_rate": float(profile.success_rate) if has_score else None,
        "has_score": has_score,
        "threshold": float(FLAG_THRESHOLD),
        "is_flagged": is_flagged(profile),
        "total_orders": profile.total_orders,
        "scored_orders": profile.scored_orders,
        "delivered_orders": profile.delivered_orders,
        "successful_orders": profile.successful_orders,
        "unsuccessful_orders": profile.unsuccessful_orders,
        "pending_orders": profile.pending_orders,
        "excluded_orders": profile.excluded_orders,
        "delivered_amount": str(profile.delivered_amount),
        "settled_amount": str(profile.settled_amount),
        "score_explanation": (
            f"{profile.successful_orders} settled and {profile.unsuccessful_orders} refunded/cancelled payment outcomes."
            if has_score
            else "No orders have been recorded yet."
            if profile.total_orders == 0
            else f"No finalized payment outcomes yet. {profile.pending_orders} order(s) are still pending."
        ),
        "score_updated_at": _iso(profile.score_updated_at),
        "access_status": profile.access_status,
        "is_blocked": blocked,
        "blocked_until": _iso(profile.blocked_until),
        "block_reason": profile.block_reason,
        "blocked_at": _iso(profile.blocked_at),
        "blocked_by": profile.blocked_by.username if profile.blocked_by else None,
    }
    if include_merchant:
        payload.update({
            "merchant_name": merchant.merchant_name,
            "merchant_email": merchant.merchant_email,
            "merchant_phone": merchant.merchant_phone,
        })
    return payload


class AdminMerchantRiskListView(AdminAPIView):
    """Notification feed and review queue for merchants below the 80% threshold."""

    required_permission = "orders.view"

    def get(self, request):
        profiles = refresh_all_merchant_scores()
        scope = str(request.GET.get("scope") or "flagged").strip().lower()
        selected = profiles if scope == "all" else [profile for profile in profiles if is_flagged(profile)]
        selected = sorted(selected, key=lambda item: (item.success_rate, -item.total_orders, item.merchant.merchant_name.lower()))
        return Response({
            "threshold": float(FLAG_THRESHOLD),
            "scope": "all" if scope == "all" else "flagged",
            "count": len(selected),
            "flagged_count": sum(1 for profile in profiles if is_flagged(profile)),
            "results": [_profile_payload(profile) for profile in selected],
        })


class AdminMerchantRiskDetailView(AdminAPIView):
    required_permission = "orders.view"

    def get(self, request, merchant_id):
        merchant = MerchantInfo.objects.filter(id=merchant_id).first()
        if merchant is None:
            return Response({"error": "Merchant not found."}, status=status.HTTP_404_NOT_FOUND)

        profile = calculate_merchant_score(merchant)
        orders = list(
            OrderInfo.objects.filter(merchant=merchant)
            .select_related("shipment_id", "customer_info", "settlement_record")
            .order_by("-order_date", "-id")
        )
        delivery_breakdown = Counter(
            (getattr(order.shipment_id, "status", "") or "NOT_SHIPPED").upper()
            for order in orders
        )
        courier_breakdown = Counter(
            (getattr(order.shipment_id, "courier", "") or "Not assigned")
            for order in orders
        )
        recent_orders = [
            {
                "id": order.id,
                "merchant_order_id": order.merchant_order_id,
                "amount": str(order.order_amount),
                "currency": order.order_currency,
                "order_status": order.order_status,
                "payment_state": order.payment_state,
                "settlement_status": _settlement_status(order),
                "delivery_status": getattr(order.shipment_id, "status", "") or "NOT_SHIPPED",
                "courier": getattr(order.shipment_id, "courier", "") or "",
                "awb": getattr(order.shipment_id, "awb", "") or "",
                "customer_name": order.customer_info.customer_name,
                "order_date": _iso(order.order_date),
            }
            for order in orders
        ]
        actions = [
            {
                "action": row.action,
                "actor": row.actor_display,
                "remarks": row.remarks,
                "created_at": _iso(row.created_at),
                "old_values": row.old_values,
                "new_values": row.new_values,
            }
            for row in AuditLog.objects.filter(case_type="merchant_risk", case_id=str(merchant.id))[:20]
        ]
        return Response({
            "merchant": {
                "id": merchant.id,
                "merchant_name": merchant.merchant_name,
                "merchant_email": merchant.merchant_email,
                "merchant_phone": merchant.merchant_phone,
                "merchant_address": merchant.merchant_address,
                "created_at": _iso(merchant.created_at),
            },
            "risk": _profile_payload(profile, include_merchant=False),
            "reason": (
                f"Merchant success rate is {profile.success_rate}%, below the {FLAG_THRESHOLD}% review threshold. "
                f"{profile.successful_orders} payments were settled and {profile.unsuccessful_orders} were refunded or cancelled."
                if is_flagged(profile)
                else f"Merchant success rate is at or above the {FLAG_THRESHOLD}% review threshold: {profile.successful_orders} settled and {profile.unsuccessful_orders} refunded or cancelled."
                if profile.scored_orders > 0
                else "No settled, refunded, or cancelled payment outcomes are available yet, so a merchant success rate cannot be calculated."
            ),
            "delivery_breakdown": dict(delivery_breakdown),
            "courier_breakdown": dict(courier_breakdown),
            "recent_orders": recent_orders,
            "actions": actions,
        })


class AdminMerchantRiskAccessView(AdminAPIView):
    required_permission = "users.manage"

    def post(self, request, merchant_id):
        merchant = MerchantInfo.objects.filter(id=merchant_id).first()
        if merchant is None:
            return Response({"error": "Merchant not found."}, status=status.HTTP_404_NOT_FOUND)

        action = str(request.data.get("action") or "").strip()
        reason = str(request.data.get("reason") or "").strip()
        if action not in {"temporary_block", "permanent_block", "unblock"}:
            return Response({"error": "Choose temporary_block, permanent_block, or unblock."}, status=status.HTTP_400_BAD_REQUEST)
        if action != "unblock" and not reason:
            return Response({"error": "A reason is required when blocking a merchant."}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            profile = calculate_merchant_score(merchant)
            old_values = {
                "access_status": profile.access_status,
                "blocked_until": _iso(profile.blocked_until),
                "block_reason": profile.block_reason,
            }
            now = timezone.now()
            if action == "temporary_block":
                try:
                    duration_days = max(1, min(int(request.data.get("duration_days", 7)), 90))
                except (TypeError, ValueError):
                    return Response({"error": "Duration must be a number from 1 to 90 days."}, status=status.HTTP_400_BAD_REQUEST)
                profile.access_status = MerchantRiskProfile.ACCESS_TEMPORARY
                profile.blocked_until = now + timedelta(days=duration_days)
                profile.block_reason = reason
                profile.blocked_at = now
                profile.blocked_by = request.admin_user
            elif action == "permanent_block":
                profile.access_status = MerchantRiskProfile.ACCESS_PERMANENT
                profile.blocked_until = None
                profile.block_reason = reason
                profile.blocked_at = now
                profile.blocked_by = request.admin_user
            else:
                profile.access_status = MerchantRiskProfile.ACCESS_ACTIVE
                profile.blocked_until = None
                profile.block_reason = ""
                profile.blocked_at = None
                profile.blocked_by = None
            profile.save()
            new_values = {
                "access_status": profile.access_status,
                "blocked_until": _iso(profile.blocked_until),
                "block_reason": profile.block_reason,
            }
            audit_log = create_audit_log(
                case_type="merchant_risk",
                case_id=merchant.id,
                action=action,
                actor_user=request.admin_user,
                actor_role=request.admin_role,
                remarks=reason or "Merchant access restored.",
                old_values=old_values,
                new_values=new_values,
                metadata={"success_rate": str(profile.success_rate)},
                source="admin_merchant_risk",
                request=request,
            )
            if action == "temporary_block":
                until = timezone.localtime(profile.blocked_until).strftime("%d %b %Y, %I:%M %p %Z")
                notification_title = "New orders temporarily paused"
                notification_message = (
                    f"EscroSafe has temporarily paused new orders for your account until {until}. "
                    f"You can still sign in and manage existing orders. Reason: {reason}"
                )
                event_type = "merchant_temporary_restriction"
            elif action == "permanent_block":
                notification_title = "New orders permanently paused"
                notification_message = (
                    "EscroSafe has permanently paused new orders for your account. "
                    f"You can still sign in and manage existing orders. Reason: {reason}"
                )
                event_type = "merchant_permanent_restriction"
            else:
                notification_title = "New-order access restored"
                notification_message = (
                    "EscroSafe has restored new-order access for your account. "
                    "You can now receive and create new orders."
                )
                event_type = "merchant_order_access_restored"

            PaymentNotification.objects.create(
                merchant=merchant,
                event_type=event_type,
                channel=PaymentNotification.CHANNEL_DASHBOARD,
                dedupe_key=f"DASHBOARD:{event_type}:{audit_log.id}",
                title=notification_title,
                message=notification_message,
                status=PaymentNotification.STATUS_SENT,
                metadata={
                    "source": "admin_merchant_restriction",
                    "block_type": "temporary" if action == "temporary_block" else "permanent" if action == "permanent_block" else "none",
                    "reason": reason,
                    "blocked_until": _iso(profile.blocked_until),
                },
                sent_at=now,
            )

        return Response({"risk": _profile_payload(profile, include_merchant=False)})
