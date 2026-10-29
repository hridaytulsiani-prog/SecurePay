from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.views import Response

from adminpanel.models import AdminUserRole, HighValueDecisionReview, MerchantIssue
from adminpanel.permissions import AdminAPIView, get_admin_role
from payments.models import PAProtectedOrder
from payments.services.pa_control import audit, schedule_aggregator_request


User = get_user_model()
HIGH_VALUE_REVIEW_THRESHOLD_MINOR = 5000 * 100


def requires_owner_review(protected_order):
    return protected_order.currency.upper() == "INR" and protected_order.amount_minor >= HIGH_VALUE_REVIEW_THRESHOLD_MINOR


def serialize_owner_review(review):
    protected = review.protected_order
    order = protected.order
    order_key = ""
    if order is not None:
        order_key = order.merchant_order_id or order.pa_order_id or str(order.pk)
    report_url = ""
    if review.issue_id and order_key:
        report_url = f"/dashboard/merchant-issues/{review.issue_id}/report?order={quote(order_key)}"
    elif order_key:
        report_url = f"/dashboard/order-check?order={quote(order_key)}"
    return {
        "id": review.id,
        "vaultpay_order_id": protected.vaultpay_order_id,
        "merchant_id": protected.merchant_id,
        "merchant_order_id": protected.merchant_order_id,
        "merchant_name": protected.merchant.merchant_name,
        "decision": review.decision,
        "amount_minor": review.amount_minor,
        "currency": review.currency,
        "status": review.status,
        "requested_by": review.requested_by.username if review.requested_by else "System",
        "assigned_owner": {
            "id": review.assigned_owner_id,
            "username": review.assigned_owner.username,
            "email": review.assigned_owner.email,
        },
        "reviewed_by": review.reviewed_by.username if review.reviewed_by else "",
        "reviewer_note": review.reviewer_note,
        "created_at": review.created_at.isoformat(),
        "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at else None,
        "report_url": report_url,
        "decision_window": (protected.metadata or {}).get("decision_window"),
        "aggregator_request": (protected.metadata or {}).get("aggregator_request"),
    }


def review_queryset():
    return HighValueDecisionReview.objects.select_related(
        "protected_order",
        "protected_order__order",
        "protected_order__merchant",
        "requested_by",
        "assigned_owner",
        "reviewed_by",
        "issue",
    )


class OwnerReviewerListView(AdminAPIView):
    # Operations users can assign a high-value decision for owner review.
    # This only lists eligible owners; it does not execute a PA action.
    required_permission = "orders.view"

    def get(self, request):
        users = User.objects.filter(is_active=True, is_staff=True).filter(
            Q(is_superuser=True) | Q(admin_role__role=AdminUserRole.ROLE_OWNER)
        ).distinct().order_by("username")
        return Response({
            "results": [
                {"id": user.id, "username": user.username, "email": user.email}
                for user in users
            ]
        })


