import json

from django.http import JsonResponse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from payments.api.v1.generate_order import _get_auth_token_from_request
from payments.auth import get_merchant_id_from_token
from payments.models import OrderInfo, RefundRequest
from payments.services.refunds import create_common_refund, process_refund_webhook


def _refund_json(refund_request):
    return {
        "refund_order": refund_request.order.merchant_order_id,
        "provider": refund_request.provider,
        "refund_status": refund_request.status,
        "refund_id": refund_request.provider_refund_id,
        "refund_reference": refund_request.refund_reference,
        "reason": refund_request.reason,
        "amount": str(refund_request.amount),
        "currency": refund_request.currency,
        "verification": refund_request.verification_snapshot,
    }


@method_decorator(csrf_exempt, name="dispatch")
class RefundRequestView(View):
    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else {}
        except ValueError:
            return JsonResponse({"ok": False, "error": "invalid json"}, status=400)

        merchant_id = get_merchant_id_from_token(_get_auth_token_from_request(request, payload))
        if merchant_id is None:
            return JsonResponse({"error": "Invalid or expired merchant token"}, status=401)

        merchant_order_id = str(payload.get("merchantOrderId") or payload.get("order_id") or "").strip()
        pa_order_id = str(payload.get("paOrderId") or payload.get("providerOrderId") or "").strip()
        if not merchant_order_id and not pa_order_id:
            return JsonResponse({"ok": False, "error": "merchantOrderId or providerOrderId is required"}, status=400)

        order = OrderInfo.objects.filter(merchant_id=merchant_id, merchant_order_id=merchant_order_id).first()
        if not order and pa_order_id:
            order = OrderInfo.objects.filter(merchant_id=merchant_id, pa_order_id=pa_order_id).first()
        if not order:
            return JsonResponse({"ok": False, "error": "order not found"}, status=404)
        if order.order_status not in {"paid", "refund_pending", "refunded"}:
            return JsonResponse({"ok": False, "error": "only paid orders can be refunded"}, status=400)

        submit = bool(payload.get("submit", True))
        try:
            refund_request = create_common_refund(
                order,
                payload.get("reason") or None,
                submit=submit,
                actor_display=f"merchant:{merchant_id}",
                actor_role="Merchant",
                request=request,
            )
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            existing = RefundRequest.objects.filter(order=order).first()
            body = {"ok": False, "error": str(exc)}
            if existing:
                body["refund"] = _refund_json(existing)
            return JsonResponse(body, status=502)

        return JsonResponse({"ok": True, "refund": _refund_json(refund_request)}, status=201)


@method_decorator(csrf_exempt, name="dispatch")
class RefundWebhookView(View):
    def post(self, request, provider, *args, **kwargs):
        signature = (
            request.headers.get("x-fsk-wh-chksm")
            or request.headers.get("X-Webhook-Signature")
            or request.headers.get("X-Signature")
            or ""
        )
        try:
            result = process_refund_webhook(provider, request.body or b"", signature)
        except PermissionError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=401)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse(result, status=200)
