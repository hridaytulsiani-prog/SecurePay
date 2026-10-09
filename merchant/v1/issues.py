"""Merchant "Support" API: a logged-in merchant raises an issue about one or more of their own orders and then
chats with the EscroSafe team about it.

    GET  /merchants/issues/                       the merchant's own issues (newest first)
    POST /merchants/issues/                       raise a new issue: {issue_type, description, orders: [order ids]}
    GET  /merchants/issues/<id>/                  one issue with its whole conversation
    POST /merchants/issues/<id>/messages/         send a message in that conversation: {body}

Every order id the merchant sends must belong to that merchant (matched against the merchant order id or the
payment aggregator order id, the same ids the dashboard shows). The orders are copied into the issue with their
courier, AWB, delivery / payment status and amount as they were when the issue was raised, so the admin sees exactly
what the merchant saw. The admin reads and answers these in the admin app under "Merchant issues"
(adminpanel/api/v1/merchant_issue_views.py).
"""

import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from adminpanel.models import MerchantIssue, MerchantIssueMessage
from adminpanel.order_report import build_order_report
from payments.auth import get_merchant_id_from_token
from payments.models.merchantinfo import MerchantInfo
from payments.models.orderinfo import OrderInfo

logger = logging.getLogger(__name__)

MAX_ORDERS_PER_ISSUE = 100
MAX_DESCRIPTION_LENGTH = 2000
MAX_MESSAGE_LENGTH = 2000
MAX_ISSUES_PER_HOUR = 10
MAX_MESSAGES_PER_HOUR = 60
VALID_ISSUE_TYPES = {value for value, _label in MerchantIssue.ISSUE_TYPE_CHOICES}


def serialize_message(message):
    return {
        "id": message.id,
        "sender": message.sender,
        "body": message.body,
        "created_at": message.created_at,
    }


def serialize_issue(issue, include_messages=False):
    data = {
        "id": issue.id,
        "reference": issue.reference,
        "issue_type": issue.issue_type,
        "issue_type_label": issue.get_issue_type_display(),
        "orders": issue.orders,
        "description": issue.description,
        "status": issue.status,
        "status_label": issue.get_status_display(),
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
    }
    if include_messages:
        data["messages"] = [serialize_message(message) for message in issue.messages.all()]
    return data


