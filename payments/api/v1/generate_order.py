# Razorpay checkout flow: create an order, then verify the payment signature.
#
# NOTE: this file defines CreatePayment and VerifyPayment twice — a DRF
# APIView pair up top (GenerateOrder/VerifyPayment), and a plain Django View
# pair further down (also named CreatePayment/VerifyPayment). Because both
# live at module scope, the later definitions silently replace the earlier
# ones; payments/urls.py imports CreatePayment/VerifyPayment from here and
# gets the *second* (View-based) versions. GenerateOrder itself is unused/
# unrouted. Left as-is since untangling it is a behavior change, not a
# comment — flagging it here so it's not mistaken for dead code.
from django.utils import timezone
import os

import requests
import razorpay
import logging
from zoneinfo import ZoneInfo
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import OuterRef, Subquery
from rest_framework.views import APIView, Response, status

from payments.auth import get_merchant_id_from_token
from payments.models.orderinfo import OrderInfo
from payments.models.customerinfo import CustomerInfo
from payments.models.payment_lifecycle import PaymentNotification
from payments.api.v1.phonepe import PhonePeInitiateView
from payments.config import RAZORPAY_CREATE_ORDER_URL, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET
from tracking.models.tracking_snapshot import TrackingApiCallLog, TrackingSnapshot
from tracking.api.v1.delhivery_public_tracking import refresh_delhivery_public_snapshot
from tracking.api.v1.trackparcel import should_refresh_snapshot


client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
logger = logging.getLogger(__name__)

REMINDER_SLOT_LABELS = {
    10: "am",
    16: "pm",
}


