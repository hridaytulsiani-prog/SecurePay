import socket
from datetime import timedelta
import json

import requests
from django.utils import timezone
from rest_framework.response import Response

from adminpanel.permissions import AdminAPIView
from tracking.api.v1.delhivery_otp import (
    fetch_delhivery_tracking,
    extract_tracking_summary,
    normalize_awb,
    parse_delhivery_otp_status,
    stable_json_hash,
)
from tracking.api.v1.bluedart_otp import (
    fetch_bluedart_tracking,
    extract_tracking_summary as extract_bluedart_tracking_summary,
    parse_bluedart_otp_status,
    stable_json_hash as stable_bluedart_json_hash,
)
from tracking.api.v1.dhl_bluedart_otp import (
    extract_dhl_bluedart_tracking_summary,
    fetch_dhl_bluedart_tracking,
    parse_dhl_bluedart_otp_status,
)
from tracking.api.v1.xpressbees_otp import (
    extract_xpressbees_tracking_summary,
    fetch_xpressbees_tracking,
    normalize_awb as normalize_xpressbees_awb,
    parse_xpressbees_otp_status,
)
from tracking.api.v1.dtdc_otp import (
    extract_dtdc_tracking_summary,
    fetch_dtdc_tracking,
    normalize_awb as normalize_dtdc_awb,
    parse_dtdc_otp_status,
)
from tracking.api.v1.shiprocket_otp import (
    extract_shiprocket_tracking_summary,
    fetch_shiprocket_tracking,
    normalize_awb as normalize_shiprocket_awb,
    parse_shiprocket_otp_status,
)
from tracking.api.v1.ekart_otp import (
    extract_ekart_tracking_summary,
    fetch_ekart_tracking,
    normalize_awb as normalize_ekart_awb,
    parse_ekart_otp_status,
)
from tracking.api.v1.shadowfax_otp import (
    extract_shadowfax_tracking_summary,
    fetch_shadowfax_tracking,
    normalize_awb as normalize_shadowfax_awb,
    parse_shadowfax_otp_status,
)
from tracking.api.v1.courier_tracking_sync import sync_manual_check_to_snapshot
from tracking.api.v1.bluedart_label_otp_evidence import analyze_bluedart_label_otp_evidence
from tracking.models.bluedart_otp_check import BlueDartOtpCheck
from tracking.models.bluedart_verification_decision import BlueDartVerificationDecision
from tracking.models.delhivery_otp_check import DelhiveryOtpCheck
from tracking.models.delhivery_verification_decision import DelhiveryVerificationDecision


COURIER_CHECK_COOLDOWN = timedelta(hours=1)
COURIER_CHECK_WINDOW = timedelta(minutes=1)
COURIER_CHECKS_PER_MINUTE = 5


def _public_tracking_with_shiprocket_fallback(
    awb, fetch_public, parse_public, extract_public, courier_name
):
    primary_error = None
    primary_payload = None
    try:
        http_status, payload = fetch_public(awb)
        parsed = parse_public(payload)
        summary = extract_public(payload)
        delivery_status = str(parsed.get("delivery_status") or "").upper()
        if delivery_status not in {"", "UNKNOWN", "NOT_DELIVERED_OR_UNKNOWN"}:
            return http_status, payload, parsed, summary
        primary_payload = payload
    except (requests.RequestException, ValueError) as exc:
        primary_error = exc

    try:
        fallback_http_status, fallback_payload = fetch_shiprocket_tracking(awb)
        fallback_parsed = parse_shiprocket_otp_status(fallback_payload)
        fallback_summary = extract_shiprocket_tracking_summary(fallback_payload)
        fallback_payload = {
            **fallback_payload,
            "_securepay_fallback_provider": "shiprocket",
            "_securepay_primary_courier": courier_name,
            "_securepay_primary_response": primary_payload,
        }
        return fallback_http_status, fallback_payload, fallback_parsed, fallback_summary
    except (requests.RequestException, ValueError):
        if primary_error:
            raise primary_error
        raise


