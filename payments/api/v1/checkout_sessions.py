import json

from django.conf import settings
from django.http import HttpResponseBadRequest, JsonResponse
from django.utils import timezone
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from payments.models import CheckoutSession, MerchantInfo
from payments.api.v1.generate_order import _get_auth_token_from_request
from payments.auth import get_merchant_id_from_token


CHECKOUT_SESSION_TTL_SECONDS = 5 * 60


def _client_ip(request):
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.META.get("REMOTE_ADDR")


def _checkout_url(session_id, base_url=None):
    base_url = base_url or getattr(settings, "SECUREPAY_CHECKOUT_BASE_URL", "http://localhost:5173/checkout")
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}session_id={session_id}"


def _safe_dict(value):
    return value if isinstance(value, dict) else {}


def _is_pending_session_valid(session):
    return (
        session
        and session.status == CheckoutSession.STATUS_CREATED
        and session.created_at >= timezone.now() - timezone.timedelta(seconds=CHECKOUT_SESSION_TTL_SECONDS)
    )


def _expire_stale_session(session):
    if session and session.status == CheckoutSession.STATUS_CREATED:
        session.status = "expired"
        session.save(update_fields=["status", "updated_at"])


@method_decorator(csrf_exempt, name="dispatch")
class CheckoutSessionView(View):
    """Create the immutable customer/order snapshot sent by button.js."""

    def get(self, request, *args, **kwargs):
        merchant_id = get_merchant_id_from_token(_get_auth_token_from_request(request))
        if merchant_id is None:
            return JsonResponse({"error": "Invalid or expired merchant token"}, status=401)

        sessions = CheckoutSession.objects.filter(merchant_id=merchant_id)[:20]
        return JsonResponse({
            "results": [
                {
                    "session_id": session.session_id,
                    "status": session.status,
                    "customer_snapshot": session.customer_snapshot,
                    "order_snapshot": session.order_snapshot,
                    "page_snapshot": session.page_snapshot,
                    "source_origin": session.source_origin,
                    "source_url": session.source_url,
                    "created_at": session.created_at.isoformat(),
                }
                for session in sessions
            ]
        })

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return HttpResponseBadRequest("invalid json")

        merchant_key = str(payload.get("merchant_key") or payload.get("merchantKey") or "").strip()
        if not merchant_key:
            return JsonResponse({"ok": False, "error": "merchant_key is required"}, status=400)

        merchant = MerchantInfo.objects.filter(merchant_key=merchant_key).first()
        if not merchant:
            return JsonResponse({"ok": False, "error": "invalid merchant_key"}, status=401)

        customer_snapshot = _safe_dict(payload.get("customer"))
        order_snapshot = _safe_dict(payload.get("order"))
        page_snapshot = _safe_dict(payload.get("page"))
        source_origin = str(payload.get("source_origin") or payload.get("sourceOrigin") or "")[:255]
        source_url = str(payload.get("source_url") or payload.get("sourceUrl") or "")
        order_id = str(order_snapshot.get("order_id") or "").strip()

        existing_session = None
        if order_id:
            existing_session = (
                CheckoutSession.objects
                .filter(
                    merchant=merchant,
                    status=CheckoutSession.STATUS_CREATED,
                    source_url=source_url,
                    order_snapshot__order_id=order_id,
                )
                .order_by("-created_at")
                .first()
            )

        if _is_pending_session_valid(existing_session):
            session = existing_session
            session.customer_snapshot = customer_snapshot
            session.order_snapshot = order_snapshot
            session.page_snapshot = page_snapshot
            session.source_origin = source_origin
            session.source_url = source_url
            session.customer_user_agent = request.headers.get("User-Agent", "")
            session.customer_ip = _client_ip(request)
            session.save(update_fields=[
                "customer_snapshot",
                "order_snapshot",
                "page_snapshot",
                "source_origin",
                "source_url",
                "customer_user_agent",
                "customer_ip",
                "updated_at",
            ])
        else:
            _expire_stale_session(existing_session)
            session = CheckoutSession.objects.create(
                merchant=merchant,
                merchant_key=merchant_key,
                customer_snapshot=customer_snapshot,
                order_snapshot=order_snapshot,
                page_snapshot=page_snapshot,
                source_origin=source_origin,
                source_url=source_url,
                customer_user_agent=request.headers.get("User-Agent", ""),
                customer_ip=_client_ip(request),
            )

        return JsonResponse(
            {
                "ok": True,
                "session_id": session.session_id,
                "checkout_url": _checkout_url(session.session_id, str(payload.get("checkout_base_url") or "").strip() or None),
                "reused": session == existing_session,
            },
            status=201,
        )


@method_decorator(csrf_exempt, name="dispatch")
class CheckoutSessionDetailView(View):
    """Public customer-facing checkout session read/decision endpoint."""

    def get(self, request, session_id, *args, **kwargs):
        session = CheckoutSession.objects.filter(session_id=session_id).select_related("merchant").first()
        if not session:
            return JsonResponse({"ok": False, "error": "checkout session not found"}, status=404)
        if session.status == CheckoutSession.STATUS_CREATED and not _is_pending_session_valid(session):
            _expire_stale_session(session)
            return JsonResponse({"ok": False, "error": "checkout session expired"}, status=410)

        return JsonResponse({
            "ok": True,
            "session_id": session.session_id,
            "status": session.status,
            "merchant": {
                "name": session.merchant.merchant_name,
                "email": session.merchant.merchant_email,
            },
            "customer_snapshot": session.customer_snapshot,
            "order_snapshot": session.order_snapshot,
            "page_snapshot": session.page_snapshot,
            "source_url": session.source_url,
            "created_at": session.created_at.isoformat(),
        })

    def post(self, request, session_id, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else {}
        except (UnicodeDecodeError, ValueError):
            return HttpResponseBadRequest("invalid json")

        session = CheckoutSession.objects.filter(session_id=session_id).first()
        if not session:
            return JsonResponse({"ok": False, "error": "checkout session not found"}, status=404)
        if session.status == CheckoutSession.STATUS_CREATED and not _is_pending_session_valid(session):
            _expire_stale_session(session)
            return JsonResponse({"ok": False, "error": "checkout session expired"}, status=410)

        action = str(payload.get("action") or "").strip().lower()
        if action == "confirm":
            session.status = CheckoutSession.STATUS_CONFIRMED
        elif action == "reject":
            session.status = CheckoutSession.STATUS_CANCELLED
        else:
            return JsonResponse({"ok": False, "error": "action must be confirm or reject"}, status=400)

        session.save(update_fields=["status", "updated_at"])
        checkout_base_url = str(payload.get("checkout_base_url") or "").strip()
        payment_url = _checkout_url(session.session_id, checkout_base_url or None)
        payment_url = f"{payment_url}&payment_ready=1" if "?" in payment_url else f"{payment_url}?payment_ready=1"
        return JsonResponse({
            "ok": True,
            "session_id": session.session_id,
            "status": session.status,
            "source_url": session.source_url,
            "payment_url": payment_url,
        })
