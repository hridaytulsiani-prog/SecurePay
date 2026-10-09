import hashlib
import json
import re
from html import unescape
from html.parser import HTMLParser

import requests


BLUEDART_TRACK_URL = "https://www.bluedart.com/web/guest/trackdartresultthirdparty"
BLUEDART_TRACK_REFERER = "https://www.bluedart.com/"

OTP_VERIFIED_PATTERNS = [
    "otp verified",
    "otp based delivery verified",
    "delivery verified by otp",
    "lastmiledynamicotp",
    "lastmilefixedotp",
    "otpbaseddelivery",
]

CODE_VERIFIED_PATTERNS = [
    "code verified",
    "verified by code",
]

NON_OTP_PATTERNS = [
    "delivered without otp",
    "non otp delivered",
    "no otp",
]

DELIVERED_PATTERNS = [
    "shipment delivered",
    "delivered",
]


def normalize_awb(awb):
    return re.sub(r"[^A-Za-z0-9_-]", "", (awb or "").strip())


def stable_json_hash(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fetch_bluedart_tracking(awb):
    response = requests.get(
        BLUEDART_TRACK_URL,
        params={"trackFor": "0", "trackNo": awb},
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": BLUEDART_TRACK_REFERER,
            "User-Agent": "SecurePay/1.0 BlueDart OTP Verification",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.status_code, normalize_bluedart_tracking_page(awb, response.url, response.text)


def normalize_bluedart_tracking_page(awb, url, html):
    text = _html_to_text(html)
    fields = _extract_fields(text)
    scans = _extract_scans(text)

    return {
        "source": "bluedart_public_tracking",
        "url": url,
        "searched_awb": awb,
        "fields": fields,
        "scans": scans,
        "text": text[:20000],
    }


def extract_tracking_summary(payload):
    fields = payload.get("fields") if isinstance(payload, dict) else {}
    scans = payload.get("scans") if isinstance(payload, dict) else []
    fields = fields if isinstance(fields, dict) else {}
    scans = scans if isinstance(scans, list) else []

    status = fields.get("Status") or ""
    return {
        "message": _not_found_message(payload),
        "carrier_status_code": None,
        "awb": fields.get("Waybill No") or payload.get("searched_awb") or "",
        "current_flow": "",
        "current_track_index": None,
        "delivery_date": fields.get("Date of Delivery") or "",
        "delivery_date_label": fields.get("Date of Delivery") or "",
        "delivery_pill_label": status,
        "hq_status": status,
        "product_type": "",
        "reference_no": fields.get("Reference No") or "",
        "status": status,
        "status_type": "",
        "status_datetime": _combine_date_time(fields.get("Date of Delivery"), fields.get("Time of Delivery")),
        "instructions": "",
        "scan_count": len(scans),
        "latest_scan": scans[0] if scans else None,
        "scans": scans[:25],
        "extra_fields": {
            "pickup_date": fields.get("Pickup Date") or "",
            "from": fields.get("From") or "",
            "to": fields.get("To") or "",
            "recipient": fields.get("Recipient") or "",
            "public_endpoint": payload.get("url") or "",
        },
    }


def parse_bluedart_otp_status(payload):
    evidence = []
    delivered_evidence = []

    for path, text in _walk_values(payload):
        otp_match = _first_pattern_match(text, OTP_VERIFIED_PATTERNS)
        if otp_match:
            evidence.append({"type": "OTP_VERIFIED", "path": path, "match": otp_match, "value": text})

        code_match = _first_pattern_match(text, CODE_VERIFIED_PATTERNS)
        if code_match:
            evidence.append({"type": "CODE_VERIFIED", "path": path, "match": code_match, "value": text})

        non_otp_match = _first_pattern_match(text, NON_OTP_PATTERNS)
        if non_otp_match:
            evidence.append({"type": "NON_OTP_DELIVERED", "path": path, "match": non_otp_match, "value": text})

        delivered_match = _first_pattern_match(text, DELIVERED_PATTERNS)
        if delivered_match:
            delivered_evidence.append({"type": "DELIVERED", "path": path, "match": delivered_match, "value": text})

    otp_evidence = [item for item in evidence if item["type"] == "OTP_VERIFIED"]
    code_evidence = [item for item in evidence if item["type"] == "CODE_VERIFIED"]
    non_otp_evidence = [item for item in evidence if item["type"] == "NON_OTP_DELIVERED"]

    if otp_evidence:
        return {"otp_status": "OTP_VERIFIED", "delivery_status": "DELIVERED_OR_OTP_INDICATED", "evidence": otp_evidence[:10]}

    if code_evidence:
        return {"otp_status": "CODE_VERIFIED", "delivery_status": "DELIVERED", "evidence": code_evidence[:10]}

    if non_otp_evidence:
        return {"otp_status": "NON_OTP_DELIVERED", "delivery_status": "DELIVERED", "evidence": non_otp_evidence[:10]}

    if delivered_evidence:
        return {"otp_status": "UNKNOWN", "delivery_status": "DELIVERED", "evidence": delivered_evidence[:10]}

    return {"otp_status": "UNKNOWN", "delivery_status": "NOT_DELIVERED_OR_UNKNOWN", "evidence": []}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        text = data.strip()
        if text:
            self.parts.append(text)


def _html_to_text(html):
    parser = _TextExtractor()
    parser.feed(html or "")
    text = "\n".join(parser.parts)
    text = unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _extract_fields(text):
    known_labels = [
        "Waybill No",
        "Pickup Date",
        "From",
        "To",
        "Status",
        "Date of Delivery",
        "Time of Delivery",
        "Recipient",
        "Reference No",
    ]
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    fields = {}

    for index, line in enumerate(lines):
        normalized_line = line.rstrip(":")
        if normalized_line in known_labels and index + 1 < len(lines):
            if normalized_line in fields:
                continue
            value = lines[index + 1].strip()
            if value and value not in known_labels:
                fields[normalized_line] = value.replace(" Image", "").strip()

    for label in known_labels:
        if label in fields:
            continue
        match = re.search(rf"{re.escape(label)}\s*\|\s*([^\n|]+)", text or "", re.IGNORECASE)
        if match:
            fields[label] = match.group(1).strip()

    return fields


def _extract_scans(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    scans = []
    start = next((index for index, line in enumerate(lines) if line.lower() == "status and scans"), -1)
    if start < 0:
        return scans

    date_pattern = re.compile(r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}")
    time_pattern = re.compile(r"^\d{2}:\d{2}$")

    index = start + 1
    while index + 3 < len(lines):
        location, details, date, time = lines[index : index + 4]
        if date_pattern.search(date) and time_pattern.match(time):
            scans.append(
                {
                    "state_label": details,
                    "scan": details,
                    "scan_type": "",
                    "scan_nsl_remark": details,
                    "city_location": location,
                    "scanned_location": location,
                    "scan_date_time": f"{date} {time}",
                }
            )
            index += 4
            continue
        index += 1

    return scans


def _combine_date_time(date, time):
    if date and time:
        return f"{date} {time}"
    return date or time or ""


def _not_found_message(payload):
    text = payload.get("text") if isinstance(payload, dict) else ""
    if "Records Not Found" in text or "Invalid Query Number" in text:
        return "Records Not Found"
    return ""


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


def _first_pattern_match(text, patterns):
    lower_text = text.lower()
    for pattern in patterns:
        if pattern in lower_text:
            return pattern
    return None
