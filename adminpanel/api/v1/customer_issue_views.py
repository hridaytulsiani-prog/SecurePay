"""Owner-only "Customer issues" tickets (same idea as the merchant issues, for customers).

    GET   /adminpanel/customer-issues/                 paginated list with status counts (filters: status, type, q)
    POST  /adminpanel/customer-issues/                 create a ticket (the admin app's test form)
    PATCH /adminpanel/customer-issues/<id>/            change the status
    POST  /adminpanel/customer-issues/<id>/messages/   a chat message ("sender": "admin" by default, "customer" to test)
    POST  /adminpanel/customer-issues/<id>/notes/      an internal note (admins only)
    POST  /adminpanel/customer-issues/<id>/notify-merchant/ forward the issue to the responsible merchant
"""

import json
import logging
import os
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db.models import Count, Prefetch, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView, Response

from adminpanel.models import CustomerIssue, CustomerIssueAttachment, CustomerIssueMessage, CustomerIssueMessageAttachment, CustomerIssueNote
from adminpanel.permissions import AdminAPIView
from adminpanel.api.v1.payment_ids import payment_id_for_order
from payments.models.payment_lifecycle import PaymentNotification
from payments.models.orderinfo import OrderInfo

logger = logging.getLogger(__name__)
MAX_TEXT = 2000
MAX_REPORTS_PER_IP_PER_HOUR = 5

