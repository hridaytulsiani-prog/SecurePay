# utils/whatsapp.py
#
# WhatsApp sending via GetGabs (https://www.getgabs.com/) — this is the ONLY
# place in the codebase that talks to GetGabs. Every other part of the app
# (delivery notifications, future notification types) should call
# send_whatsapp_text() below rather than hitting the GetGabs API directly,
# so all sends stay logged and auditable in one place.
#
# --- API key setup (do this first, or every send will fail) -----------------
# GetGabs requires an `api_key` in the request body (not a header). It's read
# from settings.GETGABS_API_KEY, which in turn reads the GETGABS_API_KEY
# environment variable (see securepay/settings.py) — falling back to the
# placeholder string "your_production_api_key_here" if that env var isn't set,
# which will make every send fail with a GetGabs auth error.
#
# To fix that: get your API key from the GetGabs dashboard (Settings > API
# Keys, or wherever "How to get Your API key?" points in their docs), then
# set it as an environment variable wherever this Django process runs, e.g.:
#
#     export GETGABS_API_KEY="the-real-key-from-getgabs"
#
# Do NOT hardcode the real key into settings.py or this file — that's how
# keys end up leaked in git history. Use the env var.
#
# --- What this file does -----------------------------------------------------
# send_whatsapp_text() does two things every time it's called:
#   1. POSTs a WhatsApp text message to GetGabs's API for one recipient.
#   2. Writes one WhatsAppMessageLog row recording whether that succeeded or
#      failed, so anyone (support, another developer, you) can later answer
#      "did we actually message this customer?" without re-triggering a send.
#      See tracking/models/whatsapp_log.py for the log model, and Django's
#      built-in /admin/ site (tracking/admin.py registers it there) for a
#      ready-made UI to search/filter that log — no separate dashboard needed.
import requests
import logging
from django.conf import settings

from tracking.models.whatsapp_log import WhatsAppMessageLog

logger = logging.getLogger(__name__)


def _normalize_whatsapp_number(to_number: str) -> str:
    digits = "".join(ch for ch in str(to_number) if ch.isdigit())
    if len(digits) == 10:
        return f"91{digits}"
    return digits or str(to_number)


