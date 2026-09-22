"""DHL public tracking for Blue Dart shipment tests.

The public DHL tracker returns JSON for its browser tracking flow. This module
keeps the complete response and turns its nested values into the same
``fields``/``scans`` shape used by the admin OTP evidence pipeline.
"""

import json
import hashlib
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET

import requests

from tracking.api.v1.bluedart_otp import (
    _html_to_text,
    parse_bluedart_otp_status,
)


DHL_BLUEDART_TRACK_URL = os.getenv(
    "DHL_BLUEDART_PUBLIC_TRACK_URL",
    "https://www.dhl.com/utapi",
)
DHL_BLUEDART_TIMEOUT_SECONDS = float(os.getenv("DHL_BLUEDART_TIMEOUT_SECONDS", "60"))


def fetch_dhl_bluedart_tracking(awb):
    """Fetch a Blue Dart AWB through DHL's public tracking endpoint."""
    params = {
        "trackingNumber": awb,
        "language": "en",
        "requesterCountryCode": "IN",
        "source": "tt",
    }
    page_url = f"https://www.dhl.com/in-en/home/tracking.html?tracking-id={awb}&submit=1&inputsource=marketingstage"
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Referer": page_url,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    }

    with requests.Session() as session:
        session.headers.update(headers)
        session.get(
            page_url,
            headers={
                **headers,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            },
            timeout=DHL_BLUEDART_TIMEOUT_SECONDS,
        )
        response = session.get(
            DHL_BLUEDART_TRACK_URL,
            params=params,
            timeout=DHL_BLUEDART_TIMEOUT_SECONDS,
        )
        payload = _response_payload(response.text)

        if _is_crypto_challenge(payload):
            _solve_crypto_challenge(session, payload, page_url)
            response = session.get(
                DHL_BLUEDART_TRACK_URL,
                params=params,
                timeout=DHL_BLUEDART_TIMEOUT_SECONDS,
            )

        return response.status_code, normalize_dhl_bluedart_response(awb, response.url, response.text)


def _response_payload(body):
    try:
        return json.loads(body or "")
    except (TypeError, ValueError):
        return {}


def _is_crypto_challenge(payload):
    return (
        isinstance(payload, dict)
        and str(payload.get("sec-cp-challenge", "")).lower() == "true"
        and str(payload.get("provider", "")).lower() == "crypto"
    )


def _solve_crypto_challenge(session, challenge, page_url):
    """Complete DHL's short-lived crypto challenge using the session cookie."""
    duration = int(challenge.get("chlg_duration") or 0)
    if duration > 0:
        time.sleep(min(duration, 10))

    sec_cookie = next(
        (cookie.value for cookie in session.cookies if cookie.name == "sec_cpt"),
        "",
    )
    sec, separator, _ = sec_cookie.partition("~")
    if not separator:
        raise ValueError("DHL public tracking returned a crypto challenge without a session cookie.")

    difficulty = int(challenge.get("difficulty") or 0)
    count = int(challenge.get("count") or 1)
    if difficulty <= 0:
        raise ValueError("DHL public tracking returned an invalid crypto challenge difficulty.")

    answers = []
    for _ in range(count):
        while True:
            answer = f"0.{secrets.token_hex(8)}"
            digest_input = f"{sec}{challenge.get('timestamp', 0)}{challenge.get('nonce', '')}{difficulty}{answer}"
            output = 0
            for byte in hashlib.sha256(digest_input.encode("ascii")).digest():
                output = ((output << 8) | byte) % difficulty
            if output == 0:
                answers.append(answer)
                difficulty += 1
                break

    origin = "https://www.dhl.com"
    verify_headers = {
        "Accept": "*/*",
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": origin,
        "Referer": page_url,
        "User-Agent": session.headers.get("User-Agent", ""),
    }
    session.post(
        f"{origin}/_sec/verify?provider=crypto",
        data=json.dumps({"token": challenge.get("token", ""), "answers": answers}),
        headers=verify_headers,
        timeout=DHL_BLUEDART_TIMEOUT_SECONDS,
    )

    verify_url = challenge.get("verify_url") or f"{origin}/_sec/cp_challenge/verify"
    session.get(
        verify_url,
        headers={
            "Accept": "*/*",
            "Referer": page_url,
            "User-Agent": session.headers.get("User-Agent", ""),
        },
        timeout=DHL_BLUEDART_TIMEOUT_SECONDS,
    )


def normalize_dhl_bluedart_response(awb, url, body):
    """Return a JSON-serializable, searchable representation of XML/JSON."""
    raw = body
    parsed = None
    content = body or ""

    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        try:
            parsed = ET.fromstring(content)
        except (ET.ParseError, TypeError):
            parsed = None

    if isinstance(parsed, dict):
        fields = _flatten_fields(parsed)
        text = json.dumps(parsed, ensure_ascii=False, default=str)
        raw_response = parsed
    elif isinstance(parsed, ET.Element):
        fields = _xml_fields(parsed)
        text = "\n".join(f"{key}: {value}" for key, value in fields.items())
        raw_response = content[:20000]
    else:
        fields = {}
        text = _html_to_text(content)
        raw_response = content[:20000]

    scans = _extract_scans(fields)
    return {
        "source": "dhl_bluedart_tracking_api",
        "url": url,
        "searched_awb": awb,
        "fields": fields,
        "scans": scans,
        "text": text[:20000],
        "raw": raw_response,
    }