class OwnerDecisionReviewListCreateView(AdminAPIView):
    def get_required_permission(self, request):
        # Submitting a review does not move money. Only the assigned owner can
        # approve it and start the protected 10-minute PA decision window.
        return "owner_reviews.manage" if request.method == "GET" else "orders.view"

    def initial(self, request, *args, **kwargs):
        self.required_permission = self.get_required_permission(request)
        super().initial(request, *args, **kwargs)

    def get(self, request):
        rows = review_queryset().filter(assigned_owner=request.admin_user)
        requested_status = (request.GET.get("status") or "PENDING").upper()
        if requested_status != "ALL":
            rows = rows.filter(status=requested_status)
        return Response({"results": [serialize_owner_review(row) for row in rows[:100]]})

    def post(self, request):
        vaultpay_order_id = (request.data.get("vaultpay_order_id") or "").strip()
        decision = (request.data.get("decision") or "").upper()
        owner_id = request.data.get("owner_id")
        if decision not in {HighValueDecisionReview.DECISION_RELEASE, HighValueDecisionReview.DECISION_REFUND}:
            return Response({"error": "decision must be RELEASE or REFUND."}, status=400)

        protected = PAProtectedOrder.objects.filter(vaultpay_order_id=vaultpay_order_id).first()
        if protected is None:
            return Response({"error": "Protected order not found."}, status=404)
        if not requires_owner_review(protected):
            return Response({"error": "This order does not require high-value owner review."}, status=400)
        if (protected.metadata or {}).get("aggregator_request"):
            return Response({"error": "A final request has already been sent for this order."}, status=400)
        if ((protected.metadata or {}).get("decision_window") or {}).get("status") == "PENDING":
            return Response({"error": "This order already has a staged financial decision."}, status=400)

        owner = User.objects.filter(id=owner_id, is_active=True, is_staff=True).first()
        if owner is None or get_admin_role(owner) != AdminUserRole.ROLE_OWNER:
            return Response({"error": "Select an active owner account for review."}, status=400)
        issue = MerchantIssue.objects.filter(id=request.data.get("issue_id")).first() if request.data.get("issue_id") else None

        try:
            with transaction.atomic():
                protected = PAProtectedOrder.objects.select_for_update().get(pk=protected.pk)
                existing = review_queryset().filter(
                    protected_order=protected,
                    status=HighValueDecisionReview.STATUS_PENDING,
                ).first()
                if existing:
                    return Response({"error": f"This order is already awaiting review from {existing.assigned_owner.username}."}, status=400)
                review = HighValueDecisionReview.objects.create(
                    protected_order=protected,
                    issue=issue,
                    decision=decision,
                    amount_minor=protected.amount_minor,
                    currency=protected.currency,
                    requested_by=request.admin_user,
                    assigned_owner=owner,
                )
                audit(
                    protected,
                    "OWNER_REVIEW_REQUESTED",
                    f"{decision} decision assigned to {owner.username} for owner review",
                    {"review_id": review.id, "requested_by": request.admin_user.username, "assigned_owner": owner.username},
                )
        except IntegrityError:
            return Response({"error": "This order already has a pending owner review."}, status=400)

        review = review_queryset().get(pk=review.pk)
        return Response({"review": serialize_owner_review(review)}, status=status.HTTP_201_CREATED)


class OwnerDecisionReviewActionView(AdminAPIView):
    required_permission = "owner_reviews.manage"

    def post(self, request, review_id):
        action = (request.data.get("action") or "").upper()
        if action not in {"APPROVE", "REJECT"}:
            return Response({"error": "action must be APPROVE or REJECT."}, status=400)

        with transaction.atomic():
            # Lock only the review table. Applying FOR UPDATE to the fully
            # select_related queryset makes PostgreSQL reject nullable joins.
            review = HighValueDecisionReview.objects.select_for_update().filter(
                id=review_id,
                assigned_owner=request.admin_user,
            ).first()
            if review is None:
                return Response({"error": "Review request not found."}, status=404)
            if review.status != HighValueDecisionReview.STATUS_PENDING:
                return Response({"error": "This review has already been completed."}, status=400)

            note = (request.data.get("note") or "").strip()
            if action == "APPROVE":
                try:
                    schedule_aggregator_request(review.protected_order, review.decision, actor=request.admin_user)
                except ValueError as exc:
                    return Response({"error": str(exc)}, status=400)
                review.status = HighValueDecisionReview.STATUS_APPROVED
                event_type = "OWNER_REVIEW_APPROVED"
                message = f"{review.decision} decision approved by {request.admin_user.username}; 10-minute window started"
            else:
                review.status = HighValueDecisionReview.STATUS_REJECTED
                event_type = "OWNER_REVIEW_REJECTED"
                message = f"{review.decision} decision rejected by {request.admin_user.username}"
            review.reviewed_by = request.admin_user
            review.reviewer_note = note
            review.reviewed_at = timezone.now()
            review.save(update_fields=["status", "reviewed_by", "reviewer_note", "reviewed_at", "updated_at"])
            audit(
                review.protected_order,
                event_type,
                message,
                {"review_id": review.id, "reviewer_note": note},
            )

        review = review_queryset().get(pk=review.pk)
        return Response({"review": serialize_owner_review(review)})
