import re
from html.parser import HTMLParser
from urllib.parse import quote

import requests


DTDC_TRACK_URL = "https://www.dtdc.in/tracking/tracking_results.asp?Ession=0&strCnno={awb}"


def normalize_awb(awb):
    return re.sub(r"[^A-Za-z0-9_-]", "", (awb or "").strip())


class _DtdcHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.rows = []
        self._row = None
        self._cell = None
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._ignored_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        self.parts.append(text)
        if self._cell is not None:
            self._cell.append(text)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag in {"td", "th"} and self._cell is not None:
            self._row.append(" ".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            row = [value for value in self._row if value]
            if row:
                self.rows.append(row)
            self._row = None


def _scalar_fields(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _scalar_fields(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _scalar_fields(child, f"{path}[{index}]")
    elif value is not None:
        yield path, str(value)


def fetch_dtdc_tracking(awb):
    response = requests.get(
        DTDC_TRACK_URL.format(awb=quote(awb, safe="")),
        headers={
            "Accept": "text/html,application/json",
            "User-Agent": "SecurePay/1.0 DTDC OTP Verification",
        },
        timeout=20,
    )
    response.raise_for_status()
    if "json" in response.headers.get("Content-Type", "").lower():
        payload = response.json()
    else:
        parser = _DtdcHtmlParser()
        parser.feed(response.text)
        payload = {
            "source_url": response.url,
            "content_type": response.headers.get("Content-Type", ""),
            "page_text": " ".join(parser.parts),
            "table_rows": parser.rows,
            "html": response.text,
        }
    return response.status_code, payload


def _field_values(payload):
    return list(_scalar_fields(payload))


def _labelled_fields(payload):
    labelled = []
    rows = payload.get("table_rows", []) if isinstance(payload, dict) else []
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) < 2:
            continue
        for column in range(0, len(row) - 1, 2):
            label = str(row[column]).strip(" :")
            value = str(row[column + 1]).strip()
            if label and value and len(value) <= 300:
                labelled.append((f"table_rows[{index}].{label}", value))

    page_text = payload.get("page_text", "") if isinstance(payload, dict) else ""
    for label in (
        "Status", "Origin", "Destination", "Reference No", "Reference Number",
        "Pickup Date", "Delivery Date", "Booking Date", "Last Location",
        "Consignee", "Product Type", "Weight", "Pieces",
    ):
        match = re.search(rf"{re.escape(label)}\s*[:\-]\s*([^|\n]{1,120})", page_text, re.I)
        if match:
            labelled.append((f"page.{label}", match.group(1).strip()))
    return labelled


def extract_dtdc_tracking_summary(payload):
    if isinstance(payload, dict) and payload.get("html"):
        html_parser = _DtdcHtmlParser()
        html_parser.feed(str(payload["html"]))
        payload = {
            **payload,
            "page_text": " ".join(html_parser.parts),
            "table_rows": html_parser.rows,
        }
    raw_fields = _field_values(payload)
    raw_fields.extend(_labelled_fields(payload))
    fields = [{"path": path, "value": value} for path, value in raw_fields]
    status_fields = [
        field for field in fields
        if not re.search(r"(?:^|\.)(html|source_url|content_type)$", field["path"], re.I)
    ]
    meaningful_fields = []
    seen = set()
    for field in fields:
        key = (field["path"].lower(), field["value"].lower())
        if (
            field["value"].strip()
            and key not in seen
            and not re.search(r"^(html|page_text|source_url|content_type)$", field["path"], re.I)
            and len(field["value"]) <= 300
        ):
            meaningful_fields.append(field)
            seen.add(key)
    joined = " ".join(field["value"] for field in status_fields)
    status_pattern = (
        r"delivered|out for delivery|in transit|rto|booked|pending|"
        r"shipment picked up|not delivered"
    )

    def clean_status(value):
        if re.fullmatch(r"(?:courier\s+)?status", str(value).strip(), re.I):
            return ""
        match = re.search(status_pattern, str(value), re.I)
        return match.group(0).upper() if match else ""

    otp_fields = [
        field for field in fields
        if re.search(r"otp|verification|verified|delivery.?code|security.?code", f"{field['path']} {field['value']}", re.I)
    ]
    status = next(
        (
            clean_status(field["value"])
            for field in status_fields
            if len(field["value"]) <= 120 and re.search(r"status", field["path"], re.I)
            and clean_status(field["value"])
        ),
        "",
    )
    if not status:
        status = next(
            (
                clean_status(field["value"])
                for field in status_fields
                if len(field["value"]) <= 120 and clean_status(field["value"])
            ),
            "",
        )
    if not status:
        status_match = re.search(
            status_pattern,
            joined,
            re.I,
        )
        status = status_match.group(0).upper() if status_match else ""

    status_time = next(
        (
            field["value"]
            for field in fields
            if re.search(r"date|time|updated|timestamp", field["path"], re.I)
            and len(field["value"]) <= 80
        ),
        "",
    )
    if not status_time:
        date_time_pattern = re.compile(
            r"(?:\d{1,4}[-/]\d{1,2}[-/]\d{1,4}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})"
            r"(?:[ ,T]+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)?",
            re.I,
        )
        status_time = next(
            (
                field["value"]
                for field in fields
                if len(field["value"]) <= 80 and date_time_pattern.search(field["value"])
            ),
            "",
        )
    return {
        "status": status,
        "courier_status": status or "Unknown",
        "status_time": status_time,
        "delivery_status": status or "NOT_DELIVERED_OR_UNKNOWN",
        "otp_fields": otp_fields[:50],
        "meaningful_fields": meaningful_fields[:40],
        "fields": fields[:300],
    }


def parse_dtdc_otp_status(payload):
    fields = _field_values(payload)
    evidence = []
    joined = " ".join(f"{path} {value}" for path, value in fields)
    tracking_summary = extract_dtdc_tracking_summary(payload)
    for path, value in fields:
        lower_text = f"{path} {value}".lower()
        if re.search(r"otp.{0,30}(verified|success)|(?:verified|success).{0,30}otp", lower_text):
            evidence.append({"type": "OTP_VERIFIED", "path": path, "match": value, "value": value})
        elif re.search(r"code.{0,30}(verified|success)|(?:verified|success).{0,30}code", lower_text):
            evidence.append({"type": "CODE_VERIFIED", "path": path, "match": value, "value": value})
        elif re.search(r"otp|verification|delivery.?code|security.?code", lower_text):
            evidence.append({"type": "OTP_FIELD_PRESENT", "path": path, "match": value, "value": value})

    lower_joined = joined.lower()
    if re.search(r"otp.{0,30}(verified|success)|(?:verified|success).{0,30}otp", lower_joined):
        otp_status = "OTP_VERIFIED"
    elif re.search(r"code.{0,30}(verified|success)|(?:verified|success).{0,30}code", lower_joined):
        otp_status = "CODE_VERIFIED"
    elif evidence:
        otp_status = "OTP_FIELD_PRESENT"
    else:
        otp_status = "UNKNOWN"

    return {
        "otp_status": otp_status,
        "delivery_status": tracking_summary["delivery_status"],
        "evidence": evidence[:50],
    }
