from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from adminpanel.models import CustomerIssue, CustomerRiskProfile
from payments.models import CustomerInfo, OrderInfo


CUSTOMER_FLAG_THRESHOLD = Decimal("60.00")
MIN_REPEAT_ORDERS = 2
MIN_ASSESSED_ORDERS = 3
MIN_PROBLEM_ORDERS = 2

PROBLEM_PAYMENT_STATES = {"refunded", "refund", "refund_completed", "disputed", "chargeback", "cancelled", "canceled"}
PROBLEM_DELIVERY_STATES = {"returned", "rto", "return_to_origin", "cancelled", "canceled", "lost", "damaged"}
CLEAN_PAYMENT_STATES = {"settled", "released", "completed"}
CLEAN_DELIVERY_STATES = {"delivered", "completed"}


def _norm(value):
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _refs_for_order(order):
    shipment = getattr(order, "shipment_id", None)
    return {
        str(value).strip().lower()
        for value in (order.merchant_order_id, order.pa_order_id, getattr(shipment, "awb", ""))
        if value
    }


def _refs_for_issue(issue):
    order = issue.order or {}
    return {
        str(value).strip().lower()
        for value in (
            issue.order_ref,
            order.get("merchant_order_id"),
            order.get("order_id"),
            order.get("awb"),
        )
        if value
    }


def _issue_matches_customer(issue, customer, customer_order_refs):
    phone_match = issue.customer_phone and _norm(issue.customer_phone) == _norm(customer.customer_phone)
    email_match = issue.customer_email and _norm(issue.customer_email) == _norm(customer.customer_email)
    return bool(phone_match or email_match or (_refs_for_issue(issue) & customer_order_refs))


def order_problem_reasons(order, issue_refs):
    reasons = []
    payment = _norm(order.payment_state or order.order_status)
    delivery = _norm(getattr(order.shipment_id, "status", ""))
    if _refs_for_order(order) & issue_refs:
        reasons.append("Customer issue reported")
    if payment in PROBLEM_PAYMENT_STATES:
        reasons.append("Refunded, disputed, or cancelled payment")
    if delivery in PROBLEM_DELIVERY_STATES:
        reasons.append("Returned, cancelled, lost, or damaged delivery")
    return reasons


def order_is_clean(order):
    payment = _norm(order.payment_state or order.order_status)
    delivery = _norm(getattr(order.shipment_id, "status", ""))
    return payment in CLEAN_PAYMENT_STATES or delivery in CLEAN_DELIVERY_STATES


def calculate_customer_score(customer, *, orders=None, issues=None):
    orders = list(orders if orders is not None else OrderInfo.objects.filter(customer_info=customer).select_related("shipment_id", "merchant"))
    all_refs = set().union(*(_refs_for_order(order) for order in orders)) if orders else set()
    all_issues = list(issues if issues is not None else CustomerIssue.objects.all())
    customer_issues = [issue for issue in all_issues if _issue_matches_customer(issue, customer, all_refs)]
    issue_refs = set().union(*(_refs_for_issue(issue) for issue in customer_issues)) if customer_issues else set()

    problem = 0
    clean = 0
    for order in orders:
        if order_problem_reasons(order, issue_refs):
            problem += 1
        elif order_is_clean(order):
            clean += 1
    assessed = clean + problem
    pending = max(0, len(orders) - assessed)
    success_rate = (Decimal(clean) * Decimal("100") / Decimal(assessed)).quantize(Decimal("0.01")) if assessed else Decimal("100.00")

    profile, _ = CustomerRiskProfile.objects.update_or_create(
        customer=customer,
        defaults={
            "success_rate": success_rate,
            "total_orders": len(orders),
            "assessed_orders": assessed,
            "clean_orders": clean,
            "problem_orders": problem,
            "issue_count": len(customer_issues),
            "pending_orders": pending,
            "score_updated_at": timezone.now(),
        },
    )
    return profile


def customer_is_flagged(profile):
    return (
        profile.assessed_orders >= MIN_ASSESSED_ORDERS
        and profile.problem_orders >= MIN_PROBLEM_ORDERS
        and profile.success_rate < CUSTOMER_FLAG_THRESHOLD
    )


@transaction.atomic
def refresh_all_customer_scores():
    customers = list(CustomerInfo.objects.all())
    orders_by_customer = defaultdict(list)
    for order in OrderInfo.objects.select_related("shipment_id", "merchant").all():
        orders_by_customer[order.customer_info_id].append(order)
    issues = list(CustomerIssue.objects.all())
    return [
        calculate_customer_score(customer, orders=orders_by_customer.get(customer.id, []), issues=issues)
        for customer in customers
    ]
