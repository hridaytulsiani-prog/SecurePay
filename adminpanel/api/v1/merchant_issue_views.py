"""Owner-only inbox for issues merchants raise from their dashboard (see merchant/v1/issues.py).

    GET   /adminpanel/merchant-issues/                    paginated list with status counts (filters: status, type, q)
    PATCH /adminpanel/merchant-issues/<id>/               change the status or the internal note
    POST  /adminpanel/merchant-issues/<id>/messages/      send a message to the merchant in the issue's conversation
"""

from django.db.models import Count, Prefetch, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.views import Response

from adminpanel.models import MerchantIssue, MerchantIssueMessage, MerchantIssueNote
from adminpanel.permissions import AdminAPIView
from adminpanel.api.v1.payment_ids import numeric_payment_id, payment_id_for_order
from payments.models.orderinfo import OrderInfo

MAX_NOTE_LENGTH = 2000
MAX_MESSAGE_LENGTH = 2000
VALID_STATUSES = {value for value, _label in MerchantIssue.STATUS_CHOICES}
VALID_ISSUE_TYPES = {value for value, _label in MerchantIssue.ISSUE_TYPE_CHOICES}


def _serialize_message(message):
    return {
        "id": message.id,
        "sender": message.sender,
        "body": message.body,
        "admin_name": message.admin_user.username if message.admin_user else "",
        "created_at": message.created_at,
    }


def _serialize_issue_order(order_summary):
    row = dict(order_summary or {})
    merchant_order_id = row.get("merchant_order_id") or row.get("order_id")
    awb = row.get("awb")
    query = Q()
    if merchant_order_id:
        query |= Q(merchant_order_id=merchant_order_id) | Q(pa_order_id=merchant_order_id)
    if awb:
        query |= Q(shipment_id__awb=awb)
    if query:
        order = OrderInfo.objects.filter(query).order_by("-order_date").first()
        if order:
            row["payment_id"] = payment_id_for_order(order)
    if not row.get("payment_id"):
        stable_reference = (
            row.get("pa_order_id")
            or row.get("order_id")
            or merchant_order_id
            or awb
        )
        if stable_reference:
            row["payment_id"] = numeric_payment_id(
                f"merchant-issue-order:{stable_reference}:{awb or ''}"
            )
    return row


def _serialize(issue):
    messages = list(issue.messages.all())
    last_message = messages[-1] if messages else None
    return {
        "id": issue.id,
        "reference": issue.reference,
        "merchant_id": issue.merchant_id,
        "merchant_name": issue.merchant_name,
        "merchant_email": issue.merchant_email,
        "issue_type": issue.issue_type,
        "issue_type_label": issue.get_issue_type_display(),
        "orders": [_serialize_issue_order(order) for order in (issue.orders or [])],
        "order_count": len(issue.orders or []),
        "description": issue.description,
        "status": issue.status,
        "status_label": issue.get_status_display(),
        "messages": [_serialize_message(message) for message in messages],
        # True when the merchant wrote last, i.e. the admin owes a reply.
        "awaiting_reply": bool(last_message and last_message.sender == MerchantIssueMessage.SENDER_MERCHANT),
        # Internal notes: shown only in the admin app (the merchant API never returns them).
        "notes": [
            {
                "id": note.id,
                "note": note.note,
                "admin_name": note.admin_user.username if note.admin_user else "",
                "created_at": note.created_at,
            }
            for note in issue.notes.all()
        ],
        "handled_by": issue.handled_by.username if issue.handled_by else "",
        "handled_at": issue.handled_at,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
    }


def _issues_queryset():
    return MerchantIssue.objects.select_related("handled_by").prefetch_related(
        Prefetch("messages", queryset=MerchantIssueMessage.objects.select_related("admin_user")),
        Prefetch("notes", queryset=MerchantIssueNote.objects.select_related("admin_user")),
    )


