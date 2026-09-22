from django.http import JsonResponse
from django.views import View

from payments.models import MerchantInfo


class ButtonConfigView(View):
    """Public config consumed by the one-line SecurePay button SDK."""

    def get(self, request, *args, **kwargs):
        merchant_key = str(request.GET.get("merchant_key") or request.GET.get("merchantKey") or "").strip()
        if not merchant_key:
            return JsonResponse({"ok": False, "error": "merchant_key is required"}, status=400)

        merchant = MerchantInfo.objects.filter(merchant_key=merchant_key).first()
        if not merchant:
            return JsonResponse({"ok": False, "error": "invalid merchant_key"}, status=404)

        return JsonResponse({
            "ok": True,
            "button_text": "Pay with SecurePay",
            "checkout_field_mapping": merchant.checkout_field_mapping or {},
        })
