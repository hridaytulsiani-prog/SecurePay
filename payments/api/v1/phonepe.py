# PhonePe Standard Checkout (v2) integration.
#
# --- The three-view flow, and why there are three of them ------------------
# A hosted-checkout payment naturally has three separate HTTP touch-points,
# and each gets its own view here because they're triggered by different
# parties and need different auth:
#
#   1. PhonePeInitiateView (POST, merchant-authenticated) — the merchant's
#      own backend/frontend calls this to start a payment. It creates/updates
#      the local OrderInfo row as "pending", then calls PhonePe to get back
#      a checkout URL, which the merchant's frontend redirects the shopper's
#      browser to.
#   2. PhonePeReturnView (GET, public/no-auth) — PhonePe redirects the
#      shopper's BROWSER back here after they finish (or abandon) checkout.
#      Critically, the query params PhonePe puts on this redirect are NOT
#      trustworthy proof of payment (a shopper could hand-edit the URL), so
#      this view always re-verifies the real status directly with PhonePe's
#      API (_verify_phonepe_order) rather than believing the URL. It then
#      redirects the browser again, this time to the merchant's own store UI,
#      with the verified status attached as query params for the frontend
#      to display.
#   3. PhonePeCallbackView (POST, public/no-auth) — a server-to-server
#      webhook PhonePe calls directly (not through the shopper's browser).
#      This can arrive before, after, or completely independently of #2 (a
#      shopper closing their browser tab right after paying, before the
#      redirect completes, is a common case where ONLY this webhook fires).
#      Because of that race, both #2 and #3 write to OrderInfo.order_status
#      independently and idempotently — neither one assumes the other has
#      or hasn't already run.
#
# All three end up updating the same OrderInfo row, found by
# merchant_order_id (ours) or phonepe_order_id/pa_payment_id (PhonePe's).
#
# --- Security note: PhonePeCallbackView has no signature verification ------
# PhonePeCallbackView is `public` — no auth on the view — because PhonePe
# itself can't present one of our merchant session tokens. That's expected.
# What's NOT ideal: this implementation does not verify that the webhook
# body actually came from PhonePe (e.g. via a signature header PhonePe
# includes). As written, anyone who knows/guesses a merchant_order_id could
# POST a fake "SUCCESS" callback to this URL and mark that order as paid in
# our DB — that's just recording an order_status, so it doesn't hand out
# money by itself, but it could confuse fulfillment/shipping if unaddressed.
# If PhonePe's docs specify a callback signature (they typically do), adding
# that verification here would close this gap; left as-is per not making
# unrequested behavior changes, but flagging it because it's the kind of
# thing that's expensive to discover after the fact.
import json
import logging
from decimal import Decimal
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from payments.api.v1.phonepe_auth import get_phonepe_oauth_token
from payments.auth import get_merchant_id_from_token
from payments.models.orderinfo import OrderInfo
from payments.models.customerinfo import CustomerInfo
from payments.models.merchantinfo import MerchantInfo

logger = logging.getLogger(__name__)