class AdminMerchantIssueListView(AdminAPIView):
    required_permission = "contact_messages.manage"

    def get(self, request):
        try:
            limit = min(max(int(request.GET.get("limit", 25)), 1), 200)
        except ValueError:
            limit = 25
        try:
            page = max(int(request.GET.get("page", 1)), 1)
        except ValueError:
            page = 1

        status_filter = (request.GET.get("status") or "").strip()
        type_filter = (request.GET.get("type") or "").strip()
        q = (request.GET.get("q") or "").strip()

        qs = _issues_queryset()
        if status_filter in VALID_STATUSES:
            qs = qs.filter(status=status_filter)
        if type_filter in VALID_ISSUE_TYPES:
            qs = qs.filter(issue_type=type_filter)
        if q:
            # Issue references look like ISS-00012; also match the numeric part.
            ref_id = q.upper().removeprefix("ISS-").lstrip("0")
            query = (
                Q(merchant_name__icontains=q)
                | Q(merchant_email__icontains=q)
                | Q(description__icontains=q)
                | Q(orders__icontains=q)
            )
            if ref_id.isdigit():
                query |= Q(id=int(ref_id))
            qs = qs.filter(query)

        total = qs.count()
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit

        counts = {value: 0 for value in VALID_STATUSES}
        for row in MerchantIssue.objects.values("status").annotate(total=Count("id")):
            counts[row["status"]] = row["total"]

        return Response(
            {
                "results": [_serialize(issue) for issue in qs[start:start + limit]],
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "counts": counts,
            }
        )


class AdminMerchantIssueDetailView(AdminAPIView):
    required_permission = "contact_messages.manage"

    def patch(self, request, issue_id):
        issue = _issues_queryset().filter(id=issue_id).first()
        if issue is None:
            return Response({"error": "Issue not found."}, status=status.HTTP_404_NOT_FOUND)

        changed = False
        if "status" in request.data:
            new_status = str(request.data.get("status") or "").strip()
            if new_status not in VALID_STATUSES:
                return Response({"error": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST)
            issue.status = new_status
            changed = True
        if "internal_note" in request.data:
            issue.internal_note = str(request.data.get("internal_note") or "").strip()[:MAX_NOTE_LENGTH]
            changed = True

        if changed:
            issue.handled_by = request.admin_user
            issue.handled_at = timezone.now()
            issue.save()
        return Response(_serialize(issue))


class AdminMerchantIssueMessageView(AdminAPIView):
    """POST - the admin writes to the merchant. A brand-new issue moves to "in progress" on the first reply."""

    required_permission = "contact_messages.manage"

    def post(self, request, issue_id):
        issue = _issues_queryset().filter(id=issue_id).first()
        if issue is None:
            return Response({"error": "Issue not found."}, status=status.HTTP_404_NOT_FOUND)

        body = str(request.data.get("body") or "").strip()
        if not body:
            return Response({"error": "Write a message first."}, status=status.HTTP_400_BAD_REQUEST)
        if len(body) > MAX_MESSAGE_LENGTH:
            return Response({"error": f"A message must be {MAX_MESSAGE_LENGTH} characters or fewer."}, status=status.HTTP_400_BAD_REQUEST)

        MerchantIssueMessage.objects.create(
            issue=issue, sender=MerchantIssueMessage.SENDER_ADMIN, body=body, admin_user=request.admin_user
        )
        if issue.status == MerchantIssue.STATUS_NEW:
            issue.status = MerchantIssue.STATUS_IN_PROGRESS
        issue.handled_by = request.admin_user
        issue.handled_at = timezone.now()
        issue.save()
        # Re-read so the response includes the message that was just added.
        return Response(_serialize(_issues_queryset().get(id=issue.id)), status=status.HTTP_201_CREATED)


class AdminMerchantIssueNoteView(AdminAPIView):
    """POST /adminpanel/merchant-issues/<id>/notes/ - add an internal note (admin app only, never shown to the merchant)."""

    required_permission = "contact_messages.manage"

    def post(self, request, issue_id):
        issue = _issues_queryset().filter(id=issue_id).first()
        if issue is None:
            return Response({"error": "Issue not found."}, status=status.HTTP_404_NOT_FOUND)

        text = str(request.data.get("note") or "").strip()
        if not text:
            return Response({"error": "Write a note first."}, status=status.HTTP_400_BAD_REQUEST)
        if len(text) > MAX_NOTE_LENGTH:
            return Response({"error": f"A note must be {MAX_NOTE_LENGTH} characters or fewer."}, status=status.HTTP_400_BAD_REQUEST)

        MerchantIssueNote.objects.create(issue=issue, note=text, admin_user=request.admin_user)
        return Response(_serialize(_issues_queryset().get(id=issue.id)), status=status.HTTP_201_CREATED)
