import json
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote

import requests


SHIPROCKET_TRACK_URL = "https://shiprocket.co/tracking/{awb}"


def normalize_awb(awb):
    return re.sub(r"[^A-Za-z0-9_-]", "", (awb or "").strip())


class _ShiprocketHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        text = " ".join(data.split())
        if text:
            self.parts.append(text)


def fetch_shiprocket_tracking(awb):
    response = requests.get(
        SHIPROCKET_TRACK_URL.format(awb=quote(awb, safe="")),
        headers={
            "Accept": "text/html,application/json",
            "User-Agent": "SecurePay/1.0 Shiprocket Public Tracking",
        },
        timeout=20,
    )
    response.raise_for_status()
    if "json" in response.headers.get("Content-Type", "").lower():
        payload = response.json()
    else:
        parser = _ShiprocketHtmlParser()
        parser.feed(response.text)
        embedded_data = []
        for match in re.findall(r"<script[^>]*>(.*?)</script>", response.text, re.I | re.S):
            candidate = match.strip()
            if candidate.startswith(("{", "[")):
                try:
                    embedded_data.append(json.loads(candidate))
                except ValueError:
                    continue
        payload = {
            "source_url": response.url,
            "content_type": response.headers.get("Content-Type", ""),
            "page_text": " ".join(parser.parts),
            "embedded_data": embedded_data,
            "html": response.text,
        }
    return response.status_code, payload


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


def _usable_fields(payload):
    fields = [
        (path, value) for path, value in _walk_values(payload)
        if not re.search(r"^(page_text|html|source_url|content_type)$", path, re.I)
    ]
    html = payload.get("html", "") if isinstance(payload, dict) else ""
    if html:
        def clean(value):
            return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value))).strip()

        status_match = re.search(r"id=['\"]shipment_status['\"][^>]*>(.*?)</p>", html, re.I | re.S)
        if status_match:
            status = clean(status_match.group(1))
            if status:
                fields.append(("public.shipment_status", status))

        date_parts = []
        for field_id in ("edd_day", "edd_month", "edd_txt"):
            match = re.search(rf"id=['\"]{field_id}['\"][^>]*>(.*?)</", html, re.I | re.S)
            if match:
                date_parts.append(clean(match.group(1)))
        if date_parts:
            fields.append(("public.delivered_on", " ".join(date_parts)))

        for index, match in enumerate(re.finditer(r"<activity>(.*?)</activity>", html, re.I | re.S)):
            activity = clean(match.group(1))
            if activity:
                fields.append((f"public.activity[{index}]", activity))
    return fields


def extract_shiprocket_tracking_summary(payload):
    usable_fields = _usable_fields(payload)
    fields = [{"path": path, "value": value} for path, value in usable_fields]
    status = next(
        (value for path, value in usable_fields if path.rsplit(".", 1)[-1].split("[", 1)[0].lower() in {
            "current_status", "shipment_status", "status", "status_value"
        }),
        "",
    )
    status = status or next((value for path, value in usable_fields if path == "public.shipment_status"), "")
    if not status:
        page_text = _find_value(payload, ("page_text",))
        awb = _find_value(payload, ("awb_code", "awb", "tracking_number"))
        status_match = re.search(
            r"delivered|out for delivery|in transit|picked up|order received|reached destination|rto|not found",
            page_text,
            re.I,
        ) if awb and awb in page_text else None
        status = status_match.group(0).upper() if status_match else "Unknown"
    delivery_status = next(
        (value for path, value in usable_fields if path.rsplit(".", 1)[-1].split("[", 1)[0].lower() in {
            "delivery_status", "current_status", "status"
        }),
        status,
    )
    status_time = next(
        (value for path, value in usable_fields if path.rsplit(".", 1)[-1].split("[", 1)[0].lower() in {
            "delivered_date", "pickup_date", "updated_at", "date"
        }),
        next((value for path, value in usable_fields if path == "public.delivered_on"), ""),
    )
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
        "meaningful_fields": meaningful_fields[:80],
        "fields": fields[:300],
    }


def parse_shiprocket_otp_status(payload):
    fields = _usable_fields(payload)
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
