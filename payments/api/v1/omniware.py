import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from payments.adapters.omniware import OmniwareError, create_payment_request, generate_hash, verify_response_hash
from payments.api.v1.checkout_sessions import _is_pending_session_valid
from payments.models import (
    CheckoutSession,
    OrderInfo,
    PaymentAttempt,
    PaymentEvent,
    PaymentLedgerEntry,
)
from payments.services.payment_lifecycle import transition_payment_state


SUCCESS_CODES = {"0", "0000"}
PENDING_CODES = {"1006", "1088"}
CANCELLED_CODES = {"1043"}


def _json_payload(request):
    try:
        return json.loads(request.body.decode("utf-8")) if request.body else {}
    except (UnicodeDecodeError, ValueError):
        return {}


def _find_order(session):
    order_id = str(session.order_snapshot.get("order_id") or "").strip()
    return OrderInfo.objects.filter(merchant=session.merchant, merchant_order_id=order_id).first()


def _provider_order_id(order):
    prefix = "ESP"
    return f"{prefix}{order.pk}{uuid.uuid4().hex[:16]}"[:30]


def _amount(value):
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def _frontend_return_url(request, session):
    configured = getattr(settings, "SECUREPAY_CHECKOUT_BASE_URL", "http://localhost:5173/checkout")
    root = configured.split("?", 1)[0].rstrip("/")
    return f"{root}/payment-result?session_id={session.session_id}"


def _payment_payload(request, attempt):
    session = attempt.checkout_session
    customer = session.customer_snapshot
    callback_url = request.build_absolute_uri(reverse("omniware-payment-callback"))
    return {
        "order_id": attempt.provider_order_id,
        "amount": _amount(attempt.order.order_amount),
        "currency": attempt.order.order_currency or "INR",
        "description": f"EscroSafe order {attempt.order.merchant_order_id}"[:255],
        "name": str(customer.get("name") or attempt.order.customer_info.customer_name),
        "email": str(customer.get("email") or attempt.order.customer_info.customer_email),
        "phone": str(customer.get("phone") or attempt.order.customer_info.customer_phone),
        "address": str(customer.get("address") or attempt.order.customer_info.customer_address),
        "city": str(customer.get("city") or "NA"),
        "state": str(customer.get("state") or "NA"),
        "country": str(customer.get("country") or "IND"),
        "zip_code": str(customer.get("pincode") or customer.get("zip_code") or "000000"),
        "return_url": callback_url,
        "mode": getattr(settings, "OMNIWARE_MODE", "TEST"),
    }


def _checkout_response(attempt, reused=False):
    response = attempt.response_payload if isinstance(attempt.response_payload, dict) else {}
    return {
        "ok": True,
        "attempt_id": attempt.id,
        "payment_mode": "UPI_QR",
        "qr_image_url": str(
            response.get("qr_image_url")
            or response.get("qrImageUrl")
            or response.get("qr_code")
            or response.get("qrCode")
            or ""
        ),
        "qr_payload": str(response.get("qr_payload") or response.get("upi_intent") or response.get("upiIntent") or ""),
        "mock": attempt.provider == "mock_omniware",
        "mock_control_url": attempt.checkout_url if attempt.provider == "mock_omniware" else "",
        "expires_at": attempt.expires_at.isoformat() if attempt.expires_at else None,
        "reused": reused,
    }


def _status_from_callback(payload):
    code = str(payload.get("response_code") or payload.get("responseCode") or "")
    status_text = str(payload.get("status") or "").upper()
    if code in SUCCESS_CODES or status_text in {"SUCCESS", "SUCCESSFUL", "PAID", "CAPTURED"}:
        return PaymentAttempt.STATUS_SUCCESS
    if code in PENDING_CODES or status_text in {"PENDING", "PROCESSING", "INITIATED"}:
        return PaymentAttempt.STATUS_PENDING
    if code in CANCELLED_CODES or status_text in {"CANCELLED", "CANCELED"}:
        return PaymentAttempt.STATUS_CANCELLED
    return PaymentAttempt.STATUS_FAILED


