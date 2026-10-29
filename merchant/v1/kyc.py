import hashlib
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from payments.adapters.omniware import OmniwareError, add_vendor, get_vendor_status, modify_vendor
from payments.auth import get_merchant_from_session_token
from payments.models import MerchantProviderProfile


PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
PHONE_RE = re.compile(r"^[6-9][0-9]{9}$")
AADHAAR_RE = re.compile(r"^[0-9]{12}$")


def _clean(value):
    return str(value or "").strip()


def _mask(value, visible=4):
    value = _clean(value)
    if not value:
        return ""
    return "*" * max(0, len(value) - visible) + value[-visible:]


def _mask_upi(value):
    value = _clean(value)
    if "@" not in value:
        return _mask(value)
    name, handle = value.split("@", 1)
    visible = name[:2]
    return f"{visible}{'*' * max(3, len(name) - 2)}@{handle}"


def _vendor_code(merchant):
    digest = hashlib.sha256(merchant.merchant_key.encode()).hexdigest()[:10].upper()
    return f"ESP{merchant.id}{digest}"[:30]


def _merchant(request):
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return get_merchant_from_session_token(token)


def _is_mock():
    return settings.DEBUG and getattr(settings, "PAYMENT_PROVIDER", "") == "mock_omniware"


def _profile_payload(profile, merchant):
    details = profile.provider_response if isinstance(profile.provider_response, dict) else {}
    return {
        "status": profile.vendor_status,
        "vendor_code": profile.vendor_code,
        "provider_account_id": profile.provider_account_id,
        "submitted_at": profile.submitted_at.isoformat() if profile.submitted_at else None,
        "approved_at": profile.approved_at.isoformat() if profile.approved_at else None,
        "details": details.get("submitted_details", {}),
        "provider_message": details.get("message", ""),
        "mock": _is_mock(),
        "can_submit": profile.vendor_status in {
            MerchantProviderProfile.STATUS_NOT_SUBMITTED,
            MerchantProviderProfile.STATUS_REJECTED,
        },
        "defaults": {
            "vendor_name": merchant.merchant_name,
            "vendor_contact_email": merchant.merchant_email,
            "vendor_contact_num": merchant.merchant_phone,
            "vendor_contact_address": merchant.merchant_address,
        },
    }


def _validated_submission(data):
    payload = {
        "vendor_name": _clean(data.get("vendor_name")),
        "vendor_contact_email": _clean(data.get("vendor_contact_email")).lower(),
        "vendor_contact_num": re.sub(r"\D", "", _clean(data.get("vendor_contact_num"))),
        "vendor_contact_address": _clean(data.get("vendor_contact_address")),
        "vendor_pan": _clean(data.get("vendor_pan")).upper(),
    }
    if not payload["vendor_name"]:
        raise ValueError("Vendor name is required.")
    try:
        validate_email(payload["vendor_contact_email"])
    except ValidationError as exc:
        raise ValueError("Enter a valid vendor email address.") from exc
    if not PHONE_RE.fullmatch(payload["vendor_contact_num"]):
        raise ValueError("Enter a valid 10-digit Indian mobile number.")
    if not payload["vendor_contact_address"]:
        raise ValueError("Registered business address is required.")
    if not payload["vendor_pan"]:
        raise ValueError("Vendor PAN is required.")
    if not PAN_RE.fullmatch(payload["vendor_pan"]):
        raise ValueError("Enter PAN in the format ABCDE1234F.")

    method = _clean(data.get("settlement_method")).lower()
    if method not in {"bank", "upi"}:
        raise ValueError("Choose bank account or UPI for settlement.")
    if method == "bank":
        bank = {
            "account_name": _clean(data.get("account_name")),
            "account_number": re.sub(r"\s", "", _clean(data.get("account_number"))),
            "ifsc_code": _clean(data.get("ifsc_code")).upper(),
            "bank_name": _clean(data.get("bank_name")),
            "bank_branch": _clean(data.get("bank_branch")),
        }
        if not bank["account_name"] or not bank["account_number"] or not bank["bank_name"]:
            raise ValueError("Account holder, account number, and bank name are required.")
        if not bank["account_number"].isdigit() or not 6 <= len(bank["account_number"]) <= 50:
            raise ValueError("Enter a valid bank account number.")
        if not IFSC_RE.fullmatch(bank["ifsc_code"]):
            raise ValueError("Enter a valid IFSC code.")
        payload.update(bank)
    else:
        upi_id = _clean(data.get("upi_id")).lower()
        if not re.fullmatch(r"[^\s@]{2,}@[A-Za-z0-9._-]{2,}", upi_id):
            raise ValueError("Enter a valid UPI ID.")
        payload["upi_id"] = upi_id

    activate_qr = bool(data.get("activate_bharat_qr"))
    aadhaar = re.sub(r"\D", "", _clean(data.get("aadhar_number")))
    if activate_qr:
        if not AADHAAR_RE.fullmatch(aadhaar):
            raise ValueError("A 12-digit Aadhaar number is required for Bharat QR.")
        payload["activate_bharat_qr"] = "y"
        payload["aadhar_number"] = aadhaar
    return payload, method, activate_qr


def _safe_submission(payload, method, activate_qr):
    details = {
        "vendor_name": payload["vendor_name"],
        "vendor_contact_email": payload["vendor_contact_email"],
        "vendor_contact_num": payload["vendor_contact_num"],
        "vendor_contact_address": payload["vendor_contact_address"],
        "vendor_pan_masked": _mask(payload.get("vendor_pan")),
        "settlement_method": method,
        "account_name": payload.get("account_name", ""),
        "account_number_masked": _mask(payload.get("account_number")),
        "ifsc_code": payload.get("ifsc_code", ""),
        "bank_name": payload.get("bank_name", ""),
        "bank_branch": payload.get("bank_branch", ""),
        "upi_id_masked": _mask_upi(payload.get("upi_id")),
        "bharat_qr_requested": activate_qr,
    }
    return details