# Photos a customer can attach to a report.
MAX_PHOTOS = 4
MAX_PHOTO_BYTES = 5 * 1024 * 1024
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_MESSAGE_ATTACHMENTS = 4
MAX_MESSAGE_ATTACHMENT_BYTES = 10 * 1024 * 1024
MESSAGE_ATTACHMENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
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
    merchant_messages = [
        message
        for message in messages
        if message.sender == CustomerIssueMessage.SENDER_MERCHANT
        and message.channel == CustomerIssueMessage.CHANNEL_MERCHANT
    ]
    unread_merchant_messages = [
        message
        for message in merchant_messages
        if issue.merchant_messages_read_at is None or message.created_at > issue.merchant_messages_read_at
    ]
    order_summary = dict(issue.order or {})
    current_order = _find_order(issue.order_ref)
    if current_order:
        order_summary.update(current_order)
    return {
        "id": issue.id,
        "reference": issue.reference,
        "customer_name": issue.customer_name,
        "customer_phone": issue.customer_phone,
        "customer_email": issue.customer_email,
        "order_ref": issue.order_ref,
        "order": order_summary,
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
                "channel": m.channel,
                "body": m.body,
                "admin_name": m.admin_user.username if m.admin_user else "",
                "sender_name": (
                    m.admin_user.username
                    if m.sender == CustomerIssueMessage.SENDER_ADMIN and m.admin_user
                    else order_summary.get("merchant_name", "Merchant")
                    if m.sender == CustomerIssueMessage.SENDER_MERCHANT
                    else issue.customer_name
                ),
                "attachments": [
                    {
                        "id": attachment.id,
                        "url": attachment.file.url,
                        "name": attachment.original_name,
                        "content_type": attachment.content_type,
                    }
                    for attachment in m.attachments.all()
                ],
                "created_at": m.created_at,
            }
            for m in messages
        ],
        "awaiting_reply": bool(last and last.sender == CustomerIssueMessage.SENDER_CUSTOMER),
        "merchant_responded": bool(merchant_messages),
        "merchant_response_count": len(merchant_messages),
        "merchant_response_unread": bool(unread_merchant_messages),
        "merchant_response_unread_count": len(unread_merchant_messages),
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
        Prefetch("messages", queryset=CustomerIssueMessage.objects.select_related("admin_user").prefetch_related("attachments")),
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
        "merchant_id": order.merchant_id,
        "merchant_email": order.merchant.merchant_email if order.merchant_id else "",
        "payment_id": payment_id_for_order(order),
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
        merchant_id = (request.GET.get("merchant_id") or "").strip()

        qs = _queryset()
        if merchant_id.isdigit():
            order_refs = {
                reference
                for row in OrderInfo.objects.filter(merchant_id=int(merchant_id)).values_list(
                    "merchant_order_id", "pa_order_id", "shipment_id__awb"
                )
                for reference in row
                if reference
            }
            qs = qs.filter(order_ref__in=order_refs) if order_refs else qs.none()
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
        update_fields = []
        if "status" in request.data:
            new_status = str(request.data.get("status") or "").strip()
            if new_status not in VALID_STATUSES:
                return Response({"error": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST)
            issue.status = new_status
            issue.handled_by = request.admin_user
            issue.handled_at = timezone.now()
            update_fields.extend(["status", "handled_by", "handled_at"])

        if str(request.data.get("merchant_messages_read") or "").strip().lower() in {"true", "1", "yes"}:
            issue.merchant_messages_read_at = timezone.now()
            update_fields.append("merchant_messages_read_at")

        if not update_fields:
            return Response({"error": "No supported changes were supplied."}, status=status.HTTP_400_BAD_REQUEST)

        update_fields.append("updated_at")
        issue.save(update_fields=update_fields)
        return Response(_serialize(_queryset().get(id=issue.id)))


class AdminCustomerIssueMessageView(AdminAPIView):
    required_permission = "contact_messages.manage"

    def post(self, request, issue_id):
        issue = _queryset().filter(id=issue_id).first()
        if issue is None:
            return Response({"error": "Issue not found."}, status=status.HTTP_404_NOT_FOUND)
        body = str(request.data.get("body") or "").strip()
        attachments = request.FILES.getlist("attachments")
        if not body and not attachments:
            return Response({"error": "Write a message or attach a file first."}, status=status.HTTP_400_BAD_REQUEST)
        if len(body) > MAX_TEXT:
            return Response({"error": f"A message must be {MAX_TEXT} characters or fewer."}, status=status.HTTP_400_BAD_REQUEST)
        if len(attachments) > MAX_MESSAGE_ATTACHMENTS:
            return Response({"error": f"You can attach up to {MAX_MESSAGE_ATTACHMENTS} files."}, status=status.HTTP_400_BAD_REQUEST)
        for upload in attachments:
            if os.path.splitext(upload.name)[1].lower() not in MESSAGE_ATTACHMENT_EXTENSIONS:
                return Response({"error": "Attachments must be JPG, PNG, WebP or PDF files."}, status=status.HTTP_400_BAD_REQUEST)
            if upload.size > MAX_MESSAGE_ATTACHMENT_BYTES:
                return Response({"error": "Each attachment must be 10 MB or smaller."}, status=status.HTTP_400_BAD_REQUEST)

        from_customer = str(request.data.get("sender") or "").strip() == CustomerIssueMessage.SENDER_CUSTOMER
        to_merchant = str(request.data.get("channel") or "").strip() == CustomerIssueMessage.CHANNEL_MERCHANT
        message = CustomerIssueMessage.objects.create(
            issue=issue,
            sender=CustomerIssueMessage.SENDER_CUSTOMER if from_customer else CustomerIssueMessage.SENDER_ADMIN,
            channel=CustomerIssueMessage.CHANNEL_MERCHANT if to_merchant and not from_customer else CustomerIssueMessage.CHANNEL_CUSTOMER,
            body=body,
            admin_user=None if from_customer else request.admin_user,
        )
        for upload in attachments:
            CustomerIssueMessageAttachment.objects.create(
                message=message,
                file=upload,
                original_name=os.path.basename(upload.name)[:200],
                content_type=(getattr(upload, "content_type", "") or "")[:100],
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


class AdminCustomerIssueMerchantNotifyView(AdminAPIView):
    """Forward a customer issue to the merchant as both a portal notification and an email."""

    required_permission = "contact_messages.manage"

    def post(self, request, issue_id):
        issue = _queryset().filter(id=issue_id).first()
        if issue is None:
            return Response({"error": "Issue not found."}, status=status.HTTP_404_NOT_FOUND)

        order = (
            OrderInfo.objects.select_related("merchant", "customer_info")
            .filter(
                Q(merchant_order_id=issue.order_ref)
                | Q(pa_order_id=issue.order_ref)
                | Q(shipment_id__awb=issue.order_ref)
            )
            .order_by("-order_date")
            .first()
        )
        if order is None or order.merchant_id is None:
            return Response(
                {"error": "No responsible merchant could be found for this customer issue."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        requested_note_ids = request.data.get("public_call_note_ids", [])
        if not isinstance(requested_note_ids, list):
            return Response(
                {"error": "public_call_note_ids must be a list."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        selected_note_ids = {
            int(note_id)
            for note_id in requested_note_ids[:20]
            if str(note_id).isdigit()
        }
        selected_call_notes = [
            note.note[len("[call-note]"):].strip()
            for note in issue.notes.filter(id__in=selected_note_ids).order_by("created_at")
            if note.note.startswith("[call-note]") and note.note[len("[call-note]"):].strip()
        ]

        title = "Customer issue requires your attention"
        message = (
            f"EscroSafe forwarded customer issue {issue.reference} for Order ID "
            f"{order.merchant_order_id}: {issue.get_issue_type_display()}. "
            "Please review the order and contact EscroSafe if action is needed."
        )
        if selected_call_notes:
            message += " Customer call notes: " + " | ".join(selected_call_notes)
        metadata = {
            "source": "admin_customer_issue",
            "customer_issue_id": issue.id,
            "issue_reference": issue.reference,
            "issue_type": issue.get_issue_type_display(),
            "customer_name": issue.customer_name,
            "public_call_notes": selected_call_notes,
        }
        now = timezone.now()
        dashboard_notification, portal_created = PaymentNotification.objects.get_or_create(
            dedupe_key=f"DASHBOARD:customer_issue_forwarded:{issue.id}",
            defaults={
                "merchant": order.merchant,
                "order": order,
                "event_type": "customer_issue_forwarded",
                "channel": PaymentNotification.CHANNEL_DASHBOARD,
                "title": title,
                "message": message,
                "status": PaymentNotification.STATUS_SENT,
                "metadata": metadata,
                "sent_at": now,
            },
        )
        if selected_call_notes and not portal_created:
            dashboard_notification.message = message
            dashboard_notification.metadata = {**(dashboard_notification.metadata or {}), **metadata}
            dashboard_notification.save(update_fields=["message", "metadata"])

        if selected_call_notes:
            shared_notes_message = "Customer call notes shared by EscroSafe:\n\n" + "\n".join(
                f"{index}. {note}" for index, note in enumerate(selected_call_notes, start=1)
            )
            CustomerIssueMessage.objects.get_or_create(
                issue=issue,
                sender=CustomerIssueMessage.SENDER_ADMIN,
                channel=CustomerIssueMessage.CHANNEL_MERCHANT,
                body=shared_notes_message[:MAX_TEXT],
                defaults={"admin_user": request.admin_user},
            )

        email_notification, _ = PaymentNotification.objects.get_or_create(
            dedupe_key=f"EMAIL:customer_issue_forwarded:{issue.id}",
            defaults={
                "merchant": order.merchant,
                "order": order,
                "event_type": "customer_issue_forwarded",
                "channel": PaymentNotification.CHANNEL_EMAIL,
                "title": f"{title} - {order.merchant_order_id}",
                "message": message,
                "status": PaymentNotification.STATUS_PENDING,
                "metadata": metadata,
            },
        )

        merchant_email = (order.merchant.merchant_email or "").strip()
        email_already_sent = email_notification.status == PaymentNotification.STATUS_SENT
        email_sent = email_already_sent
        email_error = ""
        if not email_sent and merchant_email:
            email_lines = [
                    f"Hello {order.merchant.merchant_name},",
                    "",
                    "A customer issue has been forwarded for your attention.",
                    "",
                    f"Ticket ID: {issue.reference}",
                    f"Order ID: {order.merchant_order_id}",
                    f"Issue: {issue.get_issue_type_display()}",
                    f"Customer: {issue.customer_name}",
                    f"Customer report: {issue.description}",
                    "",
            ]
            if selected_call_notes:
                email_lines.extend(
                    ["Customer call notes selected by EscroSafe:"]
                    + [f"{index}. {note}" for index, note in enumerate(selected_call_notes, start=1)]
                    + [""]
                )
            email_lines.extend(
                [
                    "Please review this order in your EscroSafe merchant portal and coordinate with EscroSafe if action is required.",
                    "",
                    "EscroSafe Support",
                ]
            )
            email_body = "\n".join(email_lines)
            partnerships_email = getattr(settings, "PARTNERSHIPS_EMAIL", "partnerships@escrosafe.com")
            cc = [partnerships_email] if partnerships_email and partnerships_email.lower() != merchant_email.lower() else []
            try:
                EmailMessage(
                    subject=email_notification.title,
                    body=email_body,
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    to=[merchant_email],
                    cc=cc,
                ).send(fail_silently=False)
                email_notification.status = PaymentNotification.STATUS_SENT
                email_notification.sent_at = now
                email_notification.metadata = {**metadata, "cc": cc}
                email_notification.save(update_fields=["status", "sent_at", "metadata"])
                email_sent = True
            except Exception as exc:
                logger.exception("Failed to email customer issue %s to merchant %s", issue.reference, order.merchant_id)
                email_error = "The portal notification was sent, but the merchant email could not be delivered."
                email_notification.status = PaymentNotification.STATUS_FAILED
                email_notification.metadata = {**metadata, "error": str(exc)}
                email_notification.save(update_fields=["status", "metadata"])
        elif not merchant_email:
            email_error = "The portal notification was sent, but this merchant does not have an email address."

        return Response(
            {
                "ok": True,
                "already_notified": not portal_created and email_already_sent,
                "portal_sent": dashboard_notification.status == PaymentNotification.STATUS_SENT,
                "email_sent": email_sent,
                "email_error": email_error,
                "merchant_name": order.merchant.merchant_name,
                "merchant_email": merchant_email,
                "order_id": order.merchant_order_id,
                "issue_reference": issue.reference,
            },
            status=status.HTTP_201_CREATED if portal_created else status.HTTP_200_OK,
        )


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
        .prefetch_related("messages__admin_user", "notes__admin_user", "attachments")
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
            "messages": [
                {
                    "sender": message.sender,
                    "body": message.body,
                    "by": message.admin_user.username if message.admin_user else "",
                    "created_at": message.created_at.isoformat(),
                }
                for message in row.messages.all()
            ],
            "notes": [
                {"note": n.note, "by": n.admin_user.username if n.admin_user else "", "created_at": n.created_at.isoformat()}
                for n in row.notes.all()
            ],
        }
        for row in rows
    ]
