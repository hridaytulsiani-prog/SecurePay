"""Public "Get in touch" form (customer page) and the owner-only inbox for it.

ContactMessageSubmitView is public (no login) because shoppers are not
logged in. It validates input, drops obvious bots (honeypot field) and limits
each IP to a few messages per hour. The other views need an admin session
with the contact_messages.manage permission (owner only by default).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView, Response, status

from adminpanel.models import ContactMessage, ContactReply
from adminpanel.permissions import AdminAPIView

MAX_MESSAGES_PER_IP_PER_HOUR = 5
MAX_NAME_LENGTH = 120
MAX_ORDER_ID_LENGTH = 64
MAX_TOPIC_LENGTH = 80
MAX_MESSAGE_LENGTH = 2000
VALID_STATUSES = {choice[0] for choice in ContactMessage.STATUS_CHOICES}
VALID_SOURCES = {choice[0] for choice in ContactMessage.SOURCE_CHOICES}
logger = logging.getLogger(__name__)


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def _serialize(message):
    return {
        "id": message.id,
        "source": message.source,
        "topic": message.topic,
        "name": message.name,
        "email": message.email,
        "order_id": message.order_id,
        "message": message.message,
        "status": message.status,
        "status_label": message.get_status_display(),
        "internal_note": message.internal_note,
        "handled_by": message.handled_by.username if message.handled_by else "",
        "handled_at": message.handled_at,
        "created_at": message.created_at,
        "replies": [
            {
                "id": reply.id,
                "subject": reply.subject,
                "body": reply.body,
                "sent_by": reply.sent_by.username if reply.sent_by else "",
                "sent_at": reply.sent_at,
            }
            for reply in message.replies.select_related("sent_by")[:10]
        ],
    }


class ContactMessageSubmitView(APIView):
    """POST /adminpanel/contact/ - store a message from the public form."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        data = request.data

        # Honeypot: real visitors never see or fill this hidden field.
        if str(data.get("website") or "").strip():
            return Response({"ok": True}, status=status.HTTP_201_CREATED)

        name = str(data.get("name") or "").strip()
        email = str(data.get("email") or "").strip()
        order_id = str(data.get("order_id") or "").strip()
        message = str(data.get("message") or "").strip()
        # "merchant" comes from the merchant site's landing-page form; anything else is a customer message.
        source = (
            ContactMessage.SOURCE_MERCHANT
            if str(data.get("source") or "").strip() == ContactMessage.SOURCE_MERCHANT
            else ContactMessage.SOURCE_CUSTOMER
        )
        topic = str(data.get("topic") or "").strip()[:MAX_TOPIC_LENGTH]

        if not name or not email or not message:
            return Response({"error": "Name, email and message are required."}, status=status.HTTP_400_BAD_REQUEST)
        if len(name) > MAX_NAME_LENGTH:
            return Response({"error": f"Name must be {MAX_NAME_LENGTH} characters or fewer."}, status=status.HTTP_400_BAD_REQUEST)
        if len(order_id) > MAX_ORDER_ID_LENGTH:
            return Response({"error": "Order ID is too long."}, status=status.HTTP_400_BAD_REQUEST)
        if len(message) > MAX_MESSAGE_LENGTH:
            return Response({"error": f"Message must be {MAX_MESSAGE_LENGTH} characters or fewer."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_email(email)
        except ValidationError:
            return Response({"error": "Enter a valid email address."}, status=status.HTTP_400_BAD_REQUEST)

        ip_address = _client_ip(request)
        if ip_address:
            recent = ContactMessage.objects.filter(
                ip_address=ip_address,
                created_at__gte=timezone.now() - timedelta(hours=1),
            ).count()
            if recent >= MAX_MESSAGES_PER_IP_PER_HOUR:
                return Response(
                    {"error": "Too many messages sent. Please try again in a little while."},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

        ContactMessage.objects.create(
            source=source,
            topic=topic,
            name=name,
            email=email,
            order_id=order_id,
            message=message,
            ip_address=ip_address,
            user_agent=str(request.META.get("HTTP_USER_AGENT", ""))[:300],
        )
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


class AdminContactMessageListView(AdminAPIView):
    """GET /adminpanel/contact-messages/ - paginated inbox with status counts."""

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
        q = (request.GET.get("q") or "").strip()
        # The Messages page lists customer messages and the Merchant queries page lists merchant ones.
        # Without a source the customer inbox is returned, as before.
        source_filter = (request.GET.get("source") or "").strip()
        if source_filter not in VALID_SOURCES:
            source_filter = ContactMessage.SOURCE_CUSTOMER

        qs = ContactMessage.objects.select_related("handled_by").filter(source=source_filter)
        if status_filter in VALID_STATUSES:
            qs = qs.filter(status=status_filter)
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(email__icontains=q)
                | Q(order_id__icontains=q)
                | Q(message__icontains=q)
                | Q(topic__icontains=q)
            )

        total = qs.count()
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit

        counts = {value: 0 for value in VALID_STATUSES}
        for row in ContactMessage.objects.filter(source=source_filter).values("status").annotate(total=Count("id")):
            counts[row["status"]] = row["total"]

        return Response(
            {
                "results": [_serialize(message) for message in qs[start:start + limit]],
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "counts": counts,
            }
        )


class AdminContactMessageDetailView(AdminAPIView):
    """PATCH /adminpanel/contact-messages/<id>/ - change status or internal note."""

    required_permission = "contact_messages.manage"

    def patch(self, request, message_id):
        try:
            message = ContactMessage.objects.select_related("handled_by").get(id=message_id)
        except ContactMessage.DoesNotExist:
            return Response({"error": "Message not found."}, status=status.HTTP_404_NOT_FOUND)

        changed = False
        if "status" in request.data:
            new_status = str(request.data.get("status") or "").strip()
            if new_status not in VALID_STATUSES:
                return Response({"error": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST)
            message.status = new_status
            changed = True
        if "internal_note" in request.data:
            message.internal_note = str(request.data.get("internal_note") or "").strip()[:2000]
            changed = True

        if changed:
            message.handled_by = request.admin_user
            message.handled_at = timezone.now()
            message.save()
        return Response(_serialize(message))


class AdminContactMessageReplyView(AdminAPIView):
    """POST /adminpanel/contact-messages/<id>/reply/ - email a reply to the sender.

    The email is sent by the server from DEFAULT_FROM_EMAIL (set it, plus the
    EMAIL_* settings, to your support mailbox), not from the admin's own
    mail account. Every reply is stored so the inbox shows what was sent.
    """

    required_permission = "contact_messages.manage"

    def post(self, request, message_id):
        try:
            message = ContactMessage.objects.get(id=message_id)
        except ContactMessage.DoesNotExist:
            return Response({"error": "Message not found."}, status=status.HTTP_404_NOT_FOUND)

        subject = str(request.data.get("subject") or "").strip()
        body = str(request.data.get("body") or "").strip()
        if not subject or not body:
            return Response({"error": "Subject and reply text are required."}, status=status.HTTP_400_BAD_REQUEST)
        if len(subject) > 200 or "\n" in subject or "\r" in subject:
            return Response({"error": "Subject must be a single line of 200 characters or fewer."}, status=status.HTTP_400_BAD_REQUEST)
        if len(body) > MAX_MESSAGE_LENGTH * 2:
            return Response({"error": "Reply is too long."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[message.email],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Failed to send contact reply for message_id=%s", message.id)
            return Response(
                {"error": "The email could not be sent. Check the email settings and try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        ContactReply.objects.create(message=message, subject=subject, body=body, sent_by=request.admin_user)
        if message.status == ContactMessage.STATUS_NEW:
            message.status = ContactMessage.STATUS_IN_PROGRESS
        message.handled_by = request.admin_user
        message.handled_at = timezone.now()
        message.save()
        return Response(_serialize(message))
