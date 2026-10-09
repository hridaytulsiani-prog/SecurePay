import socket

import requests
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
from tracking.api.v1.bluedart_label_otp_evidence import analyze_bluedart_label_otp_evidence
from tracking.models.bluedart_otp_check import BlueDartOtpCheck
from tracking.models.bluedart_verification_decision import BlueDartVerificationDecision
from tracking.models.delhivery_otp_check import DelhiveryOtpCheck
from tracking.models.delhivery_verification_decision import DelhiveryVerificationDecision


class AdminDelhiveryOtpCheckView(AdminAPIView):
    required_permission = "courier.check"

    def post(self, request, *args, **kwargs):
        awb = normalize_awb(request.data.get("awb"))
        if not awb:
            return Response({"error": "AWB is required"}, status=400)

        check = DelhiveryOtpCheck.objects.create(
            awb=awb,
            checked_by=getattr(request, "admin_user", None),
        )

        try:
            http_status, payload = fetch_delhivery_tracking(awb)
            parsed = parse_delhivery_otp_status(payload)
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
        "tracking_summary": extract_tracking_summary(check.raw_response) if check.raw_response else None,
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