def _extract_session_token(request):
    """Find the merchant's session token wherever the caller put it — an
    Authorization: Bearer header, a cookie (from the merchant login flow's
    Set-Cookie), or a field in the request body/a custom header. Only
    PhonePeInitiateView (the one authenticated view in this file) uses this;
    the token itself is then checked by payments.auth.get_merchant_id_from_token."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()

    cookie_token = request.COOKIES.get("token") or request.COOKIES.get("auth_token")
    if cookie_token:
        return cookie_token

    data = request.data or {}
    return data.get("auth_token") or data.get("token") or request.headers.get("X-Auth-Token")


def _map_phonepe_status_to_order_status(status_str: str) -> str:
    """Collapse PhonePe's various status vocabularies down to our own
    order_status values (paid / payment_failed / pending)."""
    s = (status_str or "").upper()
    if s in ("SUCCESS", "SUCCESSFUL", "COMPLETED", "PAID"):
        return "paid"
    if s in ("FAILED", "FAILURE", "DECLINED", "ERROR"):
        return "payment_failed"
    return "pending"

def _extract_verify_fields(v: dict):
    # NOTE: this function is defined but never called anywhere in this file
    # — PhonePeReturnView.get() duplicates equivalent parsing logic inline
    # instead (see the `payment_status = (...)` block a bit further down in
    # this file). Looks like leftover/superseded code; kept because deleting
    # it would be a behavior-adjacent cleanup, not a comment.
    #
    # Common PhonePe V2 shapes:
    # { "success": true, "code":"PAYMENT_SUCCESS", "data": {...} }
    # or direct { "state": "...", "amount": ... }
    data = v.get("data", {}) if isinstance(v.get("data"), dict) else {}

    payment_status = (
        data.get("state")
        or data.get("status")
        or v.get("state")
        or v.get("status")
        or v.get("paymentStatus")
        or "PENDING"
    )

    amount_paise = (
        data.get("amount")
        or data.get("paymentDetails", {}).get("amount")
        or v.get("amount")
        or 0
    )

    payment_id = (
        data.get("transactionId")
        or data.get("paymentId")
        or data.get("txnId")
        or v.get("transactionId")
        or v.get("paymentId")
        or v.get("txnId")
        or ""
    )

    # Normalize status from code if present
    code = (v.get("code") or "").upper()
    if code in ("PAYMENT_SUCCESS", "SUCCESS"):
        payment_status = "SUCCESS"
    elif code in ("PAYMENT_ERROR", "PAYMENT_DECLINED", "FAILURE", "FAILED"):
        payment_status = "FAILED"

    return str(payment_status).upper(), int(amount_paise or 0), payment_id

def _verify_phonepe_order(order_id: str) -> dict:
    """Ask PhonePe directly for an order's current status (used by
    PhonePeReturnView) rather than trusting the redirect's query params."""
    token = get_phonepe_oauth_token()
    verify_url = settings.PHONEPE_STATUS_URL.format(orderId=order_id)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"O-Bearer {token}",
    }

    resp = requests.get(verify_url, headers=headers, timeout=20)

    # Always capture raw response for debugging
    raw_text = resp.text or ""
    content_type = resp.headers.get("content-type", "")

    if not resp.ok:
        raise Exception(
            f"PhonePe verify failed: HTTP {resp.status_code}, "
            f"content-type={content_type}, body={raw_text[:500]}"
        )

    # If body is not JSON, return structured fallback
    if "application/json" not in content_type.lower():
        return {
            "status": "PENDING",
            "_non_json": True,
            "_http_status": resp.status_code,
            "_content_type": content_type,
            "_raw_body": raw_text[:1000],
        }

    try:
        return resp.json()
    except Exception:
        return {
            "status": "PENDING",
            "_json_parse_error": True,
            "_http_status": resp.status_code,
            "_content_type": content_type,
            "_raw_body": raw_text[:1000],
        }