class _MerchantAuthMixin:
    def _merchant(self, request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        merchant_id = get_merchant_id_from_token(token)
        if not merchant_id:
            return None
        return MerchantInfo.objects.filter(id=merchant_id).first()


class MerchantIssues(_MerchantAuthMixin, APIView):
    def get(self, request):
        merchant = self._merchant(request)
        if merchant is None:
            return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
        issues = MerchantIssue.objects.filter(merchant_id=merchant.id)[:200]
        return Response({"results": [serialize_issue(issue) for issue in issues]})

    def post(self, request):
        merchant = self._merchant(request)
        if merchant is None:
            return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        data = request.data
        issue_type = str(data.get("issue_type") or "").strip()
        description = str(data.get("description") or "").strip()
        raw_orders = data.get("orders")

        if issue_type not in VALID_ISSUE_TYPES:
            return Response({"error": "Choose what the issue is about."}, status=status.HTTP_400_BAD_REQUEST)
        if not description:
            return Response({"error": "Describe the issue so we can help."}, status=status.HTTP_400_BAD_REQUEST)
        if len(description) > MAX_DESCRIPTION_LENGTH:
            return Response(
                {"error": f"The description must be {MAX_DESCRIPTION_LENGTH} characters or fewer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(raw_orders, list) or not raw_orders:
            return Response({"error": "Select at least one order."}, status=status.HTTP_400_BAD_REQUEST)

        order_ids = list(dict.fromkeys(str(value or "").strip() for value in raw_orders if str(value or "").strip()))
        if not order_ids:
            return Response({"error": "Select at least one order."}, status=status.HTTP_400_BAD_REQUEST)
        if len(order_ids) > MAX_ORDERS_PER_ISSUE:
            return Response(
                {"error": f"You can report up to {MAX_ORDERS_PER_ISSUE} orders in one issue."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        recent = MerchantIssue.objects.filter(
            merchant_id=merchant.id, created_at__gte=timezone.now() - timedelta(hours=1)
        ).count()
        if recent >= MAX_ISSUES_PER_HOUR:
            return Response(
                {"error": "Too many issues raised in the last hour. Please try again in a little while."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Only this merchant's own orders; the id may be the merchant order id or the aggregator order id.
        matches = (
            OrderInfo.objects.filter(merchant_id=merchant.id)
            .filter(Q(merchant_order_id__in=order_ids) | Q(pa_order_id__in=order_ids))
            .select_related("shipment_id")
        )
        found = {}
        for order in matches:
            for key in (order.merchant_order_id, order.pa_order_id):
                if key in order_ids and key not in found:
                    found[key] = order
        missing = [order_id for order_id in order_ids if order_id not in found]
        if missing:
            shown = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
            return Response(
                {"error": f"These order IDs were not found on your account: {shown}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        orders_snapshot = []
        seen_orders = set()
        for order_id in order_ids:
            order = found[order_id]
            if order.id in seen_orders:
                continue
            seen_orders.add(order.id)
            shipment = order.shipment_id
            orders_snapshot.append(
                {
                    "order_id": order_id,
                    "merchant_order_id": order.merchant_order_id,
                    "pa_order_id": order.pa_order_id or "",
                    "awb": getattr(shipment, "awb", "") or "",
                    "courier": getattr(shipment, "courier", "") or "",
                    "delivery_status": getattr(shipment, "status", "") or "",
                    "payment_state": order.payment_state or "",
                    "amount": str(order.order_amount),
                }
            )

        issue = MerchantIssue.objects.create(
            merchant_id=merchant.id,
            merchant_name=merchant.merchant_name,
            merchant_email=merchant.merchant_email,
            issue_type=issue_type,
            orders=orders_snapshot,
            description=description,
        )
        # The description is the first message of the conversation.
        MerchantIssueMessage.objects.create(issue=issue, sender=MerchantIssueMessage.SENDER_MERCHANT, body=description)

        # Build the automatic report for each order now, while the merchant's view and our data are as they were when
        # they complained. A problem building it must never stop the ticket from being raised.
        reports = {}
        seen_reports = set()
        for order_id in order_ids:
            order = found[order_id]
            if order.id in seen_reports:
                continue
            seen_reports.add(order.id)
            row = next((item for item in orders_snapshot if item["merchant_order_id"] == order.merchant_order_id), None)
            try:
                reports[order.merchant_order_id] = build_order_report(issue_type, order, row)
            except Exception:
                logger.exception("Could not build the report for issue %s order %s", issue.id, order.merchant_order_id)
        if reports:
            issue.report = reports
            issue.save(update_fields=["report"])
        return Response(serialize_issue(issue, include_messages=True), status=status.HTTP_201_CREATED)


class MerchantIssueDetail(_MerchantAuthMixin, APIView):
    def get(self, request, issue_id):
        merchant = self._merchant(request)
        if merchant is None:
            return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
        issue = MerchantIssue.objects.filter(id=issue_id, merchant_id=merchant.id).first()
        if issue is None:
            return Response({"error": "Issue not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_issue(issue, include_messages=True))


class MerchantIssueMessages(_MerchantAuthMixin, APIView):
    def post(self, request, issue_id):
        merchant = self._merchant(request)
        if merchant is None:
            return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
        issue = MerchantIssue.objects.filter(id=issue_id, merchant_id=merchant.id).first()
        if issue is None:
            return Response({"error": "Issue not found."}, status=status.HTTP_404_NOT_FOUND)

        body = str(request.data.get("body") or "").strip()
        if not body:
            return Response({"error": "Write a message first."}, status=status.HTTP_400_BAD_REQUEST)
        if len(body) > MAX_MESSAGE_LENGTH:
            return Response(
                {"error": f"A message must be {MAX_MESSAGE_LENGTH} characters or fewer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        recent = MerchantIssueMessage.objects.filter(
            issue__merchant_id=merchant.id,
            sender=MerchantIssueMessage.SENDER_MERCHANT,
            created_at__gte=timezone.now() - timedelta(hours=1),
        ).count()
        if recent >= MAX_MESSAGES_PER_HOUR:
            return Response(
                {"error": "Too many messages sent in the last hour. Please try again in a little while."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        message = MerchantIssueMessage.objects.create(issue=issue, sender=MerchantIssueMessage.SENDER_MERCHANT, body=body)
        # A message on a resolved issue reopens it so the admin sees it needs attention again.
        if issue.status == MerchantIssue.STATUS_RESOLVED:
            issue.status = MerchantIssue.STATUS_NEW
        issue.save()  # bumps updated_at
        return Response(serialize_message(message), status=status.HTTP_201_CREATED)
