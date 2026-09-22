import hashlib
import json
import re

import requests


DELHIVERY_TRACK_URL = "https://dlv-api.delhivery.com/v3/unified-tracking-new"
DELHIVERY_TRACK_ORIGIN = "https://www.delhivery.com"
DELHIVERY_TRACK_REFERER = "https://www.delhivery.com/"

# Official Delhivery Shipment Tracking API reference only. This is account/token
# scoped and may not return third-party AWBs that are visible on public tracking.
# OFFICIAL_DELHIVERY_TRACK_URL = "https://track.delhivery.com/api/v1/packages/json/"
# OFFICIAL_DELHIVERY_TRACK_PARAMS = {"waybill": "<AWB>", "ref_ids": ""}
# OFFICIAL_DELHIVERY_TRACK_HEADER = {"Authorization": "Token <DELHIVERY_TOKEN>"}

OTP_VERIFIED_PATTERNS = [
    "otp verified delivery",
    "delivered to consignee - otp verified delivery",
    "eod-135",
    "eod-135_dl",
    "eod-141",
    "eod-141_dl",
    "out for delivery with otp",
]

CODE_VERIFIED_PATTERNS = [
    "code verified delivery",
    "delivered to consignee - code verified delivery",
    "package delivered successfully after verification",
]

NON_OTP_PATTERNS = [
    "eod-136",
    "eod-136_dl",
    "delivered without verification",
    "delivered without otp",
]

DELIVERED_PATTERNS = [
    "delivered",
    "delivered to consignee",
]


def normalize_awb(awb):
    return re.sub(r"[^A-Za-z0-9_-]", "", (awb or "").strip())


def stable_json_hash(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fetch_delhivery_tracking(awb):
    response = requests.get(
        DELHIVERY_TRACK_URL,
        params={"wbn": awb},
        headers={
            "Accept": "application/json",
            "Origin": DELHIVERY_TRACK_ORIGIN,
            "Referer": DELHIVERY_TRACK_REFERER,
            "User-Agent": "SecurePay/1.0 Delhivery OTP Verification",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.status_code, response.json()


def extract_tracking_summary(payload):
    official_summary = _extract_official_tracking_summary(payload)
    if official_summary:
        return official_summary

    return _extract_public_tracking_summary(payload)


def _extract_public_tracking_summary(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    entry = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
    scans = []

    for state in entry.get("trackingStates") or []:
        if not isinstance(state, dict):
            continue
        for scan in state.get("scans") or []:
            if not isinstance(scan, dict):
                continue
            scans.append(
                {
                    "state_label": state.get("label") or "",
                    "scan": scan.get("scan") or "",
                    "scan_type": scan.get("scanType") or "",
                    "scan_nsl_remark": scan.get("scanNslRemark") or "",
                    "city_location": scan.get("cityLocation") or "",
                    "scanned_location": scan.get("scannedLocation") or "",
                    "scan_date_time": scan.get("scanDateTime") or scan.get("scanDate") or "",
                }
            )

    return {
        "message": payload.get("message") if isinstance(payload, dict) else "",
        "carrier_status_code": payload.get("statusCode") if isinstance(payload, dict) else None,
        "awb": entry.get("awb") or "",
        "current_flow": entry.get("currentFlow") or "",
        "current_track_index": entry.get("currentTrackIndex"),
        "delivery_date": entry.get("deliveryDate") or "",
        "delivery_date_label": entry.get("deliveryDate_v1") or "",
        "delivery_pill_label": entry.get("deliveryPillLabel") or "",
        "hq_status": entry.get("hqStatus") or "",
        "product_type": entry.get("productType") or "",
        "reference_no": entry.get("referenceNo") or "",
        "status": status.get("status") or "",
        "status_type": status.get("statusType") or "",
        "status_datetime": status.get("statusDateTime") or "",
        "instructions": status.get("instructions") or "",
        "scan_count": len(scans),
        "latest_scan": scans[0] if scans else None,
        "scans": scans[:25],
    }


def _extract_official_tracking_summary(payload):
    shipment_data = payload.get("ShipmentData") if isinstance(payload, dict) else None
    if not isinstance(shipment_data, list) or not shipment_data:
        return None

    shipment_wrapper = shipment_data[0] if isinstance(shipment_data[0], dict) else {}
    shipment = shipment_wrapper.get("Shipment") if isinstance(shipment_wrapper.get("Shipment"), dict) else shipment_wrapper
    status = shipment.get("Status") if isinstance(shipment.get("Status"), dict) else {}
    scans = []

    for scan_wrapper in shipment.get("Scans") or []:
        scan = scan_wrapper.get("ScanDetail") if isinstance(scan_wrapper, dict) else None
        if not isinstance(scan, dict):
            scan = scan_wrapper if isinstance(scan_wrapper, dict) else {}

        scan_text = scan.get("Scan") or scan.get("Status") or ""
        scan_remark = scan.get("Instructions") or scan.get("ScanNslRemark") or scan.get("scanNslRemark") or ""
        scans.append(
            {
                "state_label": scan_text,
                "scan": scan_text,
                "scan_type": scan.get("ScanType") or scan.get("StatusType") or "",
                "scan_nsl_remark": scan_remark,
                "city_location": scan.get("CityLocation") or scan.get("City") or "",
                "scanned_location": scan.get("ScannedLocation") or scan.get("Location") or "",
                "scan_date_time": scan.get("ScanDateTime") or scan.get("ScanDate") or "",
            }
        )

    return {
        "message": payload.get("message") or payload.get("Message") or "",
        "carrier_status_code": payload.get("statusCode") or payload.get("StatusCode"),
        "awb": shipment.get("AWB") or shipment.get("Waybill") or shipment.get("waybill") or "",
        "current_flow": shipment.get("CurrentFlow") or shipment.get("ShipmentType") or "",
        "current_track_index": None,
        "delivery_date": shipment.get("DeliveredDate") or shipment.get("DeliveryDate") or "",
        "delivery_date_label": shipment.get("ExpectedDeliveryDate") or shipment.get("DeliveredDate") or "",
        "delivery_pill_label": "",
        "hq_status": status.get("Status") or shipment.get("Status") or "",
        "product_type": shipment.get("ProductType") or shipment.get("Product") or "",
        "reference_no": shipment.get("ReferenceNo") or shipment.get("OrderId") or shipment.get("RefNo") or "",
        "status": status.get("Status") or "",
        "status_type": status.get("StatusType") or "",
        "status_datetime": status.get("StatusDateTime") or "",
        "instructions": status.get("Instructions") or "",
        "scan_count": len(scans),
        "latest_scan": scans[0] if scans else None,
        "scans": scans[:25],
    }


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


def parse_delhivery_otp_status(payload):
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
        return {
            "otp_status": "OTP_VERIFIED",
            "delivery_status": "DELIVERED",
            "evidence": otp_evidence[:10],
        }

    if code_evidence:
        return {
            "otp_status": "CODE_VERIFIED",
            "delivery_status": "DELIVERED",
            "evidence": code_evidence[:10],
        }

    if non_otp_evidence:
        return {
            "otp_status": "NON_OTP_DELIVERED",
            "delivery_status": "DELIVERED",
            "evidence": non_otp_evidence[:10],
        }

    if delivered_evidence:
        return {
            "otp_status": "UNKNOWN",
            "delivery_status": "DELIVERED",
            "evidence": delivered_evidence[:10],
        }

    return {
        "otp_status": "UNKNOWN",
        "delivery_status": "NOT_DELIVERED_OR_UNKNOWN",
        "evidence": [],
    }