def _get_auth_token_from_request(request, payload=None):
    """Pull the merchant session token from the Authorization header, the
    parsed JSON payload, request.data, or a query param — whichever the
    caller used. Lets both DRF (request.data) and plain-Django (raw payload)
    views in this file share one lookup."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()

    if payload and payload.get("auth_token"):
        return payload.get("auth_token")

    data = getattr(request, "data", None)
    if data and data.get("auth_token"):
        return data.get("auth_token")

    return request.GET.get("auth_token")

class GenerateOrder(APIView):
    """Not wired up in urls.py — superseded by the CreatePayment View below."""

    def post(self, request):
        self.auth_token = _get_auth_token_from_request(request)
        token_merchant_id = get_merchant_id_from_token(self.auth_token)
        if token_merchant_id is None:
            return Response({"error": "Invalid or expired merchant token"}, status=status.HTTP_401_UNAUTHORIZED)

        self.merchant_id = token_merchant_id
        self.merchant_order_id = request.data.get('merchant_order_id')
        self.customer_name = request.data.get('customer_name')
        self.customer_email = request.data.get('customer_email')
        self.customer_phone = request.data.get('customer_phone')
        self.amount = int(request.data.get('amount'))

        self.generate_order()
        return self.create_pa_order()

    def generate_order(self):
        try:
            customer_info = CustomerInfo.objects.get(customer_phone=self.customer_phone)
        except CustomerInfo.DoesNotExist:
            customer_info = CustomerInfo(
                customer_name=self.customer_name,
                customer_email=self.customer_email,
                customer_phone=self.customer_phone
            )
            customer_info.save()

        self.order_info = OrderInfo(
            merchant_id=self.merchant_id,
            merchant_order_id=self.merchant_order_id,
            order_amount=self.amount,
            order_currency="INR",
            customer_info_id=customer_info.id
        )

    def create_pa_order(self):
        PhonePeInitiateView
        try:
            payload = {
                "amount": self.amount * 100,  # Amount in paise
                "currency": "INR",
                "payment_capture": "1"
            }
            response = client.order.create(payload)
            self.order_info.order_status = response['status']
            self.order_info.pa_order_id = response['id']
            self.order_info.save()
            return Response(response)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class VerifyPayment(APIView):
    """Placeholder — shadowed by the View-based VerifyPayment defined later
    in this file (see the module-level note above); not actually reachable."""

    def post(self, request):
        print("Request data:", request.data)
        return Response(request.data)

# payments/api/v1/payment_views.py
import os
import json
import hmac
import hashlib
from decimal import Decimal

from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse
from django.utils.decorators import method_decorator
from django.views import View

# Optional: import real razorpay client if installed
try:
    import razorpay
    RZP_AVAILABLE = True
except Exception:
    RZP_AVAILABLE = False

# This is the CreatePayment actually routed by payments/urls.py (see note
# at the top of the file about the duplicate class names above).
@method_decorator(csrf_exempt, name='dispatch')  # for demo: allow POST from browser without CSRF token
class CreatePayment(View):
    def post(self, request, *args, **kwargs):
        """
        Expected JSON payload:
        {
          "amount": "100.50",            # decimal string in INR rupees
          "currency": "INR",
          "receipt": "receipt_12345",
          "customer": {"name":"Alice","email":"a@x.com","contact":"9999999999"},
          "metadata": { ... }            # optional
        }
        Response:
        {
          "key": "<RAZORPAY_KEY_ID>",
          "order_id": "<razorpay_order_id>",
          "amount": 10050,   # amount in paise (integer)
          "currency": "INR",
          "callback_url": "/payments/verify_payment/"  # optional
        }
        """
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except Exception:
            return HttpResponseBadRequest("invalid json")

        self.auth_token = _get_auth_token_from_request(request, payload)
        token_merchant_id = get_merchant_id_from_token(self.auth_token)
        if token_merchant_id is None:
            return JsonResponse({"error": "Invalid or expired merchant token"}, status=401)
        self.merchant_id = token_merchant_id

        # Validate & compute amount in paise
        self.amount_str = str(payload.get("amount", "0")).strip()
        try:
            # Convert rupees string to paise integer
            amount_paise = int((Decimal(self.amount_str) * 100).quantize(Decimal('1')))
        except Exception:
            return HttpResponseBadRequest("invalid amount")

        currency = payload.get("currency", "INR")
        receipt = payload.get("receipt", f"rcpt_{os.urandom(4).hex()}")
        notes = payload.get("metadata", {})
        customer = payload.get("customer", {})

        self.customer_name = customer.get("name", "")
        self.customer_email = customer.get("email", "")
        self.customer_phone = customer.get("contact", "")
        self.mock_order_id = f"mock_rzp_{os.urandom(4).hex()}"

        # Use real Razorpay if available & keys present
        if RZP_AVAILABLE and RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
            client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
            order_data = {
                "amount": amount_paise,
                "currency": currency,
                "receipt": receipt,
                "notes": notes
            }
            try:
                rzp_order = client.order.create(data=order_data)
                # rzp_order looks like {"id":"order_XXX", "status":"created", ...}
                self.order_id = rzp_order["id"]
                self.generate_order()  # store in our DB
                return JsonResponse({
                    "key": RAZORPAY_KEY_ID,
                    "order_id": rzp_order["id"],
                    "amount": amount_paise,
                    "currency": currency,
                    "callback_url": "/payments/verify_payment/",
                    "customer": customer
                })
            except Exception as e:
                return JsonResponse({"error": "failed to create razorpay order", "detail": str(e)}, status=500)

        # Fallback mock order (useful for local dev without SDK)
        # store mapping somewhere (DB) in real app; here we return for demo
        return JsonResponse({
            "key": RAZORPAY_KEY_ID,
            "order_id": self.mock_order_id,
            "amount": amount_paise,
            "currency": currency,
            "callback_url": "/payments/verify_payment/",
            "customer": customer,
            "note": "mock_razorpay_order"
        })

    def generate_order(self):
        try:
            customer_info = CustomerInfo.objects.get(customer_phone=self.customer_phone)
        except CustomerInfo.DoesNotExist:
            customer_info = CustomerInfo(
                customer_name=self.customer_name,
                customer_email=self.customer_email,
                customer_phone=self.customer_phone
            )
            customer_info.save()

        self.order_info = OrderInfo(
            merchant_id=self.merchant_id,
            merchant_order_id=self.mock_order_id,
            order_amount=self.amount_str,
            order_currency="INR",
            customer_info_id=customer_info.id,
            pa_order_id=self.order_id,
        )
        self.order_info.save()



# This is the VerifyPayment actually routed by payments/urls.py.
@method_decorator(csrf_exempt, name='dispatch')
class VerifyPayment(View):
    def post(self, request, *args, **kwargs):
        """
        Expects JSON:
        {
          "razorpay_order_id": "...",
          "razorpay_payment_id": "...",
          "razorpay_signature": "..."
        }
        Verifies signature using RAZORPAY_KEY_SECRET (HMAC SHA256 of order_id|payment_id)
        On success: mark internal order/payment as paid, return success JSON.

        *** SECURITY BUG — READ BEFORE RELYING ON THIS ENDPOINT ***
        The docstring above describes the INTENDED behavior, but the code
        below does not actually match it: `order_info.order_status = "paid"`
        and `.save()` happen BEFORE the signature is checked (see the
        `hmac.compare_digest` call much further down). That means:
          - Any POST with a valid-looking existing razorpay_order_id gets the
            matching order marked "paid" in the database immediately —
            regardless of whether payment_id/signature are present, correct,
            or even supplied at all.
          - The signature check that follows only affects the HTTP RESPONSE
            ("signature mismatch" vs. success) — it does NOT undo the
            order_status="paid" write that already happened.
          - This means a caller who knows (or brute-forces/guesses) a
            razorpay_order_id can mark that order as paid without ever
            proving they made a real payment, simply by POSTing here with a
            wrong/missing signature. That's the exact scenario payment
            signature verification exists to prevent.
        This is a payments-integrity bug, not just a style issue. The fix
        (not applied here, since it's a behavior change beyond "add
        comments") would be moving the `order_info.order_status = "paid"` /
        `.save()` lines to AFTER the `hmac.compare_digest(...)` check
        succeeds, so a failed/forged verification leaves the order
        untouched. Flagging prominently because unlike the other issues
        noted in this codebase, this one directly affects whether money
        changes hands correctly.
        """
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except Exception:
            return HttpResponseBadRequest("invalid json")
        print(payload)
        order_id = payload.get("razorpay_order_id")
        payment_id = payload.get("razorpay_payment_id")
        signature = payload.get("razorpay_signature")

        order_info = OrderInfo.objects.filter(pa_order_id=order_id).first()
        if not order_info:
            return HttpResponseBadRequest("invalid razorpay_order_id")
        # BUG (see docstring above): this write happens unconditionally,
        # before the signature below is ever checked.
        order_info.pa_payment_id = payment_id
        order_info.order_status = "paid"
        order_info.save()
        if not (order_id and payment_id and signature):
            return HttpResponseBadRequest("missing parameters")

        # This IS the correct, secure way to check a Razorpay signature
        # (HMAC-SHA256 of "order_id|payment_id" keyed on the secret, compared
        # with the constant-time hmac.compare_digest) — the problem is only
        # that it runs too late to gate the DB write above.
        msg = f"{order_id}|{payment_id}".encode()
        expected_sig = hmac.new(RAZORPAY_KEY_SECRET.encode(), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_sig, signature):
            return JsonResponse({"ok": False, "reason": "signature mismatch"}, status=400)
        return JsonResponse({"ok": True, "message": "payment verified and processed", "razorpay_order_id": order_id, "razorpay_payment_id": payment_id})


@method_decorator(csrf_exempt, name='dispatch')
class CreateManualOrder(View):
    """Create a paid dashboard order without starting a payment gateway flow."""

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except Exception:
            return HttpResponseBadRequest("invalid json")

        auth_token = _get_auth_token_from_request(request, payload)
        merchant_id = get_merchant_id_from_token(auth_token)
        if merchant_id is None:
            return JsonResponse({"error": "Invalid or expired merchant token"}, status=401)

        merchant_order_id = str(payload.get("merchantOrderId") or "").strip()
        customer_name = str(payload.get("customerName") or "").strip()
        customer_phone = str(payload.get("customerPhone") or payload.get("mobileNumber") or "").strip()
        customer_email = str(payload.get("customerEmail") or "").strip()
        customer_address = str(payload.get("customerAddress") or "Not provided").strip()
        payment_provider = str(payload.get("paymentProvider") or "Manual").strip()
        amount = str(payload.get("amount") or "").strip()

        if not merchant_order_id:
            return JsonResponse({"error": "merchantOrderId is required"}, status=400)
        if not customer_name:
            return JsonResponse({"error": "customerName is required"}, status=400)
        if not customer_phone:
            return JsonResponse({"error": "customerPhone is required"}, status=400)

        try:
            amount_value = Decimal(amount)
        except Exception:
            return JsonResponse({"error": "amount is invalid"}, status=400)
        if amount_value <= 0:
            return JsonResponse({"error": "amount must be greater than 0"}, status=400)

        if OrderInfo.objects.filter(merchant_id=merchant_id, merchant_order_id=merchant_order_id).exists():
            return JsonResponse({"error": "Order ID already exists."}, status=409)

        customer, _ = CustomerInfo.objects.update_or_create(
            customer_phone=customer_phone,
            defaults={
                "customer_name": customer_name,
                "customer_email": customer_email,
                "customer_address": customer_address,
            },
        )

        payment_id = str(payload.get("paymentId") or f"manual_{merchant_order_id[-16:].replace('-', '')}")
        order = OrderInfo.objects.create(
            merchant_id=merchant_id,
            merchant_order_id=merchant_order_id,
            pa_order_id=merchant_order_id,
            pa_payment_id=payment_id,
            order_amount=amount_value,
            order_currency="INR",
            order_status="paid",
            customer_info=customer,
            shipment_id=None,
            payment_provider=payment_provider,
            phonepe_order_id="",
            phonepe_payment_id="",
        )

        return JsonResponse({
            "ok": True,
            "order": {
                "id": order.id,
                "pa_order_id": order.pa_order_id,
                "order_status": order.order_status,
                "order_amount": str(order.order_amount),
                "customer_name": customer.customer_name,
                "awb": "",
                "shipment_status": "",
            },
        }, status=201)
    
class ShipmentListView(View):
    """Merchant-scoped order/shipment list — the merchant-facing counterpart
    to adminpanel.AdminOrderListView, but filtered to the logged-in merchant
    only rather than showing every merchant's orders."""

    def get(self, request, *args, **kwargs):
        """
        Example endpoint to list shipments.
        Accepts optional query params: limit (int), page (int), q (search string).
        Returns: { results: [...], page: 1, total_pages: 1 }
        """
        auth_token = _get_auth_token_from_request(request)
        token_merchant_id = get_merchant_id_from_token(auth_token)
        if token_merchant_id is None:
            return JsonResponse({"error": "Invalid or expired merchant token"}, status=401)

        # read query params
        try:
            limit = int(request.GET.get('limit', 25))
        except ValueError:
            limit = 25
        try:
            page = int(request.GET.get('page', 1))
        except ValueError:
            page = 1
        q = (request.GET.get('q') or '').strip().lower()

        all_shipments = OrderInfo.objects.filter(merchant_id=token_merchant_id).values('merchant_order_id', 'pa_order_id', 'order_date', 'order_status', 'payment_state', 'order_amount', 'order_currency', 'customer_info__customer_name', 'customer_info__customer_email', 'customer_info__customer_phone', 'shipment_id__awb', 'shipment_id__courier', 'shipment_id__status', 'pa_payment_id').order_by('-order_date')
        # simple filtering by q (match awb, courier, or order id)
        if q:
            filtered = [s for s in all_shipments if q in (s.get('shipment_id__awb','') + s.get('shipment_id__courier','') + s.get('pa_order_id','')).lower()]
        else:
            filtered = all_shipments

        # pagination (simple)
        total = len(filtered)
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        end = start + limit
        results = filtered[start:end]

        return JsonResponse({
            "results": results,
            "page": page,
            "total_pages": total_pages,
            "total": total
        })


class SavedShipmentTrackingView(View):
    """Return the latest server-saved tracking snapshot without refreshing a provider."""

    def get(self, request, awb, *args, **kwargs):
        auth_token = _get_auth_token_from_request(request)
        merchant_id = get_merchant_id_from_token(auth_token)
        if merchant_id is None:
            return JsonResponse({"error": "Invalid or expired merchant token"}, status=401)

        order = (
            OrderInfo.objects.filter(merchant_id=merchant_id, shipment_id__awb=awb)
            .select_related("shipment_id")
            .first()
        )
        if not order or not order.shipment_id:
            return JsonResponse({"error": "Shipment not found for this merchant"}, status=404)

        snapshot = TrackingSnapshot.objects.filter(shipment=order.shipment_id).first()
        if not snapshot or not snapshot.last_checked_at:
            return JsonResponse({"error": "No saved tracking result is available yet", "awb": awb}, status=404)

        shipment = order.shipment_id
        return JsonResponse({
            "awb": shipment.awb,
            "secureupi_order_id": shipment.pa_order_id,
            "courier": shipment.courier,
            "status": snapshot.current_status or shipment.status,
            "normalized_status": snapshot.normalized_status,
            "history": shipment.history if hasattr(shipment, "history") else [],
            "provider": snapshot.source,
            "refreshed": False,
            "refresh_reason": "served from saved tracking snapshot",
            "last_checked_at": snapshot.last_checked_at.isoformat(),
            "next_check_after": snapshot.next_check_after.isoformat() if snapshot.next_check_after else None,
            "expected_delivery_date": snapshot.expected_delivery_date.isoformat()
            if snapshot.expected_delivery_date
            else None,
            "delivered_at": snapshot.delivered_at.isoformat() if snapshot.delivered_at else None,
            "tracking": snapshot.raw_response,
            "created_at": shipment.created_at.isoformat() if hasattr(shipment, "created_at") else None,
        })


class MerchantNotificationListView(View):
    """Reminder notifications for merchant orders missing shipment labels."""

    def _current_reminder_slot(self, now):
        reminder_tz = ZoneInfo(getattr(settings, "SHIPMENT_LABEL_REMINDER_TIMEZONE", "Asia/Kolkata"))
        local_now = timezone.localtime(now, reminder_tz)
        due_hours = sorted(getattr(settings, "SHIPMENT_LABEL_REMINDER_HOURS", (10, 16)))
        due_hour = None
        for hour in due_hours:
            if local_now.hour >= hour:
                due_hour = hour

        if due_hour is None:
            return None

        label = REMINDER_SLOT_LABELS.get(due_hour, f"{due_hour:02d}00")
        return f"{local_now.date().isoformat()}-{label}"

    def _record_due_reminder(self, order, now):
        slot = self._current_reminder_slot(now)
        if slot is None or order.shipment_label_last_reminder_slot == slot:
            return False

        if order.shipment_label_reminder_started_at is None:
            order.shipment_label_reminder_started_at = now

        order.shipment_label_last_reminded_at = now
        order.shipment_label_last_reminder_slot = slot
        order.shipment_label_reminder_count = (order.shipment_label_reminder_count or 0) + 1
        order.save(update_fields=[
            "shipment_label_reminder_started_at",
            "shipment_label_last_reminded_at",
            "shipment_label_last_reminder_slot",
            "shipment_label_reminder_count",
        ])
        return True

    def _collect_escalation_orders(self, orders, now):
        escalation_days = getattr(settings, "SHIPMENT_LABEL_ESCALATION_DAYS", 4)
        escalation_cutoff = now - timezone.timedelta(days=escalation_days)
        return [
            order
            for order in orders
            if order.shipment_label_escalation_email_sent_at is None
            and order.shipment_label_reminder_started_at is not None
            and order.shipment_label_reminder_started_at <= escalation_cutoff
        ]

    def _send_escalation_email(self, merchant, orders, now):
        if not orders or not merchant.merchant_email:
            return

        lines = [
            "The following orders still do not have shipment labels after repeated reminders:",
            "",
        ]
        for order in orders:
            customer = order.customer_info
            lines.append(f"Order ID: {order.merchant_order_id}")
            lines.append(f"Payment order ID: {order.pa_order_id or 'N/A'}")
            lines.append(f"Amount: {order.order_currency} {order.order_amount}")
            lines.append(f"Customer: {customer.customer_name if customer else 'N/A'}")
            lines.append(f"Customer phone: {customer.customer_phone if customer else 'N/A'}")
            lines.append("")

        lines.append("Please upload the shipment label for these orders from your merchant dashboard.")

        try:
            send_mail(
                subject="Action required: shipment labels pending",
                message="\n".join(lines),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[merchant.merchant_email],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Failed to send shipment label escalation email to merchant_id=%s", merchant.id)
            return

        for order in orders:
            order.shipment_label_escalation_email_sent_at = now
            order.save(update_fields=["shipment_label_escalation_email_sent_at"])

    def _refresh_due_tracking(self, merchant_id, now):
        """Refresh due courier snapshots before building live notifications.

        The dashboard already polls this endpoint, so this keeps delivery
        status and tracking notifications current without forcing a full page
        refresh. The snapshot schedule still controls provider call frequency.
        """
        shipment_ids = OrderInfo.objects.filter(
            merchant_id=merchant_id,
            shipment_id__isnull=False,
        ).values_list("shipment_id_id", flat=True)
        snapshots = TrackingSnapshot.objects.select_related("shipment").filter(
            shipment_id__in=shipment_ids,
        ).order_by("next_check_after", "last_checked_at", "id")[:100]

        for snapshot in snapshots:
            due, _ = should_refresh_snapshot(snapshot, now=now)
            if not due:
                continue
            try:
                refresh_delhivery_public_snapshot(snapshot.shipment, now=now)
            except Exception as exc:
                logger.warning("Live tracking refresh failed for %s: %s", snapshot.awb, exc)

    def get(self, request, *args, **kwargs):
        auth_token = _get_auth_token_from_request(request)
        merchant_id = get_merchant_id_from_token(auth_token)
        if merchant_id is None:
            return JsonResponse({"error": "Invalid or expired merchant token"}, status=401)

        now = timezone.localtime()
        # Tracking snapshots are refreshed by the admin/background process.
        # Merchant polling must only read saved data and never trigger a provider call.
        orders = (
            OrderInfo.objects.filter(merchant_id=merchant_id, shipment_id__isnull=True)
            .select_related("customer_info", "merchant")
            .order_by("-order_date")
        )
        orders = list(orders)

        for order in orders:
            self._record_due_reminder(order, now)

        merchant = orders[0].merchant if orders else None
        if merchant:
            self._send_escalation_email(merchant, self._collect_escalation_orders(orders, now), now)

        # Notifications expire after NOTIFICATION_RETENTION_DAYS so the list stays small.
        retention_days = getattr(settings, "NOTIFICATION_RETENTION_DAYS", 7)
        retention_cutoff = now - timezone.timedelta(days=retention_days)
        PaymentNotification.objects.filter(
            merchant_id=merchant_id,
            created_at__lt=retention_cutoff,
        ).delete()

        notifications = [
            {
                "id": f"missing-label-{order.id}",
                "type": "missing_shipment_label",
                "title": "Shipment label upload pending",
                "message": f"Order ID {order.merchant_order_id} still needs a shipment label upload.",
                "order_id": order.merchant_order_id,
                "customer_name": order.customer_info.customer_name if order.customer_info else "",
                "created_at": order.shipment_label_last_reminded_at.isoformat() if order.shipment_label_last_reminded_at else "",
                "reminder_count": order.shipment_label_reminder_count,
            }
            for order in orders
            if order.shipment_label_last_reminded_at is not None
            and order.shipment_label_last_reminded_at >= retention_cutoff
            # Seeded demo orders never get labels, so don't nag about them.
            and not str(order.merchant_order_id).startswith("mock_hold_pa_order")
        ]

        tracking_notifications = self._tracking_status_notifications(merchant_id, retention_cutoff)
        notifications.extend(tracking_notifications)

        payment_notifications = PaymentNotification.objects.filter(
            merchant_id=merchant_id,
            channel=PaymentNotification.CHANNEL_DASHBOARD,
        ).select_related("order", "order__customer_info")[:50]
        notifications.extend(
            {
                "id": f"payment-{item.id}",
                "type": item.event_type,
                "title": item.title,
                "message": item.message,
                "order_id": item.order.merchant_order_id if item.order else "",
                "customer_name": item.order.customer_info.customer_name if item.order and item.order.customer_info else "",
                "created_at": item.created_at.isoformat(),
                "payment_state": item.order.payment_state if item.order else "",
            }
            for item in payment_notifications
        )

        notifications.sort(key=lambda item: item.get("created_at") or "", reverse=True)

        return JsonResponse({
            "results": notifications,
            "total": len(notifications),
        })

    def _tracking_status_notifications(self, merchant_id, retention_cutoff=None):
        latest_log_id = (
            TrackingApiCallLog.objects.filter(shipment_id=OuterRef("shipment_id_id"))
            .order_by("-called_at")
            .values("id")[:1]
        )
        orders = (
            OrderInfo.objects.filter(
                merchant_id=merchant_id,
                shipment_id__isnull=False,
                shipment_id_id__in=TrackingSnapshot.objects.filter(
                    last_checked_at__isnull=False
                ).values("shipment_id"),
            )
            .select_related("customer_info", "shipment_id")
            .annotate(latest_tracking_log_id=Subquery(latest_log_id))
            .order_by("-shipment_id_id")[:25]
        )

        notifications = []
        for order in orders:
            shipment = order.shipment_id
            snapshot = TrackingSnapshot.objects.filter(shipment=shipment).first()
            if not snapshot or not snapshot.last_checked_at:
                continue
            last_checked = snapshot.last_checked_at
            if retention_cutoff is not None and last_checked < retention_cutoff:
                continue
            status_text = snapshot.current_status or shipment.status or "Unknown"
            next_check = snapshot.next_check_after.isoformat() if snapshot.next_check_after else ""
            notifications.append(
                {
                    "id": f"tracking-status-{shipment.awb}-{order.latest_tracking_log_id or int(last_checked.timestamp())}",
                    "type": "tracking_status",
                    "title": "Shipment delivered" if snapshot.normalized_status == "DELIVERED" else f"Tracking update: {shipment.awb}",
                    "message": (
                        f"{shipment.courier or 'Courier'} status is {status_text}. "
                        f"Last checked {timezone.localtime(last_checked).strftime('%d %b %Y, %I:%M %p')}. "
                        f"Next check: {next_check or 'not scheduled'}."
                    ),
                    "order_id": order.merchant_order_id,
                    "customer_name": order.customer_info.customer_name if order.customer_info else "",
                    "created_at": last_checked.isoformat(),
                    "awb": shipment.awb,
                    "courier": shipment.courier or "",
                    "status": status_text,
                    "normalized_status": snapshot.normalized_status,
                    "next_check_after": next_check,
                    "expected_delivery_date": snapshot.expected_delivery_date.isoformat()
                    if snapshot.expected_delivery_date
                    else "",
                }
            )
        return notifications