def process_omniware_callback(payload):
    salt = getattr(settings, "OMNIWARE_SALT", "")
    if not salt or not verify_response_hash(payload, salt):
        raise PermissionError("Invalid Omniware response hash")

    provider_order_id = str(payload.get("order_id") or payload.get("orderId") or "").strip()
    attempt = PaymentAttempt.objects.select_related("order", "checkout_session").filter(
        provider_order_id=provider_order_id
    ).first()
    if not attempt:
        raise LookupError("Unknown payment order")

    try:
        callback_amount = Decimal(str(payload.get("amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Invalid callback amount")
    if callback_amount != attempt.order.order_amount.quantize(Decimal("0.01")):
        raise ValueError("Callback amount does not match the order")
    currency = str(payload.get("currency") or "INR").upper()
    if currency != (attempt.order.order_currency or "INR").upper():
        raise ValueError("Callback currency does not match the order")

    canonical = "&".join(f"{key}={payload[key]}" for key in sorted(payload) if key != "hash")
    event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    status = _status_from_callback(payload)

    try:
        with transaction.atomic():
            event = PaymentEvent.objects.create(
                provider="omniware",
                event_id=str(payload.get("transaction_id") or payload.get("transactionId") or ""),
                event_hash=event_hash,
                event_type="payment.callback",
                order=attempt.order,
                payload=payload,
                signature=str(payload.get("hash") or ""),
            )
            attempt.status = status
            attempt.provider_transaction_id = event.event_id
            attempt.response_code = str(payload.get("response_code") or "")
            attempt.response_message = str(payload.get("response_message") or payload.get("message") or "")
            attempt.payment_mode = str(payload.get("payment_mode") or "")
            attempt.payment_channel = str(payload.get("payment_channel") or "")
            attempt.response_payload = payload
            attempt.save()

            order = attempt.order
            if status == PaymentAttempt.STATUS_SUCCESS:
                if order.payment_state in {"", "CREATED", "PAYMENT_PENDING", "PAYMENT_FAILED"}:
                    if order.payment_state == "PAYMENT_FAILED":
                        transition_payment_state(order, "PAYMENT_PENDING", "omniware_callback", {"attempt_id": attempt.id}, event)
                    transition_payment_state(order, "PAYMENT_CAPTURED", "omniware_callback", {"attempt_id": attempt.id}, event)
                PaymentLedgerEntry.objects.get_or_create(
                    order=order,
                    entry_type=PaymentLedgerEntry.ENTRY_CAPTURED,
                    reference=f"omniware:{attempt.provider_order_id}",
                    defaults={"amount": order.order_amount, "currency": order.order_currency, "metadata": {"attempt_id": attempt.id}},
                )
                order.order_status = "paid"
                order.pa_order_id = attempt.provider_order_id
                order.pa_payment_id = attempt.provider_transaction_id
            elif status == PaymentAttempt.STATUS_PENDING:
                if order.payment_state in {"", "CREATED"}:
                    transition_payment_state(order, "PAYMENT_PENDING", "omniware_callback", {"attempt_id": attempt.id}, event)
                order.order_status = "pending"
            else:
                if order.payment_state in {"", "CREATED", "PAYMENT_PENDING"}:
                    transition_payment_state(order, "PAYMENT_FAILED", "omniware_callback", {"attempt_id": attempt.id}, event)
                order.order_status = "payment_cancelled" if status == PaymentAttempt.STATUS_CANCELLED else "payment_failed"
            order.payment_provider = "omniware"
            order.save(update_fields=["order_status", "pa_order_id", "pa_payment_id", "payment_provider"])
            event.status = PaymentEvent.STATUS_PROCESSED
            event.processed_at = timezone.now()
            event.save(update_fields=["status", "processed_at"])
    except IntegrityError:
        return attempt, True
    return attempt, False


@method_decorator(csrf_exempt, name="dispatch")
class CheckoutSessionPaymentView(View):
    def post(self, request, session_id):
        session = CheckoutSession.objects.select_related("merchant").filter(session_id=session_id).first()
        if not session:
            return JsonResponse({"ok": False, "error": "checkout session not found"}, status=404)
        allowed_statuses = {
            CheckoutSession.STATUS_CREATED,
            CheckoutSession.STATUS_CONFIRMED,
            CheckoutSession.STATUS_PAYMENT_STARTED,
        }
        if session.status not in allowed_statuses:
            return JsonResponse({"ok": False, "error": "confirm this checkout before starting payment"}, status=409)
        if session.status == CheckoutSession.STATUS_CREATED:
            if not _is_pending_session_valid(session):
                return JsonResponse({"ok": False, "error": "checkout session expired"}, status=410)
            session.status = CheckoutSession.STATUS_CONFIRMED
            session.save(update_fields=["status", "updated_at"])
        order = _find_order(session)
        if not order:
            return JsonResponse({"ok": False, "error": "order not found"}, status=404)

        if session.payment_attempts.filter(status=PaymentAttempt.STATUS_SUCCESS).exists():
            return JsonResponse({"ok": False, "error": "this order is already paid"}, status=409)

        active = session.payment_attempts.filter(status__in=[PaymentAttempt.STATUS_CREATED, PaymentAttempt.STATUS_PENDING], expires_at__gt=timezone.now()).first()
        if active and active.checkout_url:
            return JsonResponse(_checkout_response(active, reused=True))

        provider = getattr(settings, "PAYMENT_PROVIDER", "")
        if provider not in {"mock_omniware", "omniware"}:
            return JsonResponse({"ok": False, "error": "payment provider is not configured"}, status=503)
        attempt = PaymentAttempt.objects.create(
            order=order,
            checkout_session=session,
            provider=provider,
            provider_order_id=_provider_order_id(order),
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
            status=PaymentAttempt.STATUS_PENDING,
        )
        payload = _payment_payload(request, attempt)
        attempt.request_payload = payload
        try:
            if provider == "mock_omniware":
                attempt.provider_uuid = uuid.uuid4().hex
                attempt.checkout_url = request.build_absolute_uri(reverse("mock-omniware-payment", args=[attempt.provider_uuid]))
                response_payload = {
                    "response_code": "0",
                    "payment_url": attempt.checkout_url,
                    "qr_payload": attempt.checkout_url,
                    "payment_mode": "UPI_QR",
                    "mock": True,
                }
            else:
                _sent, response_payload = create_payment_request(payload)
                attempt.provider_uuid = str(response_payload.get("uuid") or response_payload.get("id") or "")
                attempt.checkout_url = str(response_payload.get("payment_url") or response_payload.get("paymentUrl") or response_payload.get("url") or "")
                has_qr = any(response_payload.get(key) for key in ("qr_image_url", "qrImageUrl", "qr_code", "qrCode", "qr_payload", "upi_intent", "upiIntent"))
                if not has_qr:
                    raise OmniwareError("Omniware did not return a UPI QR")
        except OmniwareError as exc:
            attempt.status = PaymentAttempt.STATUS_FAILED
            attempt.response_message = str(exc)
            attempt.save()
            return JsonResponse({"ok": False, "error": str(exc)}, status=502)
        attempt.response_payload = response_payload
        attempt.save()
        session.status = CheckoutSession.STATUS_PAYMENT_STARTED
        session.save(update_fields=["status", "updated_at"])
        return JsonResponse(_checkout_response(attempt, reused=False))


class CheckoutSessionPaymentStatusView(View):
    def get(self, request, session_id):
        session = CheckoutSession.objects.filter(session_id=session_id).first()
        if not session:
            return JsonResponse({"ok": False, "error": "checkout session not found"}, status=404)
        attempt = session.payment_attempts.first()
        if not attempt:
            return JsonResponse({"ok": True, "status": "NOT_STARTED"})
        return JsonResponse({
            "ok": True,
            "status": attempt.status,
            "message": attempt.response_message,
            "order_id": attempt.order.merchant_order_id,
            "transaction_id": attempt.provider_transaction_id,
            "return_url": session.source_url,
            "updated_at": attempt.updated_at.isoformat(),
        })


@method_decorator(csrf_exempt, name="dispatch")
class OmniwarePaymentCallbackView(View):
    def post(self, request):
        payload = {key: request.POST.get(key) for key in request.POST}
        try:
            attempt, duplicate = process_omniware_callback(payload)
        except PermissionError as exc:
            return HttpResponseForbidden(str(exc))
        except LookupError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=404)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        if request.POST.get("mock_return") == "1" and attempt.checkout_session:
            return redirect(f"{_frontend_return_url(request, attempt.checkout_session)}&status={attempt.status.lower()}")
        return JsonResponse({"ok": True, "duplicate": duplicate, "status": attempt.status})


@method_decorator(csrf_exempt, name="dispatch")
class MockOmniwarePaymentView(View):
    def _attempt(self, provider_uuid):
        if not settings.DEBUG or getattr(settings, "PAYMENT_PROVIDER", "") != "mock_omniware":
            raise Http404
        attempt = PaymentAttempt.objects.select_related("order", "order__merchant", "checkout_session").filter(provider_uuid=provider_uuid).first()
        if not attempt:
            raise Http404
        return attempt

    def get(self, request, provider_uuid):
        return render(request, "payments/mock_omniware_payment.html", {"attempt": self._attempt(provider_uuid)})

    def post(self, request, provider_uuid):
        attempt = self._attempt(provider_uuid)
        outcome = str(request.POST.get("outcome") or "pending")
        codes = {"success": ("0", "Payment successful"), "failed": ("1001", "Payment failed"), "cancelled": ("1043", "Payment cancelled"), "pending": ("1006", "Payment pending")}
        code, message = codes.get(outcome, codes["pending"])
        payload = {
            "order_id": attempt.provider_order_id,
            "transaction_id": f"MOCK{uuid.uuid4().hex[:18].upper()}",
            "amount": _amount(attempt.order.order_amount),
            "currency": attempt.order.order_currency or "INR",
            "response_code": code,
            "response_message": message,
            "status": outcome.upper(),
            "payment_mode": "UPI",
            "payment_channel": "MOCK_OMNIWARE",
            "mock_return": "1",
        }
        payload["hash"] = generate_hash(payload, getattr(settings, "OMNIWARE_SALT", ""))
        attempt, _duplicate = process_omniware_callback(payload)
        return redirect(f"{_frontend_return_url(request, attempt.checkout_session)}&status={attempt.status.lower()}")
