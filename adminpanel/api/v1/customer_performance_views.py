from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from adminpanel.customer_performance import (
    CUSTOMER_FLAG_THRESHOLD,
    MIN_REPEAT_ORDERS,
    MIN_ASSESSED_ORDERS,
    MIN_PROBLEM_ORDERS,
    calculate_customer_score,
    customer_is_flagged,
    order_is_clean,
    order_problem_reasons,
    refresh_all_customer_scores,
    _issue_matches_customer,
    _refs_for_issue,
    _refs_for_order,
)
from adminpanel.models import CustomerIssue
from adminpanel.permissions import AdminAPIView
from payments.models import CustomerInfo, OrderInfo


def _iso(value):
    return value.isoformat() if value else None


def _payload(profile, *, include_customer=True):
    eligible = profile.assessed_orders >= MIN_ASSESSED_ORDERS
    payload = {
        "customer_id": profile.customer_id,
        "success_rate": float(profile.success_rate) if eligible else None,
        "has_score": eligible,
        "is_flagged": customer_is_flagged(profile),
        "threshold": float(CUSTOMER_FLAG_THRESHOLD),
        "minimum_orders": MIN_ASSESSED_ORDERS,
        "minimum_problem_orders": MIN_PROBLEM_ORDERS,
        "total_orders": profile.total_orders,
        "assessed_orders": profile.assessed_orders,
        "clean_orders": profile.clean_orders,
        "problem_orders": profile.problem_orders,
        "issue_count": profile.issue_count,
        "pending_orders": profile.pending_orders,
        "score_updated_at": _iso(profile.score_updated_at),
    }
    if include_customer:
        payload.update({
            "customer_name": profile.customer.customer_name,
            "customer_email": profile.customer.customer_email,
            "customer_phone": profile.customer.customer_phone,
        })
    return payload


class AdminCustomerPerformanceListView(AdminAPIView):
    required_permission = "orders.view"

    def get(self, request):
        # This screen evaluates repeat-customer behaviour. A customer with a
        # single order has no recurring pattern to review and should not appear.
        profiles = [
            profile for profile in refresh_all_customer_scores()
            if profile.total_orders >= MIN_REPEAT_ORDERS
        ]
        query = str(request.GET.get("q") or "").strip().lower()
        status_filter = str(request.GET.get("status") or "all").strip().lower()
        if query:
            profiles = [
                profile for profile in profiles
                if query in " ".join([
                    profile.customer.customer_name,
                    profile.customer.customer_email,
                    profile.customer.customer_phone,
                ]).lower()
            ]
        if status_filter == "flagged":
            profiles = [profile for profile in profiles if customer_is_flagged(profile)]
        elif status_filter == "healthy":
            profiles = [profile for profile in profiles if profile.assessed_orders >= MIN_ASSESSED_ORDERS and not customer_is_flagged(profile)]
        elif status_filter == "insufficient":
            profiles = [profile for profile in profiles if profile.assessed_orders < MIN_ASSESSED_ORDERS]
        profiles = sorted(profiles, key=lambda profile: (not customer_is_flagged(profile), profile.success_rate, -profile.problem_orders, profile.customer.customer_name.lower()))
        return Response({
            "threshold": float(CUSTOMER_FLAG_THRESHOLD),
            "minimum_repeat_orders": MIN_REPEAT_ORDERS,
            "minimum_orders": MIN_ASSESSED_ORDERS,
            "minimum_problem_orders": MIN_PROBLEM_ORDERS,
            "count": len(profiles),
            "flagged_count": sum(1 for profile in profiles if customer_is_flagged(profile)),
            "results": [_payload(profile) for profile in profiles],
        })


class AdminCustomerPerformanceDetailView(AdminAPIView):
    required_permission = "orders.view"

    def get(self, request, customer_id):
        customer = CustomerInfo.objects.filter(id=customer_id).first()
        if customer is None:
            return Response({"error": "Customer not found."}, status=status.HTTP_404_NOT_FOUND)

        orders = list(OrderInfo.objects.filter(customer_info=customer).select_related("shipment_id", "merchant").order_by("-order_date", "-id"))
        order_refs = set().union(*(_refs_for_order(order) for order in orders)) if orders else set()
        issues = [issue for issue in CustomerIssue.objects.all() if _issue_matches_customer(issue, customer, order_refs)]
        issue_refs = set().union(*(_refs_for_issue(issue) for issue in issues)) if issues else set()
        profile = calculate_customer_score(customer, orders=orders, issues=issues)

        order_rows = []
        for order in orders:
            reasons = order_problem_reasons(order, issue_refs)
            outcome = "problem" if reasons else "clean" if order_is_clean(order) else "pending"
            shipment = order.shipment_id
            order_rows.append({
                "id": order.id,
                "merchant_order_id": order.merchant_order_id,
                "merchant_id": order.merchant_id,
                "merchant_name": order.merchant.merchant_name,
                "amount": str(order.order_amount),
                "currency": order.order_currency,
                "payment_state": order.payment_state,
                "order_status": order.order_status,
                "delivery_status": getattr(shipment, "status", "") or "NOT_SHIPPED",
                "courier": getattr(shipment, "courier", "") or "",
                "awb": getattr(shipment, "awb", "") or "",
                "order_date": _iso(order.order_date),
                "outcome": outcome,
                "problem_reasons": reasons,
            })

        return Response({
            "customer": {
                "id": customer.id,
                "customer_name": customer.customer_name,
                "customer_email": customer.customer_email,
                "customer_phone": customer.customer_phone,
                "customer_address": customer.customer_address,
            },
            "risk": _payload(profile, include_customer=False),
            "explanation": (
                f"{profile.problem_orders} of {profile.assessed_orders} assessed orders have customer issues, refunds, disputes, cancellations, or return problems."
                if profile.assessed_orders >= MIN_ASSESSED_ORDERS
                else f"At least {MIN_ASSESSED_ORDERS} assessed orders are required before customer behaviour is scored."
            ),
            "orders": order_rows,
            "issues": [
                {
                    "id": issue.id,
                    "reference": issue.reference,
                    "issue_type": issue.issue_type,
                    "status": issue.status,
                    "order_ref": issue.order_ref,
                    "description": issue.description,
                    "created_at": _iso(issue.created_at),
                }
                for issue in issues
            ],
        })
