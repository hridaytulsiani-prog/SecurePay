from datetime import date, datetime, time, timedelta

import requests
from django.conf import settings
from django.utils import timezone

from tracking.models.tracking_snapshot import TrackingApiCallLog, TrackingSnapshot


FINAL_STATUSES = {"DELIVERED", "CANCELLED", "RTO_DELIVERED", "FAILED_FINAL"}
DELIVERY_WINDOW_START_HOUR = 9
DELIVERY_WINDOW_END_HOUR = 18
DELIVERY_DAY_REFRESH_INTERVAL = timedelta(hours=1)


def get_or_create_snapshot(shipment):
    snapshot, _ = TrackingSnapshot.objects.get_or_create(
        shipment=shipment,
        defaults={
            "awb": shipment.awb,
            "courier": shipment.courier,
            "source": TrackingSnapshot.SOURCE_TRACKPARCEL,
            "current_status": shipment.status,
            "normalized_status": normalize_tracking_status(shipment.status),
        },
    )
    return snapshot


def should_refresh_snapshot(snapshot, force=False, now=None):
    if force:
        return True, "force refresh requested"

    now = now or timezone.localtime()
    status = normalize_tracking_status(snapshot.normalized_status or snapshot.current_status)

    if status in FINAL_STATUSES:
        return False, "final shipment status"

    expected_date = snapshot.expected_delivery_date
    if expected_date and now.date() == expected_date and not _inside_delivery_window(now):
        return False, "outside delivery window"

    if expected_date and now.date() < expected_date:
        last_checked_date = (
            timezone.localtime(snapshot.last_checked_at).date()
            if snapshot.last_checked_at
            else None
        )
        if last_checked_date == now.date():
            return False, "already checked today before delivery date"
        return True, "daily pre-delivery check due"

    if not expected_date and snapshot.last_checked_at:
        if timezone.localtime(snapshot.last_checked_at).date() == now.date():
            return False, "already checked today while delivery date is unknown"
        return True, "daily tracking check due"

    if not snapshot.last_checked_at:
        return True, "never checked"

    if snapshot.next_check_after and now < timezone.localtime(snapshot.next_check_after):
        return False, "next check time not reached"

    return True, "stale tracking data"


def refresh_trackparcel_snapshot(shipment, force=False, now=None):
    snapshot = get_or_create_snapshot(shipment)
    should_refresh, reason = should_refresh_snapshot(snapshot, force=force, now=now)
    if not should_refresh:
        return snapshot, False, reason

    response_data, call_log = call_trackparcel_api(shipment)
    parsed = parse_trackparcel_response(response_data)
    normalized_status = normalize_tracking_status(parsed["status"])

    snapshot.awb = shipment.awb
    snapshot.courier = shipment.courier
    snapshot.source = TrackingSnapshot.SOURCE_TRACKPARCEL
    snapshot.current_status = parsed["status"]
    snapshot.normalized_status = normalized_status
    snapshot.expected_delivery_date = parsed["expected_delivery_date"] or snapshot.expected_delivery_date
    snapshot.delivered_at = parsed["delivered_at"] or snapshot.delivered_at
    snapshot.last_checked_at = timezone.now()
    snapshot.next_check_after = calculate_next_check_after(
        normalized_status,
        snapshot.expected_delivery_date,
        now=now,
    )
    snapshot.raw_response = response_data
    snapshot.error = call_log.error
    snapshot.updated_at = timezone.now()
    snapshot.save()

    if parsed["status"]:
        was_delivered = shipment.status.strip().lower() == "delivered"
        shipment.status = parsed["status"]
        shipment.save(update_fields=["status"])
        if normalized_status == "DELIVERED" and not was_delivered:
            from tracking.api.v1.track_shipments import notify_customer_delivered

            notify_customer_delivered(shipment.pa_order_id)

    return snapshot, True, "refreshed from TrackParcel"


def call_trackparcel_api(shipment):
    base_url = getattr(settings, "TRACKPARCEL_BASE_URL", "").rstrip("/")
    api_key = getattr(settings, "TRACKPARCEL_API_KEY", "")
    endpoint = getattr(settings, "TRACKPARCEL_TRACK_ENDPOINT", "/api/v1/track")

    if not base_url:
        raise RuntimeError("TRACKPARCEL_BASE_URL is not configured")
    if not api_key:
        raise RuntimeError("TRACKPARCEL_API_KEY is not configured")

    url = f"{base_url}{endpoint}"
    payload = build_trackparcel_payload(shipment)
    headers = {
        "Accept": "application/json",
        "x-api-key": api_key,
    }

    call_log = TrackingApiCallLog.objects.create(
        provider=TrackingApiCallLog.PROVIDER_TRACKPARCEL,
        shipment=shipment,
        awb=shipment.awb,
        courier=shipment.courier,
        endpoint=url,
        request_payload=payload,
    )

    try:
        response = requests.get(url, params=payload, headers=headers, timeout=20)

        response_payload = response.json()
        call_log.http_status = response.status_code
        call_log.success = 200 <= response.status_code < 300
        call_log.response_payload = response_payload
        call_log.response_headers = extract_quota_headers(response.headers)
        if not call_log.success:
            call_log.error = f"TrackParcel returned HTTP {response.status_code}"
        call_log.save()
        response.raise_for_status()
        return response_payload, call_log
    except Exception as exc:
        call_log.error = str(exc)
        call_log.save(update_fields=["error"])
        raise