def send_whatsapp_text(to_number: str, message: str, preview_url: bool = False, *, purpose: str = "", order_id: str = ""):
    """
    Send one WhatsApp text message to one recipient via the GetGabs API, and
    log the attempt (success or failure) to WhatsAppMessageLog.

    HOW IT WORKS, STEP BY STEP:
      1. Create a WhatsAppMessageLog row up front with status="pending",
         before the network call. This means even if the process crashes or
         the request times out with no response, there's still a record that
         a send was *attempted* — you're never left wondering whether nothing
         happened, or whether it happened but we lost track of it.
      2. POST to GetGabs. If that raises for ANY reason (network error,
         timeout, GetGabs returning a non-2xx status via raise_for_status()),
         we mark the log row "failed", save the exception text into
         `error`, and re-raise — so the caller's own error handling (e.g.
         notify_customer_delivered()'s try/except) still runs exactly as
         before. We never swallow the exception here.
      3. On success, GetGabs's response looks like:
             {
               "messaging_product": "whatsapp",
               "contacts": [{"input": "...", "wa_id": "..."}],
               "messages": [{"id": "wamid.XXXXXXXX...."}]
             }
         `wa_id` confirms which WhatsApp account actually received it,
         and the `messages[0].id` ("wamid...") is GetGabs/WhatsApp's own
         message id — the thing you'd quote back to GetGabs support if a
         message needs investigating. Both get pulled out and saved onto
         the log row, along with the full raw response (`raw_response`)
         in case something else in it is ever needed later.

    Args:
        to_number (str): Recipient's WhatsApp number, full international
            format with country code and NO leading "+" or spaces,
            e.g. '919999999999' for an Indian number. GetGabs will reject
            or silently fail on malformed numbers, so always pass a number
            that's already been validated/normalized by the caller.
        message (str): The message text. Plain text only — this function
            always sends type="text"; GetGabs also supports templates/media
            messages but this codebase doesn't use them (yet).
        preview_url (bool): If the message body contains a URL, whether
            WhatsApp should show a link preview card for it. Delivery
            notifications pass True since they always contain the customer's
            enquiry link (see tracking.api.v1.message_templates) and a
            preview makes that link visibly clickable/trustworthy.
        purpose (str): A short free-text tag describing WHY this message was
            sent, e.g. "delivery_enquiry". Purely for later filtering in
            WhatsAppMessageLog / the admin site — has no effect on what's
            actually sent to GetGabs. Pass one for every new notification
            type you add, so the log stays searchable.
        order_id (str): The related order's pa_order_id (payment-aggregator
            order id), if this message is about a specific order. Also just
            for logging/lookup — lets you answer "what messages went out for
            order X?" by filtering WhatsAppMessageLog on this field.

    Returns:
        dict: The parsed JSON response from GetGabs (see the shape above).

    Raises:
        requests.HTTPError: if GetGabs returns a non-2xx response.
        requests.RequestException: on network-level failures (timeout, DNS,
            connection refused, etc.) — anything requests.post() itself can
            raise.
        In both cases, a WhatsAppMessageLog row with status="failed" and the
        error message is saved BEFORE the exception propagates.
    """
    # Log the attempt before doing anything over the network — see point 1
    # in the docstring above for why this matters.
    recipient = _normalize_whatsapp_number(to_number)
    log = WhatsAppMessageLog.objects.create(
        to_number=recipient,
        message_body=message,
        purpose=purpose,
        order_id=order_id,
        status="pending",
    )

    url = "https://app.getgabs.com/sendservicemessages/sendmessages"

    # This exact shape is GetGabs's required request format for a plain text
    # message — see their "Send Text Service Message" API doc. Don't rename
    # or restructure these keys; GetGabs will reject anything that doesn't
    # match this shape exactly.
    payload = {
        "to": recipient,
        "text": {
            "body": message,
            "preview_url": preview_url
        },
        "type": "text",
        "recipient_type": "individual",
        "messaging_product": "whatsapp",
        "api_key": settings.GETGABS_API_KEY  # <-- set via GETGABS_API_KEY env var, see the module-level note above
    }

    headers = {
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()  # raises requests.HTTPError on any 4xx/5xx from GetGabs
        data = response.json()
    except Exception as exc:
        # Whatever went wrong (bad api_key, GetGabs down, network blip,
        # malformed number rejected by GetGabs, etc.) — record it on the log
        # row so it's visible in /admin/ without digging through server logs,
        # then re-raise so the caller still sees the failure and can react
        # (e.g. notify_customer_delivered() just logs a warning and moves on
        # rather than blocking shipment status updates on WhatsApp being up).
        log.status = "failed"
        log.error = str(exc)
        log.save(update_fields=["status", "error", "updated_at"])
        raise

    logger.info("WhatsApp message sent: %s", data)

    # Pull the two fields worth indexing out of GetGabs's response (see the
    # sample shape in the docstring). Defensive .get()s with fallback empty
    # dicts/strings in case GetGabs ever returns a 200 with an unexpected or
    # empty body — we'd still rather save a "sent" row with blank ids than
    # crash after the message has already actually gone out.
    contact = (data.get("contacts") or [{}])[0]
    sent_message = (data.get("messages") or [{}])[0]

    log.status = "sent"
    log.wa_id = contact.get("wa_id", "") or ""
    log.wa_message_id = sent_message.get("id", "") or ""
    log.raw_response = data  # full response kept in case anything else in it is needed later
    log.save(update_fields=["status", "wa_id", "wa_message_id", "raw_response", "updated_at"])

    return data


def send_whatsapp_template(
    to_number: str,
    *,
    template_name: str,
    language_code: str,
    body_parameters: list[str],
    receiver_name: str = "",
    purpose: str = "",
    order_id: str = "",
    campaign_id: str = "",
):
    """Send an approved WhatsApp template with BODY variables through GetGabs."""
    message_body = f"template:{template_name} body={body_parameters}"
    recipient = _normalize_whatsapp_number(to_number)
    log = WhatsAppMessageLog.objects.create(
        to_number=recipient,
        message_body=message_body,
        purpose=purpose,
        order_id=order_id,
        status="pending",
    )

    url = "https://app.getgabs.com/whatsappbusiness/send-templated-message"
    payload = {
        "api_key": settings.GETGABS_API_KEY,
        "sender": _normalize_whatsapp_number(getattr(settings, "GETGABS_WHATSAPP_SENDER", "")),
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "receiver_name": receiver_name or recipient,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": [
                {
                    "type": "BODY",
                    "parameters": [
                        {"type": "text", "text": str(value)}
                        for value in body_parameters
                    ],
                }
            ],
        },
    }
    if campaign_id:
        payload["campaign_id"] = campaign_id
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        log.status = "failed"
        response_text = ""
        response = getattr(exc, "response", None)
        if response is not None:
            response_text = getattr(response, "text", "") or ""
        log.error = f"{exc} {response_text}".strip()
        log.save(update_fields=["status", "error", "updated_at"])
        raise

    logger.info("WhatsApp template message sent: %s", data)
    contact = (data.get("contacts") or [{}])[0]
    sent_message = (data.get("messages") or [{}])[0]
    log.status = "sent"
    log.wa_id = contact.get("wa_id", "") or ""
    log.wa_message_id = sent_message.get("id", "") or ""
    log.raw_response = data
    log.save(update_fields=["status", "wa_id", "wa_message_id", "raw_response", "updated_at"])
    return data
