"""Owner-only "Customer issues" tickets (same idea as the merchant issues, for customers).

    GET   /adminpanel/customer-issues/                 paginated list with status counts (filters: status, type, q)
    POST  /adminpanel/customer-issues/                 create a ticket (the admin app's test form)
    PATCH /adminpanel/customer-issues/<id>/            change the status
    POST  /adminpanel/customer-issues/<id>/messages/   a chat message ("sender": "admin" by default, "customer" to test)
    POST  /adminpanel/customer-issues/<id>/notes/      an internal note (admins only)
"""

import json
import logging
import os
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Count, Prefetch, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView, Response

from adminpanel.models import CustomerIssue, CustomerIssueAttachment, CustomerIssueMessage, CustomerIssueNote
from adminpanel.permissions import AdminAPIView
from payments.models.orderinfo import OrderInfo

logger = logging.getLogger(__name__)
MAX_TEXT = 2000
MAX_REPORTS_PER_IP_PER_HOUR = 5

# Photos a customer can attach to a report.
MAX_PHOTOS = 4
MAX_PHOTO_BYTES = 5 * 1024 * 1024
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
# The first bytes of real JPEG / PNG / WebP files (so a renamed file is not accepted as a photo).
PHOTO_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF")
# Problems where a photo is needed to look into it.
PHOTO_REQUIRED_TYPES = {"damaged", "wrong_item"}

YES_NO = {"yes": "Yes", "no": "No", "not_sure": "Not sure"}
# Extra questions per kind of problem. Keep in sync with QUESTIONS in
# securepay_client/src/pages/ReportIssue.jsx (and the customer-next copy).
# type "choice" answers must be a key of YES_NO; "text" and "number" are free text.
QUESTIONS = {
    "not_received": [
        {"key": "someone_else_received", "label": "Did someone else receive the parcel?", "type": "choice"},
        {"key": "agent_contacted", "label": "Did you contact the delivery agent?", "type": "choice"},
    ],
    "wrong_item": [
        {"key": "expected_item", "label": "What did you order?", "type": "text", "required": True},
        {"key": "received_item", "label": "What did you receive instead?", "type": "text", "required": True},
    ],
    "damaged": [
        {"key": "damaged_part", "label": "What is damaged?", "type": "text", "required": True},
        {"key": "unboxing_video", "label": "Do you have an unboxing video?", "type": "choice"},
    ],
    "delayed": [
        {"key": "promised_date", "label": "Delivery date you were promised", "type": "text"},
        {"key": "courier_contacted", "label": "Did you contact the courier?", "type": "choice"},
    ],
    "refund": [
        {"key": "refund_amount", "label": "Refund amount you expect", "type": "number"},
        {"key": "payment_reference", "label": "Payment reference or UTR", "type": "text"},
    ],
    "other": [],
}
VALID_STATUSES = {value for value, _label in CustomerIssue.STATUS_CHOICES}
VALID_TYPES = {value for value, _label in CustomerIssue.ISSUE_TYPE_CHOICES}


def _clean_answers(issue_type, raw):
    """Turn the customer's answers into [{"key", "label", "value"}] for this kind of problem, or return an error text."""
    if not isinstance(raw, dict):
        raw = {}
    details = []
    for question in QUESTIONS.get(issue_type, []):
        value = str(raw.get(question["key"]) or "").strip()
        if not value:
            if question.get("required"):
                return None, f"Please answer: {question['label']}"
            continue
        if question["type"] == "choice":
            if value not in YES_NO:
                return None, f"Please answer: {question['label']}"
            value = YES_NO[value]
        elif question["type"] == "number":
            try:
                float(value)
            except ValueError:
                return None, f"Enter a number for: {question['label']}"
        details.append({"key": question["key"], "label": question["label"], "value": value[:300]})
    return details, ""


def _check_photos(issue_type, files):
    """An error text when the uploaded photos are not acceptable, else an empty string."""
    if issue_type in PHOTO_REQUIRED_TYPES and not files:
        return "Please attach a photo so we can check this."
    if len(files) > MAX_PHOTOS:
        return f"You can attach up to {MAX_PHOTOS} photos."
    for upload in files:
        if os.path.splitext(upload.name)[1].lower() not in PHOTO_EXTENSIONS:
            return "Photos must be JPG, PNG or WebP files."
        if upload.size > MAX_PHOTO_BYTES:
            return "Each photo must be 5 MB or smaller."
        head = upload.read(12)
        upload.seek(0)
        if not head.startswith(PHOTO_SIGNATURES):
            return "One of the files is not a valid photo."
    return ""


