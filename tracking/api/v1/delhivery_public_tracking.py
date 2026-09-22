import requests
from django.utils import timezone

from tracking.api.v1.delhivery_otp import (
    DELHIVERY_TRACK_URL,
    extract_tracking_summary,
    fetch_delhivery_tracking,
)
from tracking.api.v1.trackparcel import (
    calculate_next_check_after,
    get_or_create_snapshot,
    normalize_tracking_status,
    parse_date_value,
    parse_datetime_value,
    should_refresh_snapshot,
)
from tracking.models.tracking_snapshot import TrackingApiCallLog, TrackingSnapshot


PROVIDER = "delhivery_public"


def refresh_delhivery_public_snapshot(shipment, force=False, now=None):
    snapshot = get_or_create_snapshot(shipment)
    should_refresh, reason = should_refresh_snapshot(snapshot, force=force, now=now)
    if not should_refresh:
        return snapshot, False, reason

    response_data, call_log = call_delhivery_public_api(shipment)
    summary = extract_tracking_summary(response_data)
    status = summary.get("status") or summary.get("hq_status") or summary.get("delivery_pill_label") or ""
    normalized_status = normalize_tracking_status(status)
    expected_delivery_date = parse_date_value(
        summary.get("delivery_date_label") or summary.get("delivery_date")
    )
    delivered_at = parse_datetime_value(
        summary.get("delivery_date") if normalized_status == "DELIVERED" else None
    )

    snapshot.awb = shipment.awb
    snapshot.courier = shipment.courier or "Delhivery"
    snapshot.source = PROVIDER
    snapshot.current_status = status
    snapshot.normalized_status = normalized_status
    snapshot.expected_delivery_date = expected_delivery_date or snapshot.expected_delivery_date
    snapshot.delivered_at = delivered_at or snapshot.delivered_at
    snapshot.last_checked_at = timezone.now()
    snapshot.next_check_after = calculate_next_check_after(
        normalized_status,
        snapshot.expected_delivery_date,
        now=now,
    )
    snapshot.raw_response = {
        "summary": summary,
        "raw": response_data,
    }
    snapshot.error = call_log.error
    snapshot.updated_at = timezone.now()
    snapshot.save()

    if status:
        was_delivered = shipment.status.strip().lower() == "delivered"
        shipment.status = status
        shipment.save(update_fields=["status"])
        if normalized_status == "DELIVERED" and not was_delivered:
            from tracking.api.v1.track_shipments import notify_customer_delivered

            notify_customer_delivered(shipment.pa_order_id)

    return snapshot, True, "refreshed from Delhivery public tracking"


def call_delhivery_public_api(shipment):
    call_log = TrackingApiCallLog.objects.create(
        provider=PROVIDER,
        shipment=shipment,
        awb=shipment.awb,
        courier=shipment.courier or "Delhivery",
        endpoint=DELHIVERY_TRACK_URL,
        request_payload={"wbn": shipment.awb},
    )

    try:
        http_status, payload = fetch_delhivery_tracking(shipment.awb)
        call_log.http_status = http_status
        call_log.success = True
        call_log.response_payload = payload
        call_log.save()
        return payload, call_log
    except requests.RequestException as exc:
        call_log.error = str(exc)
        response = getattr(exc, "response", None)
        if response is not None:
            call_log.http_status = response.status_code
        call_log.save(update_fields=["error", "http_status"])
        raise


def serialize_delhivery_public_snapshot(shipment, snapshot, refreshed, reason):
    return {
        "awb": shipment.awb,
        "secureupi_order_id": shipment.pa_order_id,
        "courier": shipment.courier,
        "status": snapshot.current_status or shipment.status,
        "normalized_status": snapshot.normalized_status,
        "history": shipment.history if hasattr(shipment, "history") else [],
        "provider": PROVIDER,
        "refreshed": refreshed,
        "refresh_reason": reason,
        "last_checked_at": snapshot.last_checked_at.isoformat() if snapshot.last_checked_at else None,
        "next_check_after": snapshot.next_check_after.isoformat() if snapshot.next_check_after else None,
        "expected_delivery_date": snapshot.expected_delivery_date.isoformat() if snapshot.expected_delivery_date else None,
        "delivered_at": snapshot.delivered_at.isoformat() if snapshot.delivered_at else None,
        "tracking": snapshot.raw_response,
        "created_at": shipment.created_at.isoformat() if hasattr(shipment, "created_at") else None,
    }
