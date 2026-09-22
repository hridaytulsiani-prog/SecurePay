import hashlib
from decimal import Decimal

import requests
from django.conf import settings


class OmniwareRefundError(Exception):
    pass


def generate_hash(parameters, salt):
    values = []
    for key in sorted(parameters):
        value = parameters[key]
        if value is None:
            continue
        value = str(value).strip()
        if value:
            values.append(value)
    return hashlib.sha512("|".join([salt, *values]).encode()).hexdigest().upper()


def verify_response_hash(payload, salt):
    response_hash = payload.get("hash")
    if response_hash is None:
        return True
    values = dict(payload)
    values.pop("hash", None)
    return response_hash == generate_hash(values, salt)


def _amount_for_provider(amount):
    return str(Decimal(str(amount)).quantize(Decimal("0.01")))


def _post_form(endpoint, payload):
    api_key = getattr(settings, "OMNIWARE_API_KEY", "")
    salt = getattr(settings, "OMNIWARE_SALT", "")
    if not api_key:
        raise OmniwareRefundError("OMNIWARE_API_KEY is not configured")
    if not salt:
        raise OmniwareRefundError("OMNIWARE_SALT is not configured")

    base_url = getattr(settings, "OMNIWARE_BASE_URL", "").rstrip("/")
    form = {"api_key": api_key, **payload}
    form["hash"] = generate_hash(form, salt)
    response = requests.post(
        f"{base_url}{endpoint}",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=35,
    )
    raw_text = response.text or ""
    try:
        response_payload = response.json()
    except ValueError:
        response_payload = {"raw": raw_text[:1000]}

    if not response.ok:
        raise OmniwareRefundError(
            f"Omniware request failed: HTTP {response.status_code}, body={raw_text[:500]}"
        )
    if isinstance(response_payload, dict) and response_payload.get("error"):
        error = response_payload["error"]
        raise OmniwareRefundError(error.get("message") if isinstance(error, dict) else str(error))
    if isinstance(response_payload, dict) and not verify_response_hash(response_payload, salt):
        raise OmniwareRefundError("Omniware response hash verification failed")

    return form, response_payload


def create_refund(refund_request):
    payment_id = refund_request.provider_payment_id or refund_request.order.pa_payment_id
    if not payment_id:
        raise OmniwareRefundError("Provider payment id is required for refund")

    payload = {
        "transaction_id": payment_id,
        "merchant_refund_id": refund_request.refund_reference[:30],
        "amount": _amount_for_provider(refund_request.amount),
        "description": refund_request.reason,
    }
    return _post_form(getattr(settings, "OMNIWARE_REFUND_ENDPOINT", "/v2/refundrequest"), payload)


def get_refund_status(refund_request):
    payment_id = refund_request.provider_payment_id or refund_request.order.pa_payment_id
    if not payment_id:
        raise OmniwareRefundError("Provider payment id is required for refund status")
    payload = {"transaction_id": payment_id}
    if refund_request.order.merchant_order_id:
        payload["merchant_order_id"] = refund_request.order.merchant_order_id
    return _post_form(getattr(settings, "OMNIWARE_REFUND_STATUS_ENDPOINT", "/v2/refundstatus"), payload)
