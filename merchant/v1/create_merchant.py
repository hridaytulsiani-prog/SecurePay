# Merchant account signup + login — the only place either happens.
#
# Signup (CreateMerchant) generates merchant_key/merchant_salt, the two
# secrets used for the *signed-token* auth path (see payments.auth's big
# module comment, path B) — e.g. for hosted-checkout links a merchant
# generates offline without a live session. They're returned ONCE, in the
# signup response, and never again — MerchantInfo doesn't expose them
# through any other endpoint. If a merchant loses them, the only fix given
# the current code is generating a brand-new merchant row (there's no
# "regenerate my key" endpoint). Worth keeping in mind if support ever gets
# a "lost our merchant key" request.
#
# Login (LoginMerchant) issues a Redis session token using the exact same
# scheme as payments.auth (same TOKEN_TTL_SECONDS import, same
# "merchant_{id}:{token}" Redis key format, same "<merchant_id>-<token>"
# combined token shape) — so a token issued here is immediately valid
# anywhere payments.auth.get_merchant_id_from_token() is checked. This file
# duplicates that Redis-writing logic directly (setex(...)) rather than
# calling a shared "create session" helper in payments.auth — if you ever
# change the session format, this file and payments.auth both need updating
# together, by hand.
import secrets
import re
import logging
import threading
from hashlib import sha512
from django.conf import settings
from django.db import IntegrityError, transaction
from django.core.cache import cache
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from payments.models.merchantinfo import MerchantInfo
from payments.models.merchant_email_verification import MerchantEmailVerification
from payments.auth import TOKEN_TTL_SECONDS, get_merchant_id_from_token


SUPPORTED_COURIERS = {
    "blue_dart": "Blue Dart",
    "delhivery": "Delhivery",
    "dtdc": "DTDC",
    "ekart": "Ekart",
    "shadowfax": "Shadowfax",
    "shiprocket": "Shiprocket",
    "xpressbees": "Xpressbees",
}

BLUE_DART_PLANS = {
    "basic": {"label": "Basic", "otp_enabled": False},
    "otp_enabled": {"label": "OTP Enabled", "otp_enabled": True},
    "otp_enabled_plus": {"label": "OTP Enabled Plus", "otp_enabled": True},
}

CHECKOUT_FIELD_MAPPING_KEYS = {
    "order_id",
    "amount",
    "customer_name",
    "phone",
    "email",
    "address",
    "pincode",
    "city",
    "state",
    "country",
}

REGISTER_OTP_TTL_SECONDS = 10 * 60
REGISTER_OTP_RESEND_SECONDS = 45
REGISTER_OTP_MAX_ATTEMPTS = 5
logger = logging.getLogger(__name__)


def _normalize_email(value):
    return str(value or "").strip().lower()


def _hash_otp(email, otp):
    return sha512(f"{settings.SECRET_KEY}:{email}:{otp}".encode()).hexdigest()


def _generate_numeric_otp():
    return f"{secrets.randbelow(1000000):06d}"


def _validate_registration_payload(data):
    merchant_name = str(data.get("merchant_name") or "").strip()
    merchant_email = _normalize_email(data.get("merchant_email"))
    merchant_phone = str(data.get("merchant_phone") or "").strip()
    merchant_address = str(data.get("merchant_address") or "").strip()
    merchant_password = str(data.get("merchant_password") or "")

    if not all([merchant_name, merchant_email, merchant_phone, merchant_address, merchant_password]):
        return None, "All fields are required."

    try:
        validate_email(merchant_email)
    except ValidationError:
        return None, "Enter a valid email address."

    if MerchantInfo.objects.filter(merchant_email__iexact=merchant_email).exists():
        return None, "A merchant with this email already exists."

    if not re.fullmatch(r"\d{10,15}", merchant_phone):
        return None, "Phone must contain 10 to 15 digits."

    if len(merchant_password.strip()) < 8:
        return None, "Password must be at least 8 characters long."

    return {
        "merchant_name": merchant_name,
        "merchant_email": merchant_email,
        "merchant_phone": merchant_phone,
        "merchant_address": merchant_address,
        "password_hash": sha512(merchant_password.strip().encode()).hexdigest(),
    }, None