def _flatten_fields(value, prefix="", output=None):
    output = output if output is not None else {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            _flatten_fields(child, child_prefix, output)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _flatten_fields(child, f"{prefix}[{index}]", output)
    elif value is not None:
        output[prefix] = str(value)
        short_key = prefix.rsplit(".", 1)[-1]
        short_key = re.sub(r"\[\d+\]$", "", short_key)
        output.setdefault(short_key, str(value))
    return output


def _xml_fields(root):
    fields = {}
    for element in root.iter():
        value = " ".join((element.text or "").split())
        if not value:
            continue
        key = element.tag.rsplit("}", 1)[-1]
        if key in fields:
            existing = fields[key]
            fields[key] = f"{existing} | {value}"
        else:
            fields[key] = value
    return fields


def _extract_scans(fields):
    scans = []
    for key, value in fields.items():
        if not re.search(r"scan|status|event|delivery", key, re.I):
            continue
        scans.append({
            "state_label": key,
            "scan": value,
            "scan_type": "",
            "scan_nsl_remark": "",
            "city_location": "",
            "scanned_location": "",
            "scan_date_time": "",
        })
    return scans[:50]


def parse_dhl_bluedart_otp_status(payload):
    """Reuse Blue Dart's OTP vocabulary against the DHL-normalized payload."""
    parsed = parse_bluedart_otp_status(payload)
    field_evidence = _extract_otp_code_fields(payload)
    if field_evidence:
        parsed["evidence"] = (parsed.get("evidence") or []) + field_evidence[:20]
        if parsed.get("otp_status") == "UNKNOWN":
            parsed["otp_status"] = (
                "OTP_FIELD_PRESENT"
                if any(item["type"] == "OTP_FIELD" for item in field_evidence)
                else "CODE_FIELD_PRESENT"
            )
    return parsed


def _extract_otp_code_fields(value, path=""):
    evidence = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if "otp" in normalized_key:
                evidence.append({
                    "type": "OTP_FIELD",
                    "path": child_path,
                    "match": str(key),
                    "value": child if isinstance(child, (str, int, float, bool)) else json.dumps(child, default=str),
                })
            elif "code" in normalized_key or "pin" in normalized_key:
                evidence.append({
                    "type": "CODE_FIELD",
                    "path": child_path,
                    "match": str(key),
                    "value": child if isinstance(child, (str, int, float, bool)) else json.dumps(child, default=str),
                })
            evidence.extend(_extract_otp_code_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            evidence.extend(_extract_otp_code_fields(child, f"{path}[{index}]"))
    return evidence


def extract_dhl_bluedart_tracking_summary(payload):
    """Map common DHL/Blue Dart names into the admin OTP result shape."""
    fields = payload.get("fields") if isinstance(payload, dict) else {}
    fields = fields if isinstance(fields, dict) else {}
    scans = payload.get("scans") if isinstance(payload, dict) else []
    scans = scans if isinstance(scans, list) else []

    def value(*names):
        wanted = {re.sub(r"[^a-z0-9]", "", name.lower()) for name in names}
        for key, field_value in fields.items():
            short_key = key.rsplit(".", 1)[-1]
            short_key = re.sub(r"\[\d+\]$", "", short_key)
            normalized_key = re.sub(r"[^a-z0-9]", "", short_key.lower())
            if normalized_key in wanted:
                return field_value
        return ""

    status = value("Status", "ShipmentStatus", "CurrentStatus", "StatusDescription")
    delivery_date = value("DateOfDelivery", "DeliveryDate", "DeliveredDate")
    delivery_time = value("TimeOfDelivery", "DeliveryTime")
    return {
        "message": value("Message", "ErrorMessage"),
        "carrier_status_code": value("StatusCode", "ResponseCode"),
        "awb": value("WaybillNo", "Waybill", "AWB", "TrackingNumber") or payload.get("searched_awb", ""),
        "current_flow": value("CurrentFlow", "ShipmentType", "ProductType"),
        "current_track_index": None,
        "delivery_date": delivery_date,
        "delivery_date_label": delivery_date,
        "delivery_pill_label": status,
        "hq_status": status,
        "product_type": value("ProductType", "Product", "ServiceType"),
        "reference_no": value("ReferenceNo", "ReferenceNumber", "OrderId", "OrderNumber"),
        "status": status,
        "status_type": value("StatusType", "EventType"),
        "status_datetime": f"{delivery_date} {delivery_time}".strip(),
        "instructions": value("Instructions", "Remarks", "Remark"),
        "scan_count": len(scans),
        "latest_scan": scans[0] if scans else None,
        "scans": scans[:50],
        "extra_fields": {
            "pickup_date": value("PickupDate", "BookedDate", "ShipmentDate"),
            "from": value("From", "Origin", "OriginLocation"),
            "to": value("To", "Destination", "DestinationLocation"),
            "recipient": value("Recipient", "Consignee", "ReceiverName"),
            "public_endpoint": payload.get("url", ""),
            "all_fields": fields,
        },
    }