def _fetch_shipsagar_tracking(awb):
    response = requests.post(
        "http://app.shipsagar.com/api/Web/TrackShipment",
        json={
            "Token": "0691F3D5-0B37-4520-A1DC-1DD4C151CC42",
            "ClientCode": "C1375",
            "TrackingNo": awb,
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "success":
        raise ValueError(payload.get("message") or "ShipSagar could not track this AWB")

    tracking_details = payload.get("trackingDetails")
    if isinstance(tracking_details, str):
        tracking_details = json.loads(tracking_details)
        if isinstance(tracking_details, str):
            tracking_details = json.loads(tracking_details)
    tracking_details = tracking_details if isinstance(tracking_details, dict) else {}
    status = str(tracking_details.get("CurrentStatus") or "").strip()
    fallback_payload = {
        **payload,
        "_securepay_fallback_provider": "shipsagar",
        "_securepay_tracking_details": tracking_details,
    }
    parsed = {
        "otp_status": "UNKNOWN",
        "delivery_status": "DELIVERED" if status.lower() == "delivered" else (status or "NOT_DELIVERED_OR_UNKNOWN"),
        "evidence": [],
    }
    summary = {
        "status": status,
        "hq_status": status,
        "courier_status": status,
        "status_time": tracking_details.get("StatusTime") or tracking_details.get("UpdatedAt") or "",
        "scans": [],
        "meaningful_fields": [],
        "fields": [],
        "extra_fields": {},
    }
    return response.status_code, fallback_payload, parsed, summary


def _public_tracking_with_shipsagar_fallback(awb):
    primary_error = None
    primary_payload = None
    try:
        http_status, payload = fetch_delhivery_tracking(awb)
        parsed = parse_delhivery_otp_status(payload)
        summary = extract_tracking_summary(payload)
        # A valid Delhivery response can be in-transit or picked up without
        # containing the word "delivered". Do not mistake that valid response
        # for an OTP failure and send it to ShipSagar unnecessarily.
        has_public_status = any(
            str(summary.get(key) or "").strip()
            for key in ("status", "hq_status", "delivery_pill_label", "current_flow")
        ) or bool(summary.get("scans"))
        if has_public_status:
            return http_status, payload, parsed, summary
        primary_payload = payload
    except (requests.RequestException, ValueError) as exc:
        primary_error = exc

    try:
        fallback = _fetch_shipsagar_tracking(awb)
        fallback[1]["_securepay_primary_response"] = primary_payload
        return fallback
    except (requests.RequestException, ValueError):
        if primary_payload is not None:
            # Delhivery returned a valid response; keep it even if the
            # optional fallback provider does not recognize the AWB.
            return (
                http_status,
                primary_payload,
                parse_delhivery_otp_status(primary_payload),
                extract_tracking_summary(primary_payload),
            )
        if primary_error:
            raise primary_error
        raise


def _recent_courier_check(awb, courier):
    cutoff = timezone.now() - COURIER_CHECK_COOLDOWN
    return (
        DelhiveryOtpCheck.objects
        .filter(awb=awb, courier=courier, checked_at__gte=cutoff)
        .exclude(otp_status=DelhiveryOtpCheck.FETCH_FAILED)
        .order_by("-checked_at")
        .first()
    )


def _courier_rate_limit_response(courier):
    cutoff = timezone.now() - COURIER_CHECK_WINDOW
    recent_count = DelhiveryOtpCheck.objects.filter(courier=courier, checked_at__gte=cutoff).count()
    if recent_count < COURIER_CHECKS_PER_MINUTE:
        return None

    response = Response(
        {
            "error": f"{courier.title()} tracking is temporarily rate-limited. Try again shortly.",
            "retry_after_seconds": 12,
        },
        status=429,
    )
    response["Retry-After"] = "12"
    return response


class AdminDelhiveryOtpCheckView(AdminAPIView):
    required_permission = "courier.check"

    def post(self, request, *args, **kwargs):
        awb = normalize_awb(request.data.get("awb"))
        if not awb:
            return Response({"error": "AWB is required"}, status=400)

        recent = _recent_courier_check(awb, "delhivery")
        if recent:
            return Response(_serialize_check(recent))
        rate_limited = _courier_rate_limit_response("delhivery")
        if rate_limited:
            return rate_limited

        check = DelhiveryOtpCheck.objects.create(
            awb=awb,
            checked_by=getattr(request, "admin_user", None),
        )

        try:
            http_status, payload, parsed, tracking_summary = _public_tracking_with_shipsagar_fallback(awb)
            check.http_status = http_status
            check.raw_response = payload
            check.response_hash = stable_json_hash(payload)
            check.otp_status = parsed["otp_status"]
            check.delivery_status = (
                tracking_summary.get("status")
                or tracking_summary.get("hq_status")
                or tracking_summary.get("delivery_pill_label")
                or parsed["delivery_status"]
            )
            check.evidence = parsed["evidence"]
            check.save(
                update_fields=[
                    "http_status",
                    "raw_response",
                    "response_hash",
                    "otp_status",
                    "delivery_status",
                    "evidence",
                ]
            )
            sync_manual_check_to_snapshot(
                awb, "delhivery", payload, tracking_summary, parsed
            )
        except requests.RequestException as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = _format_request_error(exc)
            response = getattr(exc, "response", None)
            if response is not None:
                check.http_status = response.status_code
            check.save(update_fields=["otp_status", "error", "http_status"])
        except ValueError as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = f"Invalid JSON response: {exc}"
            check.save(update_fields=["otp_status", "error"])

        return Response(_serialize_check(check))


class AdminDelhiveryVerificationDecisionView(AdminAPIView):
    required_permission = "courier.decide"

    def post(self, request, *args, **kwargs):
        awb = normalize_awb(request.data.get("awb"))
        decision = (request.data.get("decision") or "").strip().upper()
        source_check_id = request.data.get("source_check_id")

        if not awb:
            return Response({"error": "AWB is required"}, status=400)
        if decision not in {DelhiveryVerificationDecision.VERIFIED, DelhiveryVerificationDecision.NOT_VERIFIED}:
            return Response({"error": "decision must be VERIFIED or NOT_VERIFIED"}, status=400)

        source_check = None
        if source_check_id:
            source_check = DelhiveryOtpCheck.objects.filter(id=source_check_id, awb=awb).first()

        manual_decision = DelhiveryVerificationDecision.objects.create(
            awb=awb,
            decision=decision,
            source_check=source_check,
            decided_by=getattr(request, "admin_user", None),
        )

        return Response(_serialize_decision(manual_decision))


class AdminBlueDartOtpCheckView(AdminAPIView):
    required_permission = "courier.check"

    def post(self, request, *args, **kwargs):
        awb = normalize_awb(request.data.get("awb"))
        if not awb:
            return Response({"error": "AWB is required"}, status=400)

        check = BlueDartOtpCheck.objects.create(
            awb=awb,
            checked_by=getattr(request, "admin_user", None),
        )

        try:
            http_status, payload = fetch_bluedart_tracking(awb)
            parsed = parse_bluedart_otp_status(payload)
            check.http_status = http_status
            check.raw_response = payload
            check.response_hash = stable_bluedart_json_hash(payload)
            check.otp_status = parsed["otp_status"]
            check.delivery_status = parsed["delivery_status"]
            check.evidence = parsed["evidence"]
            check.save(
                update_fields=[
                    "http_status",
                    "raw_response",
                    "response_hash",
                    "otp_status",
                    "delivery_status",
                    "evidence",
                ]
            )
        except requests.RequestException as exc:
            check.otp_status = BlueDartOtpCheck.FETCH_FAILED
            check.error = _format_request_error(exc, "Blue Dart")
            response = getattr(exc, "response", None)
            if response is not None:
                check.http_status = response.status_code
            check.save(update_fields=["otp_status", "error", "http_status"])
        except ValueError as exc:
            check.otp_status = BlueDartOtpCheck.FETCH_FAILED
            check.error = f"Invalid Blue Dart tracking response: {exc}"
            check.save(update_fields=["otp_status", "error"])

        return Response(_serialize_bluedart_check(check))


class AdminDhlBlueDartOtpCheckView(AdminAPIView):
    """Check a Blue Dart AWB through DHL's public tracking endpoint."""

    required_permission = "courier.check"

    def post(self, request, *args, **kwargs):
        awb = normalize_awb(request.data.get("awb"))
        if not awb:
            return Response({"error": "AWB is required"}, status=400)

        check = BlueDartOtpCheck.objects.create(
            awb=awb,
            courier="dhl_bluedart",
            checked_by=getattr(request, "admin_user", None),
        )

        try:
            http_status, payload = fetch_dhl_bluedart_tracking(awb)
            parsed = parse_dhl_bluedart_otp_status(payload)
            check.http_status = http_status
            check.raw_response = payload
            check.response_hash = stable_bluedart_json_hash(payload)
            check.otp_status = parsed["otp_status"]
            check.delivery_status = parsed["delivery_status"]
            check.evidence = parsed["evidence"]
            check.save(
                update_fields=[
                    "http_status",
                    "raw_response",
                    "response_hash",
                    "otp_status",
                    "delivery_status",
                    "evidence",
                ]
            )
        except requests.RequestException as exc:
            check.otp_status = BlueDartOtpCheck.FETCH_FAILED
            check.error = _format_request_error(exc, "DHL Blue Dart")
            response = getattr(exc, "response", None)
            if response is not None:
                check.http_status = response.status_code
            check.save(update_fields=["otp_status", "error", "http_status"])
        except ValueError as exc:
            check.otp_status = BlueDartOtpCheck.FETCH_FAILED
            check.error = str(exc)
            check.save(update_fields=["otp_status", "error"])

        return Response(_serialize_dhl_bluedart_check(check))


class AdminXpressbeesOtpCheckView(AdminAPIView):
    """Check an Xpressbees AWB through its public tracking endpoint."""

    required_permission = "courier.check"

    def post(self, request, *args, **kwargs):
        awb = normalize_xpressbees_awb(request.data.get("awb"))
        if not awb:
            return Response({"error": "AWB is required"}, status=400)

        check = DelhiveryOtpCheck.objects.create(
            awb=awb,
            courier="xpressbees",
            checked_by=getattr(request, "admin_user", None),
        )

        try:
            http_status, payload = fetch_xpressbees_tracking(awb)
            parsed = parse_xpressbees_otp_status(payload)
            check.http_status = http_status
            check.raw_response = payload
            check.response_hash = stable_json_hash(payload)
            check.otp_status = parsed["otp_status"]
            check.delivery_status = parsed["delivery_status"]
            check.evidence = parsed["evidence"]
            check.save(
                update_fields=[
                    "http_status",
                    "raw_response",
                    "response_hash",
                    "otp_status",
                    "delivery_status",
                    "evidence",
                ]
            )
        except requests.RequestException as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = _format_request_error(exc, "Xpressbees")
            response = getattr(exc, "response", None)
            if response is not None:
                check.http_status = response.status_code
            check.save(update_fields=["otp_status", "error", "http_status"])
        except RuntimeError as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = str(exc)
            check.save(update_fields=["otp_status", "error"])
        except ValueError as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = f"Invalid Xpressbees tracking response: {exc}"
            check.save(update_fields=["otp_status", "error"])

        return Response(_serialize_xpressbees_check(check))


class AdminDtdcOtpCheckView(AdminAPIView):
    """Check a DTDC AWB through DTDC's public tracking-results endpoint."""

    required_permission = "courier.check"

    def post(self, request, *args, **kwargs):
        awb = normalize_dtdc_awb(request.data.get("awb"))
        if not awb:
            return Response({"error": "AWB is required"}, status=400)

        recent = _recent_courier_check(awb, "dtdc")
        if recent:
            return Response(_serialize_dtdc_check(recent))
        rate_limited = _courier_rate_limit_response("dtdc")
        if rate_limited:
            return rate_limited

        check = DelhiveryOtpCheck.objects.create(
            awb=awb,
            courier="dtdc",
            checked_by=getattr(request, "admin_user", None),
        )

        try:
            http_status, payload, parsed, tracking_summary = _public_tracking_with_shiprocket_fallback(
                awb, fetch_dtdc_tracking, parse_dtdc_otp_status,
                extract_dtdc_tracking_summary, "dtdc",
            )
            check.http_status = http_status
            check.raw_response = payload
            check.response_hash = stable_json_hash(payload)
            check.otp_status = parsed["otp_status"]
            check.delivery_status = parsed["delivery_status"]
            check.evidence = parsed["evidence"]
            check.save(update_fields=[
                "http_status", "raw_response", "response_hash", "otp_status",
                "delivery_status", "evidence",
            ])
            sync_manual_check_to_snapshot(
                awb, "dtdc", payload, tracking_summary, parsed
            )
        except requests.RequestException as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = _format_request_error(exc, "DTDC")
            response = getattr(exc, "response", None)
            if response is not None:
                check.http_status = response.status_code
            check.save(update_fields=["otp_status", "error", "http_status"])
        except ValueError as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = f"Invalid DTDC tracking response: {exc}"
            check.save(update_fields=["otp_status", "error"])

        return Response(_serialize_dtdc_check(check))


class AdminShiprocketOtpCheckView(AdminAPIView):
    """Check a Shiprocket AWB through Shiprocket's authenticated AWB endpoint."""

    required_permission = "courier.check"

    def post(self, request, *args, **kwargs):
        awb = normalize_shiprocket_awb(request.data.get("awb"))
        if not awb:
            return Response({"error": "AWB is required"}, status=400)

        recent = _recent_courier_check(awb, "shiprocket")
        if recent:
            return Response(_serialize_shiprocket_check(recent))
        rate_limited = _courier_rate_limit_response("shiprocket")
        if rate_limited:
            return rate_limited

        check = DelhiveryOtpCheck.objects.create(
            awb=awb,
            courier="shiprocket",
            checked_by=getattr(request, "admin_user", None),
        )

        try:
            http_status, payload = fetch_shiprocket_tracking(awb)
            parsed = parse_shiprocket_otp_status(payload)
            check.http_status = http_status
            check.raw_response = payload
            check.response_hash = stable_json_hash(payload)
            check.otp_status = parsed["otp_status"]
            check.delivery_status = parsed["delivery_status"]
            check.evidence = parsed["evidence"]
            check.save(update_fields=[
                "http_status", "raw_response", "response_hash", "otp_status",
                "delivery_status", "evidence",
            ])
            sync_manual_check_to_snapshot(
                awb, "shiprocket", payload, extract_shiprocket_tracking_summary(payload), parsed
            )
        except requests.RequestException as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = _format_request_error(exc, "Shiprocket")
            response = getattr(exc, "response", None)
            if response is not None:
                check.http_status = response.status_code
            check.save(update_fields=["otp_status", "error", "http_status"])
        except RuntimeError as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = str(exc)
            check.save(update_fields=["otp_status", "error"])
        except ValueError as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = f"Invalid Shiprocket tracking response: {exc}"
            check.save(update_fields=["otp_status", "error"])

        return Response(_serialize_shiprocket_check(check))


class AdminEkartOtpCheckView(AdminAPIView):
    """Check an Ekart AWB through Ekart's free public tracking endpoint."""

    required_permission = "courier.check"

    def post(self, request, *args, **kwargs):
        awb = normalize_ekart_awb(request.data.get("awb"))
        if not awb:
            return Response({"error": "AWB is required"}, status=400)

        recent = _recent_courier_check(awb, "ekart")
        if recent:
            return Response(_serialize_ekart_check(recent))
        rate_limited = _courier_rate_limit_response("ekart")
        if rate_limited:
            return rate_limited

        check = DelhiveryOtpCheck.objects.create(
            awb=awb,
            courier="ekart",
            checked_by=getattr(request, "admin_user", None),
        )

        try:
            http_status, payload, parsed, tracking_summary = _public_tracking_with_shiprocket_fallback(
                awb, fetch_ekart_tracking, parse_ekart_otp_status,
                extract_ekart_tracking_summary, "ekart",
            )
            check.http_status = http_status
            check.raw_response = payload
            check.response_hash = stable_json_hash(payload)
            check.otp_status = parsed["otp_status"]
            check.delivery_status = parsed["delivery_status"]
            check.evidence = parsed["evidence"]
            check.save(update_fields=[
                "http_status", "raw_response", "response_hash", "otp_status",
                "delivery_status", "evidence",
            ])
            sync_manual_check_to_snapshot(
                awb, "ekart", payload, tracking_summary, parsed
            )
        except requests.RequestException as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = _format_request_error(exc, "Ekart")
            response = getattr(exc, "response", None)
            if response is not None:
                check.http_status = response.status_code
            check.save(update_fields=["otp_status", "error", "http_status"])
        except ValueError as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = f"Invalid Ekart tracking response: {exc}"
            check.save(update_fields=["otp_status", "error"])

        return Response(_serialize_ekart_check(check))


class AdminShadowfaxOtpCheckView(AdminAPIView):
    """Check a Shadowfax AWB through the request used by its public tracker."""

    required_permission = "courier.check"

    def post(self, request, *args, **kwargs):
        awb = normalize_shadowfax_awb(request.data.get("awb"))
        if not awb:
            return Response({"error": "AWB is required"}, status=400)

        recent = _recent_courier_check(awb, "shadowfax")
        if recent:
            return Response(_serialize_shadowfax_check(recent))
        rate_limited = _courier_rate_limit_response("shadowfax")
        if rate_limited:
            return rate_limited

        check = DelhiveryOtpCheck.objects.create(
            awb=awb,
            courier="shadowfax",
            checked_by=getattr(request, "admin_user", None),
        )

        try:
            http_status, payload, parsed, tracking_summary = _public_tracking_with_shiprocket_fallback(
                awb, fetch_shadowfax_tracking, parse_shadowfax_otp_status,
                extract_shadowfax_tracking_summary, "shadowfax",
            )
            check.http_status = http_status
            check.raw_response = payload
            check.response_hash = stable_json_hash(payload)
            check.otp_status = parsed["otp_status"]
            check.delivery_status = parsed["delivery_status"]
            check.evidence = parsed["evidence"]
            check.save(update_fields=["http_status", "raw_response", "response_hash", "otp_status", "delivery_status", "evidence"])
            sync_manual_check_to_snapshot(
                awb, "shadowfax", payload, tracking_summary, parsed
            )
        except requests.RequestException as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = _format_request_error(exc, "Shadowfax")
            response = getattr(exc, "response", None)
            if response is not None:
                check.http_status = response.status_code
            check.save(update_fields=["otp_status", "error", "http_status"])
        except ValueError as exc:
            check.otp_status = DelhiveryOtpCheck.FETCH_FAILED
            check.error = f"Invalid Shadowfax tracking response: {exc}"
            check.save(update_fields=["otp_status", "error"])

        return Response(_serialize_shadowfax_check(check))


class AdminBlueDartVerificationDecisionView(AdminAPIView):
    required_permission = "courier.decide"

    def post(self, request, *args, **kwargs):
        awb = normalize_awb(request.data.get("awb"))
        decision = (request.data.get("decision") or "").strip().upper()
        source_check_id = request.data.get("source_check_id")

        if not awb:
            return Response({"error": "AWB is required"}, status=400)
        if decision not in {BlueDartVerificationDecision.VERIFIED, BlueDartVerificationDecision.NOT_VERIFIED}:
            return Response({"error": "decision must be VERIFIED or NOT_VERIFIED"}, status=400)

        source_check = None
        if source_check_id:
            source_check = BlueDartOtpCheck.objects.filter(id=source_check_id, awb=awb).first()

        manual_decision = BlueDartVerificationDecision.objects.create(
            awb=awb,
            decision=decision,
            source_check=source_check,
            decided_by=getattr(request, "admin_user", None),
        )

        return Response(_serialize_decision(manual_decision))


class AdminBlueDartLabelOtpEvidenceView(AdminAPIView):
    required_permission = "courier.check"

    def post(self, request, *args, **kwargs):
        pdf_file = request.FILES.get("file")
        if not pdf_file:
            return Response({"error": "PDF file is required"}, status=400)
        if not pdf_file.name.lower().endswith(".pdf"):
            return Response({"error": "Only PDF files are allowed"}, status=400)

        try:
            return Response(analyze_bluedart_label_otp_evidence(pdf_file))
        except Exception as exc:
            return Response({"error": f"Could not inspect this PDF label: {exc}"}, status=422)


def _serialize_check(check):
    tracking_summary = (
        _tracking_summary_for_check(check, extract_tracking_summary)
        if check.raw_response else None
    )
    return {
        "id": check.id,
        "awb": check.awb,
        "courier": check.courier,
        "otp_status": check.otp_status,
        "delivery_status": (
            (tracking_summary or {}).get("status")
            or (tracking_summary or {}).get("hq_status")
            or (tracking_summary or {}).get("delivery_pill_label")
            or check.delivery_status
        ),
        "evidence": check.evidence,
        "response_hash": check.response_hash,
        "http_status": check.http_status,
        "raw_response": check.raw_response,
        "error": check.error,
        "checked_at": check.checked_at.isoformat(),
        "checked_by": getattr(check.checked_by, "username", None),
        "tracking_summary": tracking_summary,
        "manual_decision": _latest_decision_for_awb(check.awb),
    }


def _serialize_bluedart_check(check):
    return {
        "id": check.id,
        "awb": check.awb,
        "courier": check.courier,
        "otp_status": check.otp_status,
        "delivery_status": check.delivery_status,
        "evidence": check.evidence,
        "response_hash": check.response_hash,
        "http_status": check.http_status,
        "raw_response": check.raw_response,
        "error": check.error,
        "checked_at": check.checked_at.isoformat(),
        "checked_by": getattr(check.checked_by, "username", None),
        "tracking_summary": extract_bluedart_tracking_summary(check.raw_response) if check.raw_response else None,
        "manual_decision": _latest_bluedart_decision_for_awb(check.awb),
    }


def _serialize_dhl_bluedart_check(check):
    serialized = _serialize_bluedart_check(check)
    serialized["courier"] = "dhl_bluedart"
    serialized["provider"] = "DHL eCommerce India / Blue Dart API"
    if check.raw_response:
        serialized["tracking_summary"] = extract_dhl_bluedart_tracking_summary(check.raw_response)
    return serialized


def _serialize_xpressbees_check(check):
    return {
        "id": check.id,
        "awb": check.awb,
        "courier": check.courier,
        "provider": "Xpressbees public tracking",
        "otp_status": check.otp_status,
        "delivery_status": check.delivery_status,
        "evidence": check.evidence,
        "response_hash": check.response_hash,
        "http_status": check.http_status,
        "raw_response": check.raw_response,
        "error": check.error,
        "checked_at": check.checked_at.isoformat(),
        "checked_by": getattr(check.checked_by, "username", None),
        "tracking_summary": extract_xpressbees_tracking_summary(check.raw_response) if check.raw_response else None,
        "manual_decision": None,
    }


def _tracking_summary_for_check(check, public_extractor):
    raw_response = check.raw_response or {}
    if raw_response.get("_securepay_fallback_provider") == "shiprocket":
        return extract_shiprocket_tracking_summary(raw_response)
    if raw_response.get("_securepay_fallback_provider") == "shipsagar":
        return {
            "status": raw_response.get("_securepay_tracking_details", {}).get("CurrentStatus", ""),
            "hq_status": raw_response.get("_securepay_tracking_details", {}).get("CurrentStatus", ""),
            "courier_status": raw_response.get("_securepay_tracking_details", {}).get("CurrentStatus", ""),
            "status_time": raw_response.get("_securepay_tracking_details", {}).get("StatusTime", ""),
            "scans": [],
            "meaningful_fields": [],
            "fields": [],
            "extra_fields": {},
        }
    return public_extractor(raw_response)


def _serialize_dtdc_check(check):
    tracking_summary = (
        _tracking_summary_for_check(check, extract_dtdc_tracking_summary)
        if check.raw_response else None
    )
    return {
        "id": check.id,
        "awb": check.awb,
        "courier": check.courier,
        "provider": "DTDC public tracking",
        "otp_status": check.otp_status,
        "delivery_status": (tracking_summary or {}).get("delivery_status") or check.delivery_status,
        "evidence": check.evidence,
        "response_hash": check.response_hash,
        "http_status": check.http_status,
        "raw_response": check.raw_response,
        "error": check.error,
        "checked_at": check.checked_at.isoformat(),
        "checked_by": getattr(check.checked_by, "username", None),
        "tracking_summary": tracking_summary,
        "manual_decision": None,
    }


def _serialize_shiprocket_check(check):
    return {
        "id": check.id,
        "awb": check.awb,
        "courier": check.courier,
        "provider": "Shiprocket public AWB tracking",
        "otp_status": check.otp_status,
        "delivery_status": check.delivery_status,
        "evidence": check.evidence,
        "response_hash": check.response_hash,
        "http_status": check.http_status,
        "raw_response": check.raw_response,
        "error": check.error,
        "checked_at": check.checked_at.isoformat(),
        "checked_by": getattr(check.checked_by, "username", None),
        "tracking_summary": extract_shiprocket_tracking_summary(check.raw_response) if check.raw_response else None,
        "manual_decision": None,
    }


def _serialize_ekart_check(check):
    tracking_summary = (
        _tracking_summary_for_check(check, extract_ekart_tracking_summary)
        if check.raw_response else None
    )
    return {
        "id": check.id,
        "awb": check.awb,
        "courier": check.courier,
        "provider": "Ekart public tracking",
        "otp_status": check.otp_status,
        "delivery_status": check.delivery_status,
        "evidence": check.evidence,
        "response_hash": check.response_hash,
        "http_status": check.http_status,
        "raw_response": check.raw_response,
        "error": check.error,
        "checked_at": check.checked_at.isoformat(),
        "checked_by": getattr(check.checked_by, "username", None),
        "tracking_summary": tracking_summary,
        "manual_decision": None,
    }


def _serialize_shadowfax_check(check):
    tracking_summary = (
        _tracking_summary_for_check(check, extract_shadowfax_tracking_summary)
        if check.raw_response else None
    )
    return {
        "id": check.id,
        "awb": check.awb,
        "courier": check.courier,
        "provider": "Shadowfax public tracking page",
        "otp_status": check.otp_status,
        "delivery_status": check.delivery_status,
        "evidence": check.evidence,
        "response_hash": check.response_hash,
        "http_status": check.http_status,
        "raw_response": check.raw_response,
        "error": check.error,
        "checked_at": check.checked_at.isoformat(),
        "checked_by": getattr(check.checked_by, "username", None),
        "tracking_summary": tracking_summary,
        "manual_decision": None,
    }


def _latest_decision_for_awb(awb):
    decision = DelhiveryVerificationDecision.objects.filter(awb=awb).first()
    return _serialize_decision(decision) if decision else None


def _latest_bluedart_decision_for_awb(awb):
    decision = BlueDartVerificationDecision.objects.filter(awb=awb).first()
    return _serialize_decision(decision) if decision else None


def _serialize_decision(decision):
    return {
        "id": decision.id,
        "awb": decision.awb,
        "decision": decision.decision,
        "decided_at": decision.decided_at.isoformat(),
        "decided_by": getattr(decision.decided_by, "username", None),
        "source_check_id": decision.source_check_id,
    }


def _format_request_error(exc, courier_name="Delhivery"):
    message = str(exc)
    if isinstance(exc, requests.exceptions.ConnectionError):
        if "NameResolutionError" in message or "getaddrinfo failed" in message:
            return (
                f"{courier_name} tracking host could not be resolved from this server. "
                "This is a DNS/endpoint availability issue, not an AWB validation result. "
                f"Raw error: {message}"
            )
        return (
            f"Could not connect to {courier_name} tracking from this server. "
            f"Check internet access, firewall, proxy, or {courier_name} endpoint availability. "
            f"Raw error: {message}"
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return f"{courier_name} tracking request timed out. Raw error: {message}"
    if isinstance(getattr(exc, "__cause__", None), socket.gaierror):
        return f"{courier_name} tracking DNS lookup failed. Raw error: {message}"
    return message