def _serialize(issue):
    messages = list(issue.messages.all())
    last = messages[-1] if messages else None
    return {
        "id": issue.id,
        "reference": issue.reference,
        "customer_name": issue.customer_name,
        "customer_phone": issue.customer_phone,
        "customer_email": issue.customer_email,
        "order_ref": issue.order_ref,
        "order": issue.order,
        "issue_type": issue.issue_type,
        "issue_type_label": issue.get_issue_type_display(),
        "description": issue.description,
        "details": issue.details,
        "attachments": [
            {"id": a.id, "url": a.file.url, "name": a.original_name}
            for a in issue.attachments.all()
        ],
        "status": issue.status,
        "status_label": issue.get_status_display(),
        "messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "body": m.body,
                "admin_name": m.admin_user.username if m.admin_user else "",
                "created_at": m.created_at,
            }
            for m in messages
        ],
        "awaiting_reply": bool(last and last.sender == CustomerIssueMessage.SENDER_CUSTOMER),
        "notes": [
            {"id": n.id, "note": n.note, "admin_name": n.admin_user.username if n.admin_user else "", "created_at": n.created_at}
            for n in issue.notes.all()
        ],
        "handled_by": issue.handled_by.username if issue.handled_by else "",
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
    }


def _queryset():
    return CustomerIssue.objects.select_related("handled_by").prefetch_related(
        Prefetch("messages", queryset=CustomerIssueMessage.objects.select_related("admin_user")),
        Prefetch("notes", queryset=CustomerIssueNote.objects.select_related("admin_user")),
        "attachments",
    )


def _find_order(ref):
    try:
        return _lookup_order(ref)
    except Exception:
        # A problem finding the order must never stop a customer's report from being saved.
        logger.exception("Customer issue: order lookup failed for %r", ref)
        return {}


def _lookup_order(ref):
    """The order a customer typed (an order ID or an AWB), as a small summary dict; {} when nothing matches."""
    order = (
        OrderInfo.objects.select_related("merchant", "shipment_id")
        .filter(Q(merchant_order_id=ref) | Q(pa_order_id=ref) | Q(shipment_id__awb=ref))
        .order_by("-order_date")
        .first()
    )
    if order is None:
        return {}
    shipment = order.shipment_id
    return {
        "merchant_order_id": order.merchant_order_id,
        "awb": getattr(shipment, "awb", "") or "",
        "courier": getattr(shipment, "courier", "") or "",
        "merchant_name": order.merchant.merchant_name if order.merchant_id else "",
        "amount": str(order.order_amount),
        "delivery_status": getattr(shipment, "status", "") or "",
        "payment_state": order.payment_state,
    }


