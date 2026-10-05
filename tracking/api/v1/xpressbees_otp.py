import re

import requests
from django.conf import settings


XPRESSBEES_TRACK_URL = "https://shipment.xpressbees.com/api/shipments2/track/{awb}"


def normalize_awb(awb):
    return re.sub(r"[^A-Za-z0-9_-]", "", (awb or "").strip())


def fetch_xpressbees_tracking(awb):
    token = getattr(settings, "XPRESSBEES_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("XPRESSBEES_API_TOKEN is not configured on the server")

    response = requests.get(
        XPRESSBEES_TRACK_URL.format(awb=awb),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "SecurePay/1.0 Xpressbees OTP Verification",
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.status_code, response.json()


def _walk_values(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _walk_values(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_values(child, f"{path}[{index}]")
    elif value is not None:
        yield path, str(value)


def _find_value(payload, names):
    wanted = {name.lower() for name in names}
    for path, value in _walk_values(payload):
        key = path.rsplit(".", 1)[-1].split("[", 1)[0].lower()
        if key in wanted and value:
            return value
    return ""


def extract_xpressbees_tracking_summary(payload):
    fields = [{"path": path, "value": value} for path, value in _walk_values(payload)]
    otp_fields = [
        field
        for field in fields
        if re.search(r"otp|verification|verified|delivery.?code|security.?code", f"{field['path']} {field['value']}", re.I)
    ]
    status = _find_value(payload, ("status", "current_status", "shipment_status", "tracking_status"))
    awb = _find_value(payload, ("awb", "awb_number", "tracking_number"))
    delivery_status = "DELIVERED" if any(
        "delivered" in field["value"].lower() for field in fields
    ) else status

    return {
        "awb": awb,
        "status": status,
        "delivery_status": delivery_status,
        "otp_fields": otp_fields[:50],
        "fields": fields[:200],
    }


def parse_xpressbees_otp_status(payload):
    fields = list(_walk_values(payload))
    evidence = []
    joined = " ".join(f"{path} {value}" for path, value in fields).lower()

    for path, value in fields:
        text = f"{path} {value}"
        lower_text = text.lower()
        if re.search(r"otp.{0,30}(verified|success)|(?:verified|success).{0,30}otp", lower_text):
            evidence.append({"type": "OTP_VERIFIED", "path": path, "match": value, "value": value})
        elif re.search(r"code.{0,30}(verified|success)|(?:verified|success).{0,30}code", lower_text):
            evidence.append({"type": "CODE_VERIFIED", "path": path, "match": value, "value": value})
        elif re.search(r"otp|verification|delivery.?code|security.?code", lower_text):
            evidence.append({"type": "OTP_FIELD_PRESENT", "path": path, "match": value, "value": value})

    if re.search(r"otp.{0,30}(verified|success)|(?:verified|success).{0,30}otp", joined):
        otp_status = "OTP_VERIFIED"
    elif re.search(r"code.{0,30}(verified|success)|(?:verified|success).{0,30}code", joined):
        otp_status = "CODE_VERIFIED"
    elif evidence:
        otp_status = "OTP_FIELD_PRESENT"
    elif "delivered" in joined:
        otp_status = "UNKNOWN"
    else:
        otp_status = "UNKNOWN"

    delivery_status = "DELIVERED" if "delivered" in joined else "NOT_DELIVERED_OR_UNKNOWN"
    return {
        "otp_status": otp_status,
        "delivery_status": delivery_status,
        "evidence": evidence[:50],
    }
