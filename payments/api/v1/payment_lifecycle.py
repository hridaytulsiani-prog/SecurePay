import json

from django.conf import settings
from django.http import JsonResponse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from payments.api.v1.generate_order import _get_auth_token_from_request
from payments.auth import get_merchant_id_from_token
from payments.models import SettlementRecord
from payments.services.payment_lifecycle import process_payment_webhook
from payments.services.settlements import apply_settlement_status, process_due_settlements


@method_decorator(csrf_exempt, name="dispatch")
class PaymentWebhookView(View):
    """Provider-neutral signed webhook endpoint.

    Provider-specific adapters can normalize their payloads later; the
    lifecycle processor already accepts common order/status/payment keys.
    """

    def post(self, request, provider):
        raw_body = request.body or b""
        signature = request.headers.get("X-Webhook-Signature") or request.headers.get("X-Signature") or ""
        event_id = request.headers.get("X-Event-Id") or request.headers.get("X-Webhook-Id") or ""
        try:
            result = process_payment_webhook(provider, raw_body, signature, event_id, request.headers)
        except PermissionError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=401)
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse(result, status=200)


@method_decorator(csrf_exempt, name="dispatch")
class SettlementRunView(View):
    """Run configured provider settlement checks for the current merchant."""

    def post(self, request):
        merchant_id = get_merchant_id_from_token(_get_auth_token_from_request(request))
        if merchant_id is None:
            return JsonResponse({"error": "Invalid or expired merchant token"}, status=401)
        provider = request.GET.get("provider") or None
        if request.body and getattr(request, "content_type", "").startswith("application/json"):
            try:
                payload = json.loads(request.body.decode("utf-8"))
            except ValueError:
                payload = {}
        else:
            payload = {}
        if payload.get("simulate") and settings.DEBUG:
            records = SettlementRecord.objects.select_related("order").filter(order__merchant_id=merchant_id)
            results = []
            for record in records:
                result = apply_settlement_status(record, payload.get("settlement") or {"status": "SETTLED", "settlementId": f"TEST-{record.order_id}"}, "test_settlement")
                results.append({"order_id": record.order.merchant_order_id, "status": result.status})
            return JsonResponse({"ok": True, "simulated": True, "results": results})
        results = process_due_settlements(provider, merchant_id=merchant_id)
        return JsonResponse({"ok": True, "checked": len(results)})