class PhonePeInitiateView(APIView):
    """Merchant-authenticated: creates/updates the local order as 'pending'
    then calls PhonePe to get a checkout redirect URL for the browser.

    Expected POST body (all from the merchant's own checkout frontend):
        merchantOrderId / merchantTransactionId (str, required): the
            merchant's own order reference — either key name is accepted.
        amount (int, required): amount in PAISE (i.e. rupees * 100), must be
            >= 100 (PhonePe's minimum, i.e. Rs 1).
        redirectUrl (str, required): where PhonePe should send the shopper's
            browser after checkout — that URL is PhonePeReturnView's own
            endpoint (/payments/phonepe/return/), NOT the merchant's store,
            since PhonePeReturnView is the one that verifies status before
            forwarding on to the store UI.
        mobileNumber (str, optional): prefills the PhonePe checkout UI and
            is used to find/create the CustomerInfo row.
        customerName / customerEmail (str, optional): only used if a new
            CustomerInfo row needs to be created.

    Auth: merchant session token, found via _extract_session_token() and
    validated by payments.auth.get_merchant_id_from_token — same mechanism
    used everywhere else a merchant needs to be identified.
    """

    def post(self, request):
        data = request.data or {}

        merchant_order_id = data.get("merchantOrderId") or data.get("merchantTransactionId")
        amount_paise = int(data.get("amount") or 0)
        redirect_url = data.get("redirectUrl")
        mobile = data.get("mobileNumber")
        customer_name = (data.get("customerName") or "").strip()
        customer_email = (data.get("customerEmail") or "").strip()
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        customer_address = (data.get("customerAddress") or metadata.get("address") or "Not provided").strip()

        if not merchant_order_id:
            return Response({"ok": False, "error": "merchantOrderId is required"}, status=status.HTTP_400_BAD_REQUEST)
        if amount_paise < 100:
            return Response({"ok": False, "error": "amount must be at least 100 paise"}, status=status.HTTP_400_BAD_REQUEST)
        if not redirect_url:
            return Response({"ok": False, "error": "redirectUrl is required"}, status=status.HTTP_400_BAD_REQUEST)
        if len(merchant_order_id) > 63:
            return Response({"ok": False, "error": "merchantOrderId must be <= 63 characters"}, status=status.HTTP_400_BAD_REQUEST)

        token = _extract_session_token(request)
        merchant_id = get_merchant_id_from_token(token)
        if merchant_id is None:
            return Response({"ok": False, "error": "Invalid or expired merchant token"}, status=status.HTTP_401_UNAUTHORIZED)

        merchant = MerchantInfo.objects.filter(id=merchant_id).first()
        if merchant is None:
            return Response({"ok": False, "error": "No merchant found for token"}, status=status.HTTP_400_BAD_REQUEST)

        # Customer resolution: CustomerInfo.customer_phone is unique, so a
        # returning customer (same mobile number) reuses their existing row
        # instead of creating a duplicate — same pattern as
        # GenerateOrder.generate_order() in generate_order.py.
        if mobile:
            customer, _ = CustomerInfo.objects.get_or_create(
                customer_phone=mobile,
                defaults={
                    "customer_name": customer_name or "Guest",
                    "customer_email": customer_email,
                    "customer_address": customer_address,
                },
            )
        else:
            # customer_phone is required+unique on CustomerInfo (no mobile
            # provided), so a synthetic placeholder is generated instead of
            # leaving the field blank. NOTE: because it's derived from
            # merchant_order_id, calling this endpoint twice for the same
            # order without a mobile number would collide on the unique
            # constraint on the second attempt — not handled here.
            customer = CustomerInfo.objects.create(
                customer_phone=f"TEMP-{merchant_order_id}"[:50],
                customer_name=customer_name or "Guest",
                customer_email=customer_email,
                customer_address=customer_address,
            )

        amount_rupees = Decimal(amount_paise) / Decimal("100")

        # Create-or-update rather than create-only: lets the merchant safely
        # retry initiating the same merchant_order_id (e.g. after a network
        # blip) without hitting a duplicate-order error.
        order = OrderInfo.objects.filter(merchant_order_id=merchant_order_id).first()
        if order is None:
            order = OrderInfo.objects.create(
                merchant=merchant,
                merchant_order_id=merchant_order_id,
                order_amount=amount_rupees,
                order_currency="INR",
                order_status="pending",
                customer_info=customer,
                payment_provider="phonepe",
            )
        else:
            order.merchant = order.merchant or merchant
            order.customer_info = order.customer_info or customer
            order.order_amount = amount_rupees
            order.order_currency = "INR"
            order.order_status = "pending"
            order.payment_provider = "phonepe"
            order.save()

        # Get PhonePe token
        try:
            token = get_phonepe_oauth_token()
        except Exception as e:
            logger.exception("PhonePe OAuth token fetch failed")
            return Response(
                {"ok": False, "error": "PhonePe authentication failed", "details": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # PhonePe payload
        payload = {
            "merchantOrderId": merchant_order_id,
            "amount": amount_paise,
            "paymentFlow": {
                "type": "PG_CHECKOUT",
                "merchantUrls": {"redirectUrl": redirect_url},
            },
            "expireAfter": 1200,
        }
        if mobile:
            payload["prefillUserLoginDetails"] = {"phoneNumber": mobile}

        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"O-Bearer {token}",
            }
            resp = requests.post(settings.PHONEPE_INIT_URL, json=payload, headers=headers, timeout=15)
            logger.info("PhonePe initiate response status=%s body=%s", resp.status_code, resp.text)
            resp.raise_for_status()
            resp_json = resp.json()
        except requests.RequestException as e:
            logger.exception("PhonePe initiate call failed")
            order.phonepe_raw_response = {"error": str(e), "response": getattr(e.response, "text", None)}
            order.payment_provider = "phonepe"
            order.order_status = "payment_failed"
            order.save(update_fields=["phonepe_raw_response", "payment_provider", "order_status"])
            return Response(
                {"ok": False, "error": "PhonePe payment initiation failed", "details": str(e)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Save PhonePe order details
        phonepe_order_id = resp_json.get("orderId")
        order.phonepe_order_id = phonepe_order_id or order.phonepe_order_id
        order.pa_order_id = phonepe_order_id or order.pa_order_id
        order.phonepe_raw_response = resp_json
        order.payment_provider = "phonepe"
        order.order_status = "pending"
        order.save(update_fields=[
            "phonepe_order_id", "pa_order_id", "phonepe_raw_response", "payment_provider", "order_status"
        ])

        return Response(resp_json, status=resp.status_code)


class PhonePeReturnView(APIView):
    """Public (no auth) — the browser's redirect target after checkout.
    Actively re-verifies status with PhonePe rather than trusting query
    params, then bounces the shopper to the store UI with the outcome.

    This is the redirectUrl passed into PhonePeInitiateView's PhonePe call,
    so PhonePe controls exactly what query params land here — expect
    merchantOrderId/merchantTransactionId and orderId, but treat them only
    as a hint for which order to look up, never as proof of payment status
    (see _verify_phonepe_order below, which is the actual source of truth).

    Ends by redirecting again to /merchants/store/ with the verified status
    as query params, for that page's frontend JS to read and display.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        merchant_order_id = request.GET.get("merchantOrderId") or request.GET.get("merchantTransactionId") or ""
        phonepe_order_id = request.GET.get("orderId") or ""

        order = None
        if merchant_order_id:
            order = OrderInfo.objects.filter(merchant_order_id=merchant_order_id).first()
        if not order and phonepe_order_id:
            order = OrderInfo.objects.filter(phonepe_order_id=phonepe_order_id).first()

        payment_status = "PENDING"
        amount_paise = 0
        payment_id = ""
        verify_resp = {}

        # This whole try/except is the "actively re-verify" step: call
        # PhonePe's own status API rather than believing the redirect's
        # query params. Any failure here (network error, PhonePe down) just
        # falls through with payment_status still "PENDING" — the DB
        # fallback logic right below then has a chance to recover a better
        # answer from whatever the callback webhook already saved.
        try:
            verify_id = merchant_order_id or (order.merchant_order_id if order else "")
            if verify_id:
                verify_resp = _verify_phonepe_order(verify_id)

                # handle nested shapes
                data = verify_resp.get("data", {}) if isinstance(verify_resp.get("data"), dict) else {}

                payment_status = (
                    data.get("state")
                    or data.get("status")
                    or verify_resp.get("state")
                    or verify_resp.get("status")
                    or "PENDING"
                ).upper()

                amount_paise = int(
                    data.get("amount")
                    or verify_resp.get("amount")
                    or 0
                )

                payment_id = (
                    data.get("paymentDetails", {}).get("transactionId")
                    or data.get("transactionId")
                    or data.get("paymentId")
                    or ""
                )

                # if non-json/204 fallback, do not force pending forever
                if verify_resp.get("_non_json") or verify_resp.get("_json_parse_error"):
                    # keep previous DB status if already paid via callback
                    if order and order.order_status == "paid":
                        payment_status = "SUCCESS"

        except Exception:
            logger.exception("PhonePe verify failed")

        # Fallback from DB: if PhonePeCallbackView's webhook already fired
        # and marked this order "paid" (e.g. it arrived before this browser
        # redirect did, or the verify call above just failed/timed out),
        # trust that over a "PENDING" we couldn't actually confirm from
        # PhonePe just now. This is the main place the two independent
        # update paths (webhook vs. browser redirect) reconcile with each
        # other rather than one clobbering the other's better information.
        if order:
            if (not payment_id) and order.phonepe_payment_id:
                payment_id = order.phonepe_payment_id

            if amount_paise <= 0 and order.order_amount:
                amount_paise = int(order.order_amount * 100)

            # if callback already marked paid, honor it
            if order.order_status == "paid":
                payment_status = "SUCCESS"

        mapped = _map_phonepe_status_to_order_status(payment_status)

        if order:
            order.order_status = mapped
            order.payment_provider = "phonepe"
            order.phonepe_order_id = phonepe_order_id or order.phonepe_order_id
            order.phonepe_payment_id = payment_id or order.phonepe_payment_id
            order.pa_payment_id = payment_id or order.pa_payment_id
            order.phonepe_raw_response = {
                "return_query": request.GET.dict(),
                "verify_response": verify_resp,
            }
            order.save()

        ui_url = "/merchants/store/"
        q = urlencode({
            "payment_status": (payment_status or "PENDING").upper(),
            "merchantOrderId": merchant_order_id or (order.merchant_order_id if order else ""),
            "orderId": phonepe_order_id or (order.phonepe_order_id if order else ""),
            "paymentId": payment_id,
            "amount": amount_paise,
        })
        return redirect(f"{ui_url}?{q}")

@method_decorator(csrf_exempt, name="dispatch")
class PhonePeCallbackView(APIView):
    """
    Server-to-server webhook from PhonePe (public, no auth — PhonePe can't
    present our session tokens; see the security note at the top of this
    file about the lack of signature verification here). Can arrive
    independently of, and racing with, PhonePeReturnView; both just
    overwrite order_status idempotently, so whichever runs last "wins" and
    that's fine since they should agree on the actual outcome anyway.

    This is the callback URL registered with PhonePe when initiating a
    payment — settings.PHONEPE_CALLBACK_ENDPOINT is the path
    (/payments/phonepe/callback/) that must be registered on PhonePe's side
    (dashboard or in the initiate payload, depending on PhonePe's setup) for
    this to ever actually get called.

    An unrecognized order (payload doesn't match any OrderInfo by
    merchant_order_id or pa_payment_id) is logged and answered 200 anyway —
    PhonePe will otherwise treat a non-2xx as "delivery failed" and retry
    the webhook repeatedly, so we always acknowledge receipt even when we
    can't act on it.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        raw = request.body or b""
        if not raw:
            return Response({"ok": False, "error": "empty body"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            logger.exception("callback: invalid json")
            return Response({"ok": False, "error": "invalid json"}, status=status.HTTP_400_BAD_REQUEST)

        merchant_order_id = payload.get("merchantOrderId") or payload.get("merchantTransactionId")
        phonepe_txn = payload.get("transactionId") or payload.get("paymentId") or payload.get("txnId")
        status_str = payload.get("status") or payload.get("state") or payload.get("paymentStatus") or ""

        order = None
        if merchant_order_id:
            order = OrderInfo.objects.filter(merchant_order_id=merchant_order_id).first()
        if not order and phonepe_txn:
            order = OrderInfo.objects.filter(pa_payment_id=phonepe_txn).first()

        if not order:
            logger.warning("PhonePe callback unknown order payload=%s", payload)
            return Response({"ok": True, "note": "unknown order logged"}, status=status.HTTP_200_OK)

        order.order_status = _map_phonepe_status_to_order_status(status_str)
        order.payment_provider = "phonepe"
        order.phonepe_order_id = merchant_order_id or order.phonepe_order_id
        order.phonepe_payment_id = phonepe_txn or order.phonepe_payment_id
        order.pa_payment_id = phonepe_txn or order.pa_payment_id
        order.phonepe_raw_response = payload
        order.save()

        return Response({"ok": True, "message": "callback processed"}, status=status.HTTP_200_OK)