def _create_verified_merchant(payload):
    for _ in range(10):
        try:
            with transaction.atomic():
                merchant = MerchantInfo.objects.create(
                    merchant_name=payload["merchant_name"],
                    merchant_email=payload["merchant_email"],
                    merchant_phone=payload["merchant_phone"],
                    merchant_address=payload["merchant_address"],
                    password=payload["password_hash"],
                    merchant_key=secrets.token_hex(4),
                    merchant_salt=secrets.token_hex(4),
                )
            return merchant
        except IntegrityError:
            continue
    return None


def _send_registration_otp_email(email, otp):
    try:
        send_mail(
            subject="Verify your SecurePay merchant account",
            message=(
                f"Your SecurePay verification code is {otp}.\n\n"
                "This code expires in 10 minutes. If you did not request this account, you can ignore this email."
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Unable to send merchant registration OTP to %s", email)


def _send_registration_otp_email_soon(email, otp):
    if getattr(settings, "REGISTER_OTP_EMAIL_ASYNC", True):
        thread = threading.Thread(target=_send_registration_otp_email, args=(email, otp), daemon=True)
        thread.start()
        return

    _send_registration_otp_email(email, otp)


class SendMerchantRegistrationOtp(APIView):
    def post(self, request):
        payload, error = _validate_registration_payload(request.data)
        if error:
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)

        email = payload["merchant_email"]
        latest = MerchantEmailVerification.objects.filter(
            email=email,
            purpose=MerchantEmailVerification.PURPOSE_REGISTER,
            is_used=False,
        ).first()
        if latest and latest.created_at >= timezone.now() - timezone.timedelta(seconds=REGISTER_OTP_RESEND_SECONDS):
            return Response(
                {"error": "Verification code already sent. Please wait before requesting another code."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        otp = _generate_numeric_otp()
        MerchantEmailVerification.objects.filter(
            email=email,
            purpose=MerchantEmailVerification.PURPOSE_REGISTER,
            is_used=False,
        ).update(is_used=True, used_at=timezone.now())
        verification = MerchantEmailVerification.objects.create(
            email=email,
            purpose=MerchantEmailVerification.PURPOSE_REGISTER,
            otp_hash=_hash_otp(email, otp),
            pending_payload=payload,
            expires_at=timezone.now() + timezone.timedelta(seconds=REGISTER_OTP_TTL_SECONDS),
        )

        _send_registration_otp_email_soon(email, otp)

        return Response({
            "ok": True,
            "message": "Verification code is being sent.",
            "email": email,
            "expires_in": REGISTER_OTP_TTL_SECONDS,
            "resend_after": REGISTER_OTP_RESEND_SECONDS,
            "verification_id": verification.id,
        })


class VerifyMerchantRegistrationOtp(APIView):
    def post(self, request):
        email = _normalize_email(request.data.get("merchant_email"))
        otp = str(request.data.get("otp") or "").strip()

        if not email or not otp:
            return Response({"error": "Email and verification code are required."}, status=status.HTTP_400_BAD_REQUEST)

        verification = MerchantEmailVerification.objects.filter(
            email=email,
            purpose=MerchantEmailVerification.PURPOSE_REGISTER,
            is_used=False,
        ).first()
        if not verification:
            return Response({"error": "Request a new verification code."}, status=status.HTTP_400_BAD_REQUEST)

        if verification.expires_at < timezone.now():
            verification.is_used = True
            verification.used_at = timezone.now()
            verification.save(update_fields=["is_used", "used_at"])
            return Response({"error": "Verification code expired. Request a new code."}, status=status.HTTP_400_BAD_REQUEST)

        if verification.attempt_count >= REGISTER_OTP_MAX_ATTEMPTS:
            verification.is_used = True
            verification.used_at = timezone.now()
            verification.save(update_fields=["is_used", "used_at"])
            return Response({"error": "Too many incorrect attempts. Request a new code."}, status=status.HTTP_400_BAD_REQUEST)

        # TEMPORARY: while email delivery is not set up, REGISTER_OTP_BYPASS_CODE (e.g. 123456) is accepted for any
        # email that has requested a code. Off unless that env var is set; remove it once real email works.
        bypass_code = str(getattr(settings, "REGISTER_OTP_BYPASS_CODE", "") or "").strip()
        bypass_ok = bool(bypass_code) and secrets.compare_digest(otp, bypass_code)

        if not bypass_ok and (not re.fullmatch(r"\d{6}", otp) or verification.otp_hash != _hash_otp(email, otp)):
            verification.attempt_count += 1
            verification.save(update_fields=["attempt_count"])
            return Response({"error": "Invalid verification code."}, status=status.HTTP_400_BAD_REQUEST)

        if MerchantInfo.objects.filter(merchant_email__iexact=email).exists():
            verification.is_used = True
            verification.used_at = timezone.now()
            verification.save(update_fields=["is_used", "used_at"])
            return Response({"error": "A merchant with this email already exists."}, status=status.HTTP_400_BAD_REQUEST)

        merchant = _create_verified_merchant(verification.pending_payload)
        if merchant is None:
            return Response({"error": "Could not create merchant credentials. Please retry."}, status=500)

        verification.is_used = True
        verification.used_at = timezone.now()
        verification.save(update_fields=["is_used", "used_at"])

        return Response(
            {
                "ok": True,
                "message": "Email verified. Merchant created successfully.",
                "merchant_id": merchant.id,
                "merchant_email": merchant.merchant_email,
            },
            status=status.HTTP_201_CREATED,
        )


class CreateMerchant(APIView):
    """NOTE: passes username=merchant_username, but MerchantInfo.username was
    removed by migration 0011 (it's commented out in the model now) — this
    call will raise a TypeError as written. Flagging rather than fixing since
    that's a behavior change, not a comment."""

    def post(self, request):
        merchant_name = request.data.get("merchant_name")
        merchant_email = request.data.get("merchant_email")
        merchant_phone = request.data.get("merchant_phone")
        merchant_address = request.data.get("merchant_address")
        merchant_password = request.data.get("merchant_password")

        if not all([merchant_name, merchant_email, merchant_phone, merchant_address, merchant_password]):
            return Response({"error": "All fields are required."}, status=status.HTTP_400_BAD_REQUEST)

        # merchant_key/merchant_salt are each random 8-hex-char strings
        # (generate_key/generate_salt below) and both columns are
        # unique=True on the model — an astronomically rare collision is
        # still technically possible, so this retries up to 10 times with a
        # freshly-generated key/salt pair rather than failing outright on
        # the first collision.
        for _ in range(10):
            try:
                with transaction.atomic():
                    merchant = MerchantInfo.objects.create(
                        merchant_name=merchant_name,
                        merchant_email=merchant_email,
                        merchant_phone=merchant_phone,
                        merchant_address=merchant_address,
                        password=sha512(merchant_password.encode()).hexdigest(),
                        merchant_key=self.generate_key(),
                        merchant_salt=self.generate_salt(),
                    )
                return Response(
                    {
                        "message": "Merchant created successfully.",
                        "merchant_id": merchant.id,
                        "merchant_key": merchant.merchant_key,
                        "merchant_salt": merchant.merchant_salt,
                    },
                    status=status.HTTP_201_CREATED,
                )
            except IntegrityError:
                continue

        return Response({"error": "Could not generate unique merchant credentials. Please retry."}, status=500)

    def generate_key(self) -> str:
        return secrets.token_hex(4)

    def generate_salt(self) -> str:
        return secrets.token_hex(4)


class LoginMerchant(APIView):
    """Issues a session token good for TOKEN_TTL_SECONDS, returned both in the
    JSON body (for API/JS clients) and as a cookie (for the server-rendered
    static pages in merchant/static/)."""

    def post(self, request):
        merchant_email = request.data.get("merchant_email")
        password = request.data.get("password")

        if not all([merchant_email, password]):
            return Response(
                {"error": "merchant_email and password are required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            merchant = MerchantInfo.objects.get(merchant_email=merchant_email)
        except MerchantInfo.DoesNotExist:
            return Response({"error": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        # NOTE: plain `!=` rather than a constant-time comparison
        # (secrets.compare_digest) — a theoretical timing-attack surface on
        # the password hash, though in practice the network round-trip time
        # for this HTTP request dwarfs any measurable timing difference.
        # payments.auth.verify_merchant_auth_token does use compare_digest
        # for its signature check, for contrast.
        hashed_password = sha512(password.encode()).hexdigest()
        if merchant.password != hashed_password:
            return Response({"error": "Invalid credentials."}, status=status.HTTP_401_UNAUTHORIZED)

        # Session creation: same Redis key shape
        # ("merchant_{id}:{token}" -> merchant id, TTL = TOKEN_TTL_SECONDS)
        # that payments.auth._verify_session_token_and_refresh reads back.
        session_token = secrets.token_urlsafe(32)
        combined_token = f"{merchant.id}-{session_token}"

        redis_key = f"merchant_{merchant.id}:{session_token}"
        redis_value = str(merchant.id)
        cache.set(redis_key, redis_value, TOKEN_TTL_SECONDS)

        response = Response(
            {
                "message": "Login successful.",
                "merchant_id": merchant.id,
                "merchant_key": merchant.merchant_key,
                "token": combined_token,
                "expires_in": TOKEN_TTL_SECONDS
            },
            status=status.HTTP_200_OK
        )
        response.set_cookie(
            key="token",
            value=combined_token,
            max_age=TOKEN_TTL_SECONDS,
            samesite="Lax",
            path="/",
        )
        return response


class MerchantCourierPreferences(APIView):
    """Read and save the courier setup shown after a merchant's first login."""

    def _merchant(self, request):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
        merchant_id = get_merchant_id_from_token(token)
        if not merchant_id:
            return None
        try:
            return MerchantInfo.objects.get(id=merchant_id)
        except MerchantInfo.DoesNotExist:
            return None

    def get(self, request):
        merchant = self._merchant(request)
        if merchant is None:
            return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        return Response({
            "courier_setup_completed": merchant.courier_setup_completed,
            "courier_preferences": merchant.courier_preferences or {},
        })

    def post(self, request):
        merchant = self._merchant(request)
        if merchant is None:
            return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        courier_keys = request.data.get("couriers")
        blue_dart_plan = request.data.get("blue_dart_plan")
        if not isinstance(courier_keys, list) or not courier_keys:
            return Response({"error": "Select at least one courier partner."}, status=status.HTTP_400_BAD_REQUEST)

        courier_keys = list(dict.fromkeys(courier_keys))
        unsupported = [key for key in courier_keys if key not in SUPPORTED_COURIERS]
        if unsupported:
            return Response({"error": "One or more courier partners are not supported."}, status=status.HTTP_400_BAD_REQUEST)

        if "blue_dart" in courier_keys:
            if blue_dart_plan not in BLUE_DART_PLANS:
                return Response({"error": "Choose a Blue Dart service plan."}, status=status.HTTP_400_BAD_REQUEST)
            if not BLUE_DART_PLANS[blue_dart_plan]["otp_enabled"]:
                return Response({"error": "SecurePay supports only OTP-enabled Blue Dart plans."}, status=status.HTTP_400_BAD_REQUEST)
        else:
            blue_dart_plan = None

        merchant.courier_preferences = {
            "couriers": courier_keys,
            "blue_dart_plan": blue_dart_plan,
        }
        merchant.courier_setup_completed = True
        merchant.save(update_fields=["courier_preferences", "courier_setup_completed"])
        return Response({
            "message": "Courier preferences saved.",
            "courier_setup_completed": True,
            "courier_preferences": merchant.courier_preferences,
        })


class MerchantCheckoutFieldMapping(APIView):
    """Merchant-facing setup for the one-line SecurePay button SDK."""

    def _merchant(self, request):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
        merchant_id = get_merchant_id_from_token(token)
        if not merchant_id:
            return None
        try:
            return MerchantInfo.objects.get(id=merchant_id)
        except MerchantInfo.DoesNotExist:
            return None

    def get(self, request):
        merchant = self._merchant(request)
        if merchant is None:
            return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        return Response({
            "checkout_field_mapping": merchant.checkout_field_mapping or {},
        })

    def post(self, request):
        merchant = self._merchant(request)
        if merchant is None:
            return Response({"error": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)

        incoming = request.data.get("checkout_field_mapping", request.data)
        if not isinstance(incoming, dict):
            return Response({"error": "checkout_field_mapping must be an object."}, status=status.HTTP_400_BAD_REQUEST)

        mapping = {}
        for key in CHECKOUT_FIELD_MAPPING_KEYS:
            value = incoming.get(key)
            if value is None:
                continue
            clean_value = str(value).strip()
            if clean_value:
                mapping[key] = clean_value[:160]

        merchant.checkout_field_mapping = mapping
        merchant.save(update_fields=["checkout_field_mapping"])
        return Response({
            "message": "Checkout field mapping saved.",
            "checkout_field_mapping": merchant.checkout_field_mapping,
        })