class MerchantKYCView(APIView):
    def get(self, request):
        merchant = _merchant(request)
        if not merchant:
            return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
        profile, _created = MerchantProviderProfile.objects.get_or_create(merchant=merchant)
        return Response(_profile_payload(profile, merchant))

    def post(self, request):
        merchant = _merchant(request)
        if not merchant:
            return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
        profile, _created = MerchantProviderProfile.objects.get_or_create(merchant=merchant)
        action = _clean(request.data.get("action") or "submit").lower()

        if action in {"mock_approve", "mock_reject"}:
            if not _is_mock():
                return Response({"error": "Mock status controls are disabled."}, status=status.HTTP_403_FORBIDDEN)
            if profile.vendor_status != MerchantProviderProfile.STATUS_PENDING:
                return Response({"error": "Only a pending submission can be reviewed."}, status=status.HTTP_409_CONFLICT)
            if action == "mock_approve":
                profile.vendor_status = MerchantProviderProfile.STATUS_APPROVED
                profile.approved_at = timezone.now()
                message = "Mock Omniware vendor approved."
            else:
                profile.vendor_status = MerchantProviderProfile.STATUS_REJECTED
                profile.approved_at = None
                message = "Mock Omniware vendor rejected. Update the details and resubmit."
            profile.provider_response = {**(profile.provider_response or {}), "message": message}
            profile.save(update_fields=["vendor_status", "approved_at", "provider_response", "updated_at"])
            return Response(_profile_payload(profile, merchant))

        if action == "refresh":
            if not profile.vendor_code:
                return Response({"error": "Submit KYC before checking its status."}, status=status.HTTP_400_BAD_REQUEST)
            if _is_mock():
                return Response(_profile_payload(profile, merchant))
            try:
                _request_payload, provider_response = get_vendor_status(profile.vendor_code)
            except OmniwareError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
            data = provider_response.get("data", {}) if isinstance(provider_response, dict) else {}
            approved = str(data.get("vendor_approved") or "").lower() == "y"
            profile.vendor_status = MerchantProviderProfile.STATUS_APPROVED if approved else MerchantProviderProfile.STATUS_PENDING
            profile.approved_at = timezone.now() if approved else None
            profile.provider_account_id = str((data.get("bank_accounts") or [{}])[0].get("account_id") or profile.provider_account_id)
            profile.provider_response = {
                **(profile.provider_response or {}),
                "message": "Omniware vendor approved." if approved else "Omniware review is pending.",
            }
            profile.save()
            return Response(_profile_payload(profile, merchant))

        if action == "modify":
            if profile.vendor_status != MerchantProviderProfile.STATUS_APPROVED or not profile.vendor_code:
                return Response(
                    {"error": "Only approved KYC details can be modified."},
                    status=status.HTTP_409_CONFLICT,
                )
            try:
                provider_payload, method, activate_qr = _validated_submission(request.data)
            except ValueError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            provider_payload["vendor_code"] = profile.vendor_code
            if _is_mock():
                provider_response = {
                    "data": {
                        "code": "SUCCESS",
                        "account_id": profile.provider_account_id,
                        "message": "Updated vendor details submitted for mock Omniware reapproval.",
                    }
                }
            else:
                try:
                    _request_payload, provider_response = modify_vendor(provider_payload)
                except OmniwareError as exc:
                    return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

            data = provider_response.get("data", {}) if isinstance(provider_response, dict) else {}
            profile.provider_account_id = str(data.get("account_id") or profile.provider_account_id)
            profile.vendor_status = MerchantProviderProfile.STATUS_PENDING
            profile.approved_at = None
            profile.provider_response = {
                **(profile.provider_response or {}),
                "message": str(data.get("message") or "Updated vendor details submitted for Omniware reapproval."),
                "submitted_details": _safe_submission(provider_payload, method, activate_qr),
            }
            profile.submitted_at = timezone.now()
            profile.save(update_fields=["provider_account_id", "vendor_status", "approved_at", "provider_response", "submitted_at", "updated_at"])
            return Response(_profile_payload(profile, merchant))

        if profile.vendor_status in {MerchantProviderProfile.STATUS_PENDING, MerchantProviderProfile.STATUS_APPROVED}:
            return Response({"error": "This KYC submission is already under review or approved."}, status=status.HTTP_409_CONFLICT)
        try:
            provider_payload, method, activate_qr = _validated_submission(request.data)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        profile.vendor_code = profile.vendor_code or _vendor_code(merchant)
        provider_payload["vendor_code"] = profile.vendor_code
        if _is_mock():
            provider_response = {
                "data": {
                    "code": "SUCCESS",
                    "account_id": f"MOCK-{merchant.id}",
                    "message": "Vendor submitted to mock Omniware review.",
                }
            }
        else:
            try:
                _request_payload, provider_response = add_vendor(provider_payload)
            except OmniwareError as exc:
                return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        data = provider_response.get("data", {}) if isinstance(provider_response, dict) else {}
        profile.provider = "omniware"
        profile.provider_account_id = str(data.get("account_id") or "")
        profile.vendor_status = MerchantProviderProfile.STATUS_PENDING
        profile.submitted_at = timezone.now()
        profile.approved_at = None
        profile.provider_response = {
            "mock": _is_mock(),
            "message": str(data.get("message") or "Submitted to Omniware for review."),
            "submitted_details": _safe_submission(provider_payload, method, activate_qr),
        }
        profile.save()
        return Response(_profile_payload(profile, merchant), status=status.HTTP_201_CREATED)
