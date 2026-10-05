from datetime import timedelta

from django.utils import timezone

from tracking.api.v1.delhivery_otp import (
    extract_tracking_summary as extract_delhivery_summary,
    fetch_delhivery_tracking,
    parse_delhivery_otp_status,
    normalize_awb,
)
from tracking.api.v1.dtdc_otp import (
    extract_dtdc_tracking_summary,
    fetch_dtdc_tracking,
    parse_dtdc_otp_status,
)
from tracking.api.v1.ekart_otp import (
    extract_ekart_tracking_summary,
    fetch_ekart_tracking,
    parse_ekart_otp_status,
)
from tracking.api.v1.shadowfax_otp import (
    extract_shadowfax_tracking_summary,
    fetch_shadowfax_tracking,
    parse_shadowfax_otp_status,
)
from tracking.api.v1.shiprocket_otp import (
    extract_shiprocket_tracking_summary,
    fetch_shiprocket_tracking,
    parse_shiprocket_otp_status,
)
from tracking.api.v1.trackparcel import (
    calculate_next_check_after,
    get_or_create_snapshot,
    normalize_tracking_status,
    parse_date_value,
    should_refresh_snapshot,
)
from tracking.models.delhivery_otp_check import DelhiveryOtpCheck
from tracking.models.trackinginfo import Shipment


COURIER_ALIASES = {
    "delhivery": ("delhivery",),
    "dtdc": ("dtdc",),
    "shiprocket": ("shiprocket",),
    "ekart": ("ekart",),
    "shadowfax": ("shadowfax",),
}

HANDLERS = {
    "delhivery": (fetch_delhivery_tracking, extract_delhivery_summary, parse_delhivery_otp_status),
    "dtdc": (fetch_dtdc_tracking, extract_dtdc_tracking_summary, parse_dtdc_otp_status),
    "shiprocket": (fetch_shiprocket_tracking, extract_shiprocket_tracking_summary, parse_shiprocket_otp_status),
    "ekart": (fetch_ekart_tracking, extract_ekart_tracking_summary, parse_ekart_otp_status),
    "shadowfax": (fetch_shadowfax_tracking, extract_shadowfax_tracking_summary, parse_shadowfax_otp_status),
}


def _provider_rate_limited(courier):
    cutoff = timezone.now() - timedelta(minutes=1)
    return DelhiveryOtpCheck.objects.filter(courier=courier, checked_at__gte=cutoff).count() >= 5


def normalize_courier(courier):
    value = (courier or "").strip().lower()
    for key, aliases in COURIER_ALIASES.items():
        if any(alias in value for alias in aliases):
            return key
    return None


def courier_matches(courier, provider):
    return provider == "all" or normalize_courier(courier) == provider


def _summary_status(summary, parsed):
    return (
        summary.get("status")
        or summary.get("courier_status")
        or parsed.get("delivery_status")
        or ""
    )


def _summary_expected_date(summary):
    for key in ("expected_delivery_date", "delivery_date_label", "delivery_date", "delivered_on"):
        parsed = parse_date_value(summary.get(key))
        if parsed:
            return parsed

    for field in summary.get("meaningful_fields", []):
        path = str(field.get("path", ""))
        if "delivery" in path.lower() or "edd" in path.lower():
            parsed = parse_date_value(field.get("value"))
            if parsed:
                return parsed
    return None


def update_snapshot_from_tracking(shipment, courier, payload, summary, parsed, now=None, create_check=False):
    now = now or timezone.localtime()
    snapshot = get_or_create_snapshot(shipment)
    status = _summary_status(summary, parsed)
    normalized_status = normalize_tracking_status(status)
    expected_delivery_date = _summary_expected_date(summary)

    snapshot.awb = shipment.awb
    snapshot.courier = shipment.courier or courier
    snapshot.source = f"{courier}_public"[:32]
    snapshot.current_status = status
    snapshot.normalized_status = normalized_status
    snapshot.expected_delivery_date = expected_delivery_date or snapshot.expected_delivery_date
    snapshot.last_checked_at = timezone.now()
    snapshot.next_check_after = calculate_next_check_after(
        normalized_status,
        snapshot.expected_delivery_date,
        now=now,
    )
    snapshot.raw_response = payload
    snapshot.error = ""
    snapshot.updated_at = timezone.now()
    snapshot.save()

    if status:
        shipment.status = status
        shipment.save(update_fields=["status"])

    if create_check:
        DelhiveryOtpCheck.objects.create(
            awb=shipment.awb,
            courier=courier,
            otp_status=parsed["otp_status"],
            delivery_status=parsed["delivery_status"],
            evidence=parsed["evidence"],
            raw_response=payload,
            http_status=200,
        )
    return snapshot


def refresh_courier_snapshot(shipment, provider=None, now=None):
    courier = provider or normalize_courier(shipment.courier)
    if courier not in HANDLERS:
        return None, False, "unsupported courier"

    snapshot = get_or_create_snapshot(shipment)
    should_refresh, reason = should_refresh_snapshot(snapshot, now=now)
    if not should_refresh:
        return snapshot, False, reason
    if _provider_rate_limited(courier):
        return snapshot, False, "provider rate limit reached; retry on the next scheduler run"

    fetch, extract, parse = HANDLERS[courier]
    http_status, payload = fetch(shipment.awb)
    parsed = parse(payload)
    summary = extract(payload)
    update_snapshot_from_tracking(
        shipment,
        courier,
        payload,
        summary,
        parsed,
        now=now,
        create_check=True,
    )
    return snapshot, True, f"refreshed {courier} public tracking ({http_status})"


def sync_manual_check_to_snapshot(awb, courier, payload, summary, parsed):
    shipment = Shipment.objects.filter(awb=normalize_awb(awb)).first()
    if not shipment:
        return None
    return update_snapshot_from_tracking(shipment, courier, payload, summary, parsed)