def build_trackparcel_payload(shipment):
    courier = str(shipment.courier or '').strip()
    normalized_courier = ''.join(character for character in courier.lower() if character.isalnum())
    if normalized_courier == 'bluedart':
        courier = 'Bluedart'
    return {
        "awb": shipment.awb,
        "carrier": courier,
    }


def parse_trackparcel_response(payload):
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    booking_details = data.get("booking_details") if isinstance(data, dict) else None
    status = first_value(data, ("status", "current_status", "currentStatus", "shipment_status", "tracking_status"))
    if not status:
        status = first_value(booking_details, ("status", "current_status", "currentStatus", "shipment_status", "tracking_status"))
    expected_delivery = first_value(
        data,
        ("expected_delivery_date", "expectedDeliveryDate", "edd", "estimated_delivery_date", "estimatedDeliveryDate"),
    )
    delivered_at = first_value(data, ("delivered_at", "deliveredAt", "delivery_date", "deliveryDate"))

    return {
        "status": status or "",
        "expected_delivery_date": parse_date_value(expected_delivery),
        "delivered_at": parse_datetime_value(delivered_at),
    }


def first_value(payload, keys):
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value:
            return value
    for value in payload.values():
        if isinstance(value, dict):
            nested = first_value(value, keys)
            if nested:
                return nested
    return None


def normalize_tracking_status(status):
    raw = (status or "").strip().upper().replace("-", "_").replace(" ", "_")
    if "OUT" in raw and "DELIVERY" in raw:
        return "OUT_FOR_DELIVERY"
    if "DELIVERED" in raw and "RTO" in raw:
        return "RTO_DELIVERED"
    if "DELIVERED" in raw:
        return "DELIVERED"
    if "CANCEL" in raw:
        return "CANCELLED"
    if "RTO" in raw:
        return "RTO_IN_TRANSIT"
    if "EXCEPTION" in raw or "UNDELIVERED" in raw:
        return "EXCEPTION"
    if "DELAY" in raw:
        return "DELAYED"
    if "TRANSIT" in raw or "SHIPPED" in raw:
        return "IN_TRANSIT"
    if "PICK" in raw:
        return "PICKUP_PENDING"
    return raw or "CREATED"


def calculate_next_check_after(status, expected_delivery_date=None, now=None):
    now = now or timezone.localtime()
    status = normalize_tracking_status(status)
    if status in FINAL_STATUSES:
        return None

    if expected_delivery_date and now.date() < expected_delivery_date:
        # Before the delivery date, monitor once per day rather than polling hourly.
        return now + timedelta(days=1)

    if expected_delivery_date and now.date() == expected_delivery_date:
        next_check = now + DELIVERY_DAY_REFRESH_INTERVAL
        window_end = now.replace(
            hour=DELIVERY_WINDOW_END_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )
        if next_check < window_end:
            return next_check
        return timezone.make_aware(datetime.combine(expected_delivery_date + timedelta(days=1), time(hour=10)))

    # Without an EDD, use the same low-frequency daily monitor until the
    # carrier returns an expected delivery date.
    return now + timedelta(days=1)


def _inside_delivery_window(now):
    return DELIVERY_WINDOW_START_HOUR <= now.hour < DELIVERY_WINDOW_END_HOUR


def parse_date_value(value):
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    parsed_datetime = parse_datetime_value(value)
    if parsed_datetime:
        return timezone.localtime(parsed_datetime).date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_datetime_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text[:19], text[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
            if len(candidate) == 10:
                parsed = datetime.combine(parsed.date(), time.min)
            return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)
        except ValueError:
            continue
    return None


def extract_quota_headers(headers):
    quota_headers = {}
    for key in ("X-Quota-Limit", "X-Quota-Used", "X-Quota-Reset", "X-RateLimit-Limit", "X-RateLimit-Remaining"):
        value = headers.get(key)
        if value is not None:
            quota_headers[key] = value
    return quota_headers
