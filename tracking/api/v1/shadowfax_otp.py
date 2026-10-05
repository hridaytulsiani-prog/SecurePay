import re

import requests


SHADOWFAX_TRACK_URL = "https://saruman.shadowfax.in/web_app/delivery/track/{awb}/"
# This is the public token embedded in Shadowfax's own consumer tracking page.
SHADOWFAX_PUBLIC_TOKEN = "cePcVR7z7FIETB4PxguHC2YJGk6NncHnByrJttgRIUqNxfWezuzAUvtALyqcHJEC"


def normalize_awb(awb):
    return re.sub(r"[^A-Za-z0-9_-]", "", (awb or "").strip())


def fetch_shadowfax_tracking(awb):
    response = requests.get(
        SHADOWFAX_TRACK_URL.format(awb=awb),
        headers={
            "Accept": "application/json",
            "Authorization": f"Token {SHADOWFAX_PUBLIC_TOKEN}",
            "User-Agent": "Mozilla/5.0 SecurePay Shadowfax Public Tracking",
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


def extract_shadowfax_tracking_summary(payload):
    fields = [{"path": path, "value": value} for path, value in _walk_values(payload)]
    status = _find_value(payload, ("status", "current_status", "shipment_status", "status_text", "message"))
    status_time = _find_value(payload, ("updated_at", "last_updated", "timestamp", "status_time", "time", "date"))
    delivery_status = _find_value(payload, ("delivery_status", "shipment_status", "status", "current_status")) or status
    otp_fields = [
        field for field in fields
        if re.search(r"otp|verification|verified|delivery.?code|security.?code", f"{field['path']} {field['value']}", re.I)
    ]
    meaningful_fields = [
        field for field in fields
        if field["value"].strip() and len(field["value"]) <= 300
        and not re.search(r"token|authorization", field["path"], re.I)
    ]
    return {
        "courier_status": status or "Unknown",
        "status": status,
        "status_time": status_time,
        "delivery_status": delivery_status,
        "otp_fields": otp_fields[:50],
        "meaningful_fields": meaningful_fields[:100],
        "fields": fields[:300],
    }


def parse_shadowfax_otp_status(payload):
    fields = list(_walk_values(payload))
    evidence = []
    joined = " ".join(f"{path} {value}" for path, value in fields).lower()
    for path, value in fields:
        lower_text = f"{path} {value}".lower()
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
    else:
        otp_status = "UNKNOWN"

    return {
        "otp_status": otp_status,
        "delivery_status": "DELIVERED" if "delivered" in joined else "NOT_DELIVERED_OR_UNKNOWN",
        "evidence": evidence[:50],
    }