class AdminCustomerIssueListView(AdminAPIView):
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

        qs = _queryset()
        if status_filter in VALID_STATUSES:
            qs = qs.filter(status=status_filter)
        if type_filter in VALID_TYPES:
            qs = qs.filter(issue_type=type_filter)
        if q:
            ref_id = q.upper().removeprefix("CIS-").lstrip("0")
            query = (
                Q(customer_name__icontains=q)
                | Q(customer_email__icontains=q)
                | Q(customer_phone__icontains=q)
                | Q(order_ref__icontains=q)
                | Q(description__icontains=q)
            )
            if ref_id.isdigit():
                query |= Q(id=int(ref_id))
            qs = qs.filter(query)

        total = qs.count()
        start = (page - 1) * limit
        counts = {value: 0 for value in VALID_STATUSES}
        for row in CustomerIssue.objects.values("status").annotate(total=Count("id")):
            counts[row["status"]] = row["total"]
        return Response(
            {
                "results": [_serialize(issue) for issue in qs[start:start + limit]],
                "page": page,
                "total_pages": max(1, (total + limit - 1) // limit),
                "total": total,
                "counts": counts,
            }
        )

    def post(self, request):
        data = request.data
        name = str(data.get("customer_name") or "").strip()[:120]
        order_ref = str(data.get("order_ref") or "").strip()[:80]
        issue_type = str(data.get("issue_type") or "").strip()
        description = str(data.get("description") or "").strip()
        if not name or not order_ref or not description:
            return Response({"error": "Fill in the customer name, the order ID and what went wrong."}, status=status.HTTP_400_BAD_REQUEST)
        if issue_type not in VALID_TYPES:
            return Response({"error": "Pick what the problem is."}, status=status.HTTP_400_BAD_REQUEST)
        if len(description) > MAX_TEXT:
            return Response({"error": f"The description must be {MAX_TEXT} characters or fewer."}, status=status.HTTP_400_BAD_REQUEST)

        issue = CustomerIssue.objects.create(
            customer_name=name,
            customer_phone=str(data.get("customer_phone") or "").strip()[:30],
            customer_email=str(data.get("customer_email") or "").strip()[:254],
            order_ref=order_ref,
            order=_find_order(order_ref),
            issue_type=issue_type,
            description=description,
        )
        CustomerIssueMessage.objects.create(issue=issue, sender=CustomerIssueMessage.SENDER_CUSTOMER, body=description)
        return Response(_serialize(_queryset().get(id=issue.id)), status=status.HTTP_201_CREATED)


class AdminCustomerIssueDetailView(AdminAPIView):
    required_permission = "contact_messages.manage"

    def patch(self, request, issue_id):
        issue = _queryset().filter(id=issue_id).first()
        if issue is None:
            return Response({"error": "Issue not found."}, status=status.HTTP_404_NOT_FOUND)
        new_status = str(request.data.get("status") or "").strip()
        if new_status not in VALID_STATUSES:
            return Response({"error": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST)
        issue.status = new_status
        issue.handled_by = request.admin_user
        issue.handled_at = timezone.now()
        issue.save()
        return Response(_serialize(issue))


class AdminCustomerIssueMessageView(AdminAPIView):
    required_permission = "contact_messages.manage"

    def post(self, request, issue_id):
        issue = _queryset().filter(id=issue_id).first()
        if issue is None:
            return Response({"error": "Issue not found."}, status=status.HTTP_404_NOT_FOUND)
        body = str(request.data.get("body") or "").strip()
        if not body:
            return Response({"error": "Write a message first."}, status=status.HTTP_400_BAD_REQUEST)
        if len(body) > MAX_TEXT:
            return Response({"error": f"A message must be {MAX_TEXT} characters or fewer."}, status=status.HTTP_400_BAD_REQUEST)

        from_customer = str(request.data.get("sender") or "").strip() == CustomerIssueMessage.SENDER_CUSTOMER
        CustomerIssueMessage.objects.create(
            issue=issue,
            sender=CustomerIssueMessage.SENDER_CUSTOMER if from_customer else CustomerIssueMessage.SENDER_ADMIN,
            body=body,
            admin_user=None if from_customer else request.admin_user,
        )
        if not from_customer:
            if issue.status == CustomerIssue.STATUS_NEW:
                issue.status = CustomerIssue.STATUS_IN_PROGRESS
            issue.handled_by = request.admin_user
            issue.handled_at = timezone.now()
        issue.save()
        return Response(_serialize(_queryset().get(id=issue.id)), status=status.HTTP_201_CREATED)


class AdminCustomerIssueNoteView(AdminAPIView):
    required_permission = "contact_messages.manage"

    def post(self, request, issue_id):
        issue = _queryset().filter(id=issue_id).first()
        if issue is None:
            return Response({"error": "Issue not found."}, status=status.HTTP_404_NOT_FOUND)
        text = str(request.data.get("note") or "").strip()
        if not text:
            return Response({"error": "Write a note first."}, status=status.HTTP_400_BAD_REQUEST)
        if len(text) > MAX_TEXT:
            return Response({"error": f"A note must be {MAX_TEXT} characters or fewer."}, status=status.HTTP_400_BAD_REQUEST)
        CustomerIssueNote.objects.create(issue=issue, note=text, admin_user=request.admin_user)
        return Response(_serialize(_queryset().get(id=issue.id)), status=status.HTTP_201_CREATED)


class CustomerIssueSubmitView(APIView):
    """POST /adminpanel/customer-issue/ - the public "Report a problem with my order" page (no login).

    Drops bots (honeypot field) and limits each IP to a few reports per hour, like the "Get in touch" form.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data
        if str(data.get("website") or "").strip():
            return Response({"ok": True}, status=status.HTTP_201_CREATED)

        name = str(data.get("name") or "").strip()
        email = str(data.get("email") or "").strip()
        phone = str(data.get("phone") or "").strip()
        order_ref = str(data.get("order_id") or "").strip()
        issue_type = str(data.get("issue_type") or "").strip()
        description = str(data.get("description") or "").strip()
        photos = request.FILES.getlist("photos") if hasattr(request, "FILES") else []
        try:
            answers = json.loads(data.get("answers") or "{}")
        except (TypeError, ValueError):
            answers = {}

        if not name or not order_ref or not description:
            return Response({"error": "Your name, the order ID and what went wrong are required."}, status=status.HTTP_400_BAD_REQUEST)
        if not email and not phone:
            return Response({"error": "Add an email or a phone number so we can reach you."}, status=status.HTTP_400_BAD_REQUEST)
        if issue_type not in VALID_TYPES:
            return Response({"error": "Pick what the problem is."}, status=status.HTTP_400_BAD_REQUEST)
        if len(name) > 120 or len(order_ref) > 80 or len(phone) > 30:
            return Response({"error": "One of the fields is too long."}, status=status.HTTP_400_BAD_REQUEST)
        if len(description) > MAX_TEXT:
            return Response({"error": f"Please keep it under {MAX_TEXT} characters."}, status=status.HTTP_400_BAD_REQUEST)
        if email:
            try:
                validate_email(email)
            except ValidationError:
                return Response({"error": "Enter a valid email address."}, status=status.HTTP_400_BAD_REQUEST)
        details, answer_error = _clean_answers(issue_type, answers)
        if answer_error:
            return Response({"error": answer_error}, status=status.HTTP_400_BAD_REQUEST)
        photo_error = _check_photos(issue_type, photos)
        if photo_error:
            return Response({"error": photo_error}, status=status.HTTP_400_BAD_REQUEST)

        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip_address = (forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")) or None
        if ip_address:
            recent = CustomerIssue.objects.filter(ip_address=ip_address, created_at__gte=timezone.now() - timedelta(hours=1)).count()
            if recent >= MAX_REPORTS_PER_IP_PER_HOUR:
                return Response({"error": "Too many reports sent. Please try again in a little while."}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        issue = CustomerIssue.objects.create(
            customer_name=name,
            customer_phone=phone,
            customer_email=email,
            order_ref=order_ref,
            order=_find_order(order_ref),
            issue_type=issue_type,
            description=description,
            details=details,
            ip_address=ip_address,
        )
        for upload in photos:
            CustomerIssueAttachment.objects.create(issue=issue, file=upload, original_name=os.path.basename(upload.name)[:200])
        CustomerIssueMessage.objects.create(issue=issue, sender=CustomerIssueMessage.SENDER_CUSTOMER, body=description)
        return Response({"ok": True, "reference": issue.reference}, status=status.HTTP_201_CREATED)


def customer_issues_for_order(order):
    """Customer tickets about this order (matched on the order ID or AWB the customer typed), newest first."""
    refs = [value for value in (order.merchant_order_id, order.pa_order_id, getattr(order.shipment_id, "awb", "")) if value]
    rows = (
        CustomerIssue.objects.filter(order_ref__in=refs)
        .prefetch_related("notes__admin_user", "attachments")
        .order_by("-created_at")[:10]
    )
    return [
        {
            "id": row.id,
            "reference": row.reference,
            "issue_type_label": row.get_issue_type_display(),
            "customer_name": row.customer_name,
            "description": row.description,
            "status": row.status,
            "status_label": row.get_status_display(),
            "created_at": row.created_at.isoformat(),
            "photo_count": len(row.attachments.all()),
            "notes": [
                {"note": n.note, "by": n.admin_user.username if n.admin_user else "", "created_at": n.created_at.isoformat()}
                for n in row.notes.all()
            ],
        }
        for row in rows
    ]
