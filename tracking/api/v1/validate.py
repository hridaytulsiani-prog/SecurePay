"""Shipping-label PDF validation endpoint and forgery-risk scoring.

`PDFValidator` is the DRF view: it accepts an uploaded PDF, extracts the
courier/AWB, links it to the matching shipment/order, and runs it through
`analyze_shipping_label_pdf` to produce a forensic "is this label real"
risk score. Everything below the view is pure/stateless analysis code that
can be reused or unit-tested independently of the HTTP layer.
"""

import logging
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import fitz
from django.conf import settings
from django.utils.text import get_valid_filename
from django.db.models import Q
from PIL import Image
from rest_framework.response import Response
from rest_framework.views import APIView

from .color_parser import extract_major_colors_from_pdf
from .label_validator import validate_label
from label_bluedart_check import check_bluedart_pdf
from label_delhivery_check import check_delhivery_pdf
from label_dtdc_check import check_dtdc_pdf
from label_ekart_check import check_ekart_pdf
from label_shadowfax_check import check_shadowfax_pdf
from label_shiprocket_check import check_shiprocket_pdf
from label_xpressbees_check import check_xpressbees_pdf
from payments.auth import get_merchant_id_from_token
from payments.models import OrderInfo
from payments.models.merchantinfo import MerchantInfo
from tracking.models.trackinginfo import Shipment
from tracking.models.pdf_validation import PdfValidationRecord

try:
    from pyzbar.pyzbar import decode as zbar_decode
except Exception:
    # pyzbar depends on the system zbar library, which may not be installed
    # in every environment. Barcode decoding is treated as optional rather
    # than a hard dependency - see _decode_barcodes_from_image().
    zbar_decode = None

logger = logging.getLogger(__name__)

SUPPORTED_COURIER_NAMES = [
    "Blue Dart",
    "Delhivery",
    "DTDC",
    "Ekart",
    "Shadowfax",
    "Shiprocket",
    "Xpressbees",
]

SUPPORTED_COURIER_ALIASES = {
    "Blue Dart": ("blue dart", "bluedart"),
    "Delhivery": ("delhivery",),
    "DTDC": ("dtdc",),
    "Ekart": ("ekart",),
    "Shadowfax": ("shadowfax",),
    "Shiprocket": ("shiprocket",),
    "Xpressbees": ("xpressbees", "xpress bees"),
}

MERCHANT_COURIER_KEY_ALIASES = {
    "blue_dart": ("blue dart", "bluedart"),
    "delhivery": ("delhivery",),
    "dtdc": ("dtdc",),
    "ekart": ("ekart",),
    "shadowfax": ("shadowfax",),
    "shiprocket": ("shiprocket",),
    "xpressbees": ("xpressbees", "xpress bees"),
}


def _get_auth_token_from_request(request):
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()
    return request.GET.get("auth_token")


@dataclass
class RiskResult:
    verdict: str
    score: int
    courier_hint: Optional[str]
    awb_text: Optional[str]
    awb_barcode: Optional[str]
    reasons: List[str]
    metadata: Dict[str, Any]
    extracted_text_preview: str
    glyph_profile: Dict[str, Any]
    revision_profile: Dict[str, Any]


COURIER_AWB_PATTERNS = {
    "delhivery": [re.compile(r"\b\d{12,16}\b")],
    "bluedart": [re.compile(r"\b\d{9,11}\b")],
    "xpressbees": [re.compile(r"\b\d{12,15}\b")],
    "ecom": [re.compile(r"\b\d{10,15}\b")],
    "blue dart": [re.compile(r"\b\d{9,11}\b")],
}

SUSPICIOUS_METADATA_PATTERNS = [
    ("producer", re.compile(r"reportlab|canvas|fpdf|wkhtml|chrome|skia|libreoffice|cairo|anonymous", re.I)),
    ("creator", re.compile(r"anonymous|python|script|reportlab", re.I)),
    ("title", re.compile(r"untitled|sample|test", re.I)),
]

# Phrases that essentially only ever appear on placeholder/demo assets, never
# on a label actually being used to move a real shipment. Kept as specific
# multi-word phrases (not bare words like "sample"/"void"/"test") since those
# can legitimately appear in address text or shipment content descriptions.
FAKE_DISCLAIMER_PATTERNS = [
    re.compile(r"sample\s+only", re.I),
    re.compile(r"specimen\s+only", re.I),
    re.compile(r"demonstration\s+label", re.I),
    re.compile(r"demo\s+label", re.I),
    re.compile(r"this\s+is\s+a\s+sample\b", re.I),
    re.compile(r"placeholder\s+(document|label)", re.I),
    re.compile(r"for\s+(demo|demonstration|testing)\s+purposes", re.I),
    re.compile(r"not\s+a\s+(valid|real)\b[^.\n]{0,40}\b(shipment|document|label|awb)\b", re.I),
]

COURIER_KEYWORDS = {
    "delhivery": ["delhivery"],
    "bluedart": ["blue dart", "bluedart"],
    "xpressbees": ["xpressbees", "xpress bees"],
    "shadowfax": ["shadowfax"],
    "ecom": ["ecom express", "ecom"],
    "ekart": ["ekart"],
}

# SecurePay currently supports prepaid shipments only. Keep these patterns
# focused on payment-mode language so a normal order/customer name containing
# the letters "cod" is not rejected accidentally.
COD_LABEL_PATTERNS = (
    re.compile(r"\bcash\s+on\s+delivery\b", re.I),
    re.compile(r"\bcollect\s+on\s+delivery\b", re.I),
    re.compile(r"(?<![A-Za-z])C[.\s-]*O[.\s-]*D(?![A-Za-z])", re.I),
)


def label_indicates_cod(raw_text: str) -> bool:
    """Return whether extracted label text identifies a COD shipment."""
    return any(pattern.search(raw_text or "") for pattern in COD_LABEL_PATTERNS)


# Colors sampled from a genuine Delhivery label (grey packaging tones +
# accent red). Used as a fallback courier fingerprint when the label
# doesn't otherwise identify itself as Delhivery in text - see
# DELHIVERY_COLOR_MATCH_THRESHOLD below.
DELHIVERY_FINGERPRINT_COLORS = {
    "#394058", "#53596e", "#474d64", "#646a7d",
    "#868b9a", "#767b8c", "#e82223", "#fcdfdf",
}
SHIPROCKET_FINGERPRINT_COLORS = {
    '#a09df3', '#807cef', '#908df1', '#c1c0f6', 
    '#d1d0f8', '#e2e1fa', '#6f6bed'
}

# Minimum number of page-color hits against DELHIVERY_FINGERPRINT_COLORS
# before we're confident enough to label the courier "Delhivery" purely
# from color, rather than from text/logo detection in validate_label().
DELHIVERY_COLOR_MATCH_THRESHOLD = 4
SHIPROCKET_COLOR_MATCH_THRESHOLD = 6
DELHIVERY_COLOR_DISTANCE_TOLERANCE = 30


def _hex_to_rgb(hex_value: str) -> tuple[int, int, int]:
    value = hex_value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _is_near_fingerprint_color(hex_value: str, fingerprint_colors: set[str], tolerance: int) -> bool:
    try:
        red, green, blue = _hex_to_rgb(hex_value)
    except Exception:
        return False

    for fingerprint in fingerprint_colors:
        target_red, target_green, target_blue = _hex_to_rgb(fingerprint)
        if (
            abs(red - target_red) <= tolerance
            and abs(green - target_green) <= tolerance
            and abs(blue - target_blue) <= tolerance
        ):
            return True
    return False


class PDFValidator(APIView):
    """POST endpoint: upload a shipping-label PDF for validation.

    Pipeline:
      1. Save the upload to a temp file and run `validate_label()` to pull
         out the courier, AWB, and any barcodes it recognizes.
      2. If the courier couldn't be identified from text/logo, fall back to
         a color-fingerprint match (currently only tuned for Delhivery).
      3. Derive the merchant's order id from the label (courier-specific
         parsing, see `get_order_id`) and link the AWB to a `Shipment`,
         and that shipment to the matching `OrderInfo`.
      4. Run `analyze_shipping_label_pdf()` for a forgery/tamper risk score
         and include it in the response so suspicious labels can be
         flagged for manual review.
    """

    @staticmethod
    def _is_supported_courier(delivery_partner="", courier_partner="", raw_text=""):
        haystack = " ".join([
            delivery_partner or "",
            courier_partner or "",
            raw_text or "",
        ]).lower()
        return any(
            alias in haystack
            for aliases in SUPPORTED_COURIER_ALIASES.values()
            for alias in aliases
        )

    @staticmethod
    def _unsupported_courier_response(delivery_partner="", courier_partner=""):
        detected_courier = courier_partner if courier_partner and courier_partner != "Unknown" else delivery_partner
        if not detected_courier or detected_courier == "Unknown":
            detected_courier = "Unknown"

        return Response({
            "error": (
                "Unsupported courier service. SecurePay currently accepts shipment labels only "
                "from Blue Dart, Delhivery, DTDC, Ekart, Shadowfax, Shiprocket, and Xpressbees."
            ),
            "detected_courier": detected_courier,
            "supported_couriers": SUPPORTED_COURIER_NAMES,
        }, status=422)

    @staticmethod
    def _courier_key(value=""):
        normalized = re.sub(r"[^a-z0-9]", "", (value or "").lower())
        for key, aliases in MERCHANT_COURIER_KEY_ALIASES.items():
            if any(re.sub(r"[^a-z0-9]", "", alias) in normalized for alias in aliases):
                return key
        return None

    @classmethod
    def _configured_courier_key(cls, delivery_partner="", courier_partner=""):
        # For marketplace labels, courier_partner is the actual last-mile
        # carrier inside the outer label (for example Blue Dart inside
        # Shiprocket). That is the partner the merchant must have enabled.
        for candidate in (courier_partner, delivery_partner):
            if candidate and candidate != "Unknown":
                key = cls._courier_key(candidate)
                if key:
                    return key
        return None

    @staticmethod
    def _merchant_courier_not_configured_response(merchant_id, delivery_partner="", courier_partner=""):
        try:
            merchant = MerchantInfo.objects.get(id=merchant_id)
        except MerchantInfo.DoesNotExist:
            return None

        preferences = merchant.courier_preferences or {}
        configured_keys = preferences.get("couriers") if isinstance(preferences, dict) else []
        configured_keys = configured_keys if isinstance(configured_keys, list) else []
        detected_key = PDFValidator._configured_courier_key(delivery_partner, courier_partner)
        if not detected_key or detected_key in configured_keys:
            return None

        label = next((name for key, name in zip(
            ("blue_dart", "delhivery", "dtdc", "ekart", "shadowfax", "shiprocket", "xpressbees"),
            SUPPORTED_COURIER_NAMES,
        ) if key == detected_key), detected_key)
        configured_names = [
            name for key, name in zip(
                ("blue_dart", "delhivery", "dtdc", "ekart", "shadowfax", "shiprocket", "xpressbees"),
                SUPPORTED_COURIER_NAMES,
            ) if key in configured_keys
        ]
        selected_text = ", ".join(configured_names) if configured_names else "no courier partners"
        return Response({
            "error": (
                f"{label} label rejected. Your SecurePay account supports {selected_text}. "
                f"Add {label} in Courier Settings before uploading this label."
            ),
            "code": "COURIER_NOT_CONFIGURED",
            "detected_courier": label,
            "configured_couriers": configured_names,
        }, status=422)

    @staticmethod
    def _cod_not_supported_response():
        return Response({
            "error": (
                "Cash on Delivery (COD) shipment labels are not supported by SecurePay. "
                "Please upload a prepaid shipment label."
            ),
            "code": "COD_NOT_SUPPORTED",
        }, status=422)

    @staticmethod
    def _serialize_record(record, mask_for_merchant=True):
        status = "under_review" if mask_for_merchant else record.status
        verdict = None if mask_for_merchant else record.verdict
        risk_verdict = None if mask_for_merchant else record.risk_verdict
        return {
            "id": record.id,
            "file_name": record.file_name,
            "status": status,
            "verdict": verdict,
            "risk_verdict": risk_verdict,
            "score": 0 if mask_for_merchant else record.score,
            "risk_score": 0 if mask_for_merchant else record.risk_score,
            "delivery_partner": record.delivery_partner,
            "courier_partner": record.courier_partner,
            "awb": record.awb,
            "order_id": record.order_id,
            "uploaded_at": record.uploaded_at.isoformat(),
        }

    def get(self, request):
        auth_token = _get_auth_token_from_request(request)
        merchant_id = get_merchant_id_from_token(auth_token)
        if merchant_id is None:
            return Response({"error": "Invalid or expired merchant token"}, status=401)

        records = PdfValidationRecord.objects.filter(merchant_id=merchant_id)[:50]
        return Response({
            "results": [self._serialize_record(record) for record in records]
        })

    def post(self, request):
        auth_token = _get_auth_token_from_request(request)
        merchant_id = get_merchant_id_from_token(auth_token)
        if merchant_id is None:
            return Response({"error": "Invalid or expired merchant token"}, status=401)

        pdf_file = request.FILES.get("file")
        if not pdf_file:
            return Response({"error": "No file provided"}, status=400)

        if not pdf_file.name.lower().endswith(".pdf"):
            return Response({"error": "Only PDF files are allowed"}, status=400)

        if PdfValidationRecord.objects.filter(merchant_id=merchant_id, file_name=pdf_file.name).exists():
            return Response({"error": "This PDF is already uploaded."}, status=409)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
            for chunk in pdf_file.chunks():
                temp_pdf.write(chunk)
            temp_path = temp_pdf.name

        try:
            parsed_result = validate_label(temp_path)
            if label_indicates_cod(parsed_result.raw_text):
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
                return self._cod_not_supported_response()

            delivery_partner = parsed_result.delivery_partner
            self.from_color = False
            if not delivery_partner:
                delivery_partner, self.from_color = self._detect_courier_from_color(temp_path)
            courier_partner, _ = self.detect_courier_partner_from_color(temp_path, parsed_result.raw_text)
            if courier_partner == "Unknown" and delivery_partner:
                courier_partner = delivery_partner
            if not self._is_supported_courier(delivery_partner, courier_partner, parsed_result.raw_text):
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
                return self._unsupported_courier_response(delivery_partner, courier_partner)
            courier_settings_error = self._merchant_courier_not_configured_response(
                merchant_id, delivery_partner, courier_partner
            )
            if courier_settings_error:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
                return courier_settings_error
            parsed_order_id = self.get_order_id(
                delivery_partner,
                parsed_result.raw_text,
                parsed_result.barcodes,
                courier_partner,
            )
        except Exception:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
            return Response({"error": "This PDF could not be read as a supported shipment label."}, status=422)

        # The merchant order ID is supplied by the upload form because many
        # courier labels, especially Delhivery labels, expose the AWB as their
        # first barcode rather than the merchant's order reference. Prefer the
        # merchant-supplied value when it belongs to this merchant; otherwise
        # retain the existing courier-specific PDF extraction fallback.
        submitted_order_id = str(request.data.get("secureupi_order_id") or "").strip()
        if submitted_order_id:
            submitted_order = OrderInfo.objects.filter(merchant_id=merchant_id).filter(
                Q(merchant_order_id__iexact=submitted_order_id)
                | Q(pa_order_id__iexact=submitted_order_id)
            ).first()
            if submitted_order:
                parsed_order_id = submitted_order.merchant_order_id

        order_exists = False
        if parsed_order_id:
            order_exists = OrderInfo.objects.filter(merchant_id=merchant_id).filter(
                Q(merchant_order_id__iexact=parsed_order_id)
                | Q(pa_order_id__iexact=parsed_order_id)
            ).exists()

        if not order_exists:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
            return Response({
                "error": "This PDF is not relevant to SecurePay. No matching order ID exists for this merchant.",
                "order_id": parsed_order_id,
            }, status=422)

        record = PdfValidationRecord.objects.create(
            merchant_id=merchant_id,
            file_name=pdf_file.name,
            status="processing",
            verdict="CHECKING",
            order_id=parsed_order_id,
            details={"processing": True},
        )

        threading.Thread(
            target=self._process_pdf_record,
            args=(record.id, temp_path, merchant_id, parsed_order_id),
            daemon=True,
        ).start()

        return Response({
            "queued": True,
            "upload_record": self._serialize_record(record),
        }, status=202)

    def _process_pdf_record(
        self,
        record_id: int,
        temp_path: str,
        merchant_id: int,
        submitted_order_id: Optional[str] = None,
    ):
        try:
            result = validate_label(temp_path)
            logger.info("Label validation result: %s", result)

            delivery_partner = result.delivery_partner
            self.from_color = False
            if not delivery_partner:
                delivery_partner, self.from_color = self._detect_courier_from_color(temp_path)

            courier_partner, _ = self.detect_courier_partner_from_color(temp_path, result.raw_text)
            if courier_partner == "Unknown" and delivery_partner:
                courier_partner = delivery_partner
            if not self._is_supported_courier(delivery_partner, courier_partner, result.raw_text):
                PdfValidationRecord.objects.filter(id=record_id, merchant_id=merchant_id).update(
                    status="not_approved",
                    verdict="UNSUPPORTED COURIER",
                    delivery_partner=delivery_partner,
                    courier_partner=courier_partner,
                    details={
                        "processing": False,
                        "error": (
                            "Unsupported courier service. SecurePay currently accepts shipment labels only "
                            "from Blue Dart, Delhivery, DTDC, Ekart, Shadowfax, Shiprocket, and Xpressbees."
                        ),
                        "supported_couriers": SUPPORTED_COURIER_NAMES,
                    },
                )
                return
            order_id = submitted_order_id or self.get_order_id(
                delivery_partner,
                result.raw_text,
                result.barcodes,
                courier_partner,
            )

            analysis = analyze_shipping_label_pdf(
                temp_path,
                expected_courier=delivery_partner,
                expected_awb=result.awb,
            )
            logger.info("Risk analysis: %s", analysis)

            shipment = None
            if result.awb:
                try:
                    shipment = Shipment.objects.get(awb=result.awb)
                except Shipment.DoesNotExist:
                    shipment = Shipment(awb=result.awb, courier=delivery_partner)

                shipment.courier = delivery_partner
                shipment.courier_partner = courier_partner
                shipment.save()

            if order_id and shipment:
                order = OrderInfo.objects.filter(merchant_id=merchant_id).filter(
                    Q(merchant_order_id__iexact=order_id) | Q(pa_order_id__iexact=order_id)
                ).first()
                if order:
                    order.shipment_id = shipment
                    order.save(update_fields=["shipment_id"])

            courier_name = (delivery_partner or "").lower()
            courier_partner_name = (courier_partner or "").lower()
            forgery_result = None
            courier_checks = [
                (("blue dart", "bluedart"), check_bluedart_pdf),
                (("delhivery",), check_delhivery_pdf),
                (("xpressbees", "xpress bees"), check_xpressbees_pdf),
                (("ekart",), check_ekart_pdf),
                (("shadowfax",), check_shadowfax_pdf),
                (("dtdc",), check_dtdc_pdf),
            ]

            if "shiprocket" in courier_partner_name:
                forgery_result = check_shiprocket_pdf(temp_path)
            else:
                for names, checker in courier_checks:
                    if any(name in courier_name for name in names):
                        forgery_result = checker(temp_path)
                        break

            if forgery_result:
                approved = "PASSED" in (forgery_result.get("verdict") or "")
                forgery_verdict = forgery_result.get("verdict")
                label_score = forgery_result.get("score") or 0
                risk_score = forgery_result.get("failed_gate_count") or 0
                risk_verdict = None
            else:
                approved = result.is_authentic
                forgery_verdict = result.verdict
                label_score = result.score
                risk_score = analysis.get("score") or 0
                risk_verdict = analysis.get("verdict")

            stored_pdf = None
            if not approved:
                stored_pdf = self._store_suspicious_pdf(temp_path, merchant_id, record_id)

            details = {
                "processing": False,
                "result": result.to_dict(),
                "risk_analysis": analysis,
                "forgery_result": forgery_result,
            }
            if stored_pdf:
                details["stored_pdf"] = stored_pdf

            PdfValidationRecord.objects.filter(id=record_id, merchant_id=merchant_id).update(
                status="approved" if approved else "not_approved",
                verdict=forgery_verdict,
                risk_verdict=risk_verdict,
                score=label_score,
                risk_score=risk_score,
                delivery_partner=delivery_partner,
                courier_partner=courier_partner,
                awb=result.awb,
                order_id=order_id,
                details=details,
            )
        except Exception as exc:
            logger.exception("PDF validation failed for record %s: %s", record_id, exc)
            stored_pdf = self._store_suspicious_pdf(temp_path, merchant_id, record_id)
            details = {"processing": False, "error": "Unable to process this file."}
            if stored_pdf:
                details["stored_pdf"] = stored_pdf
            PdfValidationRecord.objects.filter(id=record_id, merchant_id=merchant_id).update(
                status="not_approved",
                verdict="PROCESSING FAILED",
                details=details,
            )
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    @staticmethod
    def _store_suspicious_pdf(temp_path: str, merchant_id: int, record_id: int) -> Optional[str]:
        if not temp_path or not os.path.exists(temp_path):
            return None

        record = PdfValidationRecord.objects.filter(id=record_id, merchant_id=merchant_id).first()
        original_name = record.file_name if record else f"record_{record_id}.pdf"
        safe_name = get_valid_filename(original_name) or f"record_{record_id}.pdf"
        relative_dir = os.path.join("suspicious_pdfs", f"merchant_{merchant_id}")
        absolute_dir = os.path.join(settings.MEDIA_ROOT, relative_dir)
        os.makedirs(absolute_dir, exist_ok=True)

        relative_path = os.path.join(relative_dir, f"{record_id}_{safe_name}")
        absolute_path = os.path.join(settings.MEDIA_ROOT, relative_path)
        shutil.copyfile(temp_path, absolute_path)
        return relative_path.replace(os.sep, "/")

    def delete(self, request):
        auth_token = _get_auth_token_from_request(request)
        merchant_id = get_merchant_id_from_token(auth_token)
        if merchant_id is None:
            return Response({"error": "Invalid or expired merchant token"}, status=401)

        record_id = request.GET.get("id") or request.data.get("id")
        if not record_id:
            return Response({"error": "PDF record id is required"}, status=400)

        record = PdfValidationRecord.objects.filter(id=record_id, merchant_id=merchant_id).first()
        if not record:
            return Response({"error": "PDF record not found"}, status=404)

        removed_awb = record.awb
        removed_order_id = record.order_id
        shipment = Shipment.objects.filter(awb=removed_awb).first() if removed_awb else None

        orders = OrderInfo.objects.filter(merchant_id=merchant_id)
        if removed_order_id:
            orders = orders.filter(Q(merchant_order_id=removed_order_id) | Q(pa_order_id=removed_order_id))
        elif shipment:
            orders = orders.filter(shipment_id=shipment)
        else:
            orders = OrderInfo.objects.none()

        unlinked_count = 0
        for order in orders:
            if order.shipment_id_id and (not shipment or order.shipment_id_id == shipment.id):
                order.shipment_id = None
                order.save(update_fields=["shipment_id"])
                unlinked_count += 1

        deleted_shipment = False
        if shipment and not OrderInfo.objects.filter(shipment_id=shipment).exists():
            shipment.delete()
            deleted_shipment = True

        stored_pdf = (record.details or {}).get("stored_pdf")
        if stored_pdf:
            stored_pdf_path = os.path.join(settings.MEDIA_ROOT, stored_pdf)
            if os.path.exists(stored_pdf_path):
                os.remove(stored_pdf_path)

        record.delete()
        return Response({
            "ok": True,
            "deleted_record_id": record_id,
            "unlinked_orders": unlinked_count,
            "deleted_shipment": deleted_shipment,
        })

    @staticmethod
    def detect_courier_partner_from_color(temp_path: str, raw_text) -> "tuple[str, bool]":
        """Fallback courier detection via dominant page colors.

        Returns (delivery_partner, matched_by_color). Only recognizes
        Delhivery today; anything below the match threshold is reported
        as "Unknown".
        """
        if 'powered by:' in raw_text.lower():
            return "Shiprocket", True
        color_result = extract_major_colors_from_pdf(temp_path)
        match_count = sum(
            1
            for page in color_result.get("all_pages", [])
            for color in page.get("colors", [])
            if color["hex"].lower() in SHIPROCKET_FINGERPRINT_COLORS
        )
        if match_count > SHIPROCKET_COLOR_MATCH_THRESHOLD:
            return "Shiprocket", True
        return "Unknown", False

    @staticmethod
    def _detect_courier_from_color(temp_path: str) -> "tuple[str, bool]":
        """Fallback courier detection via dominant page colors.

        Returns (delivery_partner, matched_by_color). Only recognizes
        Delhivery today; anything below the match threshold is reported
        as "Unknown".
        """
        color_result = extract_major_colors_from_pdf(temp_path)
        match_count = sum(
            1
            for page in color_result.get("all_pages", [])
            for color in page.get("colors", [])
            if _is_near_fingerprint_color(
                color["hex"].lower(),
                DELHIVERY_FINGERPRINT_COLORS,
                DELHIVERY_COLOR_DISTANCE_TOLERANCE,
            )
        )

        if match_count >= DELHIVERY_COLOR_MATCH_THRESHOLD:
            return "Delhivery", True
        return "Unknown", False

    def get_order_id(
        self,
        delivery_partner: str,
        raw_text: str,
        barcodes: List[str],
        courier_partner: str = "",
    ) -> Optional[str]:
        """Extract the merchant's order id from the label, courier by courier.

        Each courier lays out its order id differently - some only expose it
        as a secondary barcode, others print it as labeled text - so this is
        a plain dispatch table rather than one general-purpose pattern.
        """
        if "shiprocket" in (courier_partner or "").lower() or "shiprocket" in (raw_text or "").lower():
            patterns = [
                r"\bOrder\s*#\s*:?\s*([A-Za-z0-9][A-Za-z0-9-]{1,63})\b",
                r"\bOrder\s*(?:Id|ID|No\.?|Number)\s*#?\s*:?\s*([A-Za-z0-9][A-Za-z0-9-]{1,63})\b",
            ]
            for pattern in patterns:
                match = re.search(pattern, raw_text or "", re.IGNORECASE)
                if match:
                    return match.group(1)

        if "Delhivery" in delivery_partner:
            # Delhivery prints the merchant reference as visible text in
            # formats such as "ORDER # : abc123". This must be preferred over
            # the first barcode, which is normally the AWB.
            order_patterns = [
                r"\bOrder\s*(?:#|No\.?|ID|Number)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9_-]{1,63})\b",
                r"\bOrder\s*[:#]\s*([A-Za-z0-9][A-Za-z0-9_-]{1,63})\b",
            ]
            for pattern in order_patterns:
                match = re.search(pattern, raw_text or "", re.IGNORECASE)
                if match:
                    return match.group(1)

            # When the courier was identified via color rather than logo/text
            # (self.from_color), the first barcode tends to be a different
            # value than usual, so the order id is expected to be the second
            # barcode instead of the first.
            if self.from_color and len(barcodes) > 1:
                return barcodes[1]
            return barcodes[0] if barcodes else None

        elif delivery_partner == "Xpressbees Surface":
            return barcodes[0] if barcodes else None

        elif "Blue Dart" in delivery_partner or "Bluedart" in delivery_partner or "bluedart" in delivery_partner.lower():
            match = re.search(r"\bOrder\s*#?\s*:\s*(\d+)", raw_text, re.IGNORECASE)
            if match:
                return match.group(1)

        elif "DTDC" in delivery_partner:
            return barcodes[1] if len(barcodes) > 1 else None

        elif "SHADOWFAX" in delivery_partner:
            patterns = [
                r"Client Order Id\s*:\s*([A-Za-z0-9-]+)",
                r"Order#\s*:\s*(\d+)",
                r"Order\s*No\.?\s*[:#]?\s*([A-Za-z0-9-]+)",
            ]
            for pattern in patterns:
                match = re.search(pattern, raw_text, re.IGNORECASE)
                if match:
                    return match.group(1)

        return None


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _extract_pdf_metadata(doc: fitz.Document) -> Dict[str, Any]:
    meta = doc.metadata or {}
    return {
        "title": meta.get("title"),
        "author": meta.get("author"),
        "creator": meta.get("creator"),
        "producer": meta.get("producer"),
        "creationDate": meta.get("creationDate"),
        "modDate": meta.get("modDate"),
    }


def _extract_text(doc: fitz.Document, max_pages: int = 3) -> str:
    texts = []
    for i in range(min(len(doc), max_pages)):
        try:
            texts.append(doc.load_page(i).get_text("text"))
        except Exception:
            continue
    return _normalize_text("\n".join(texts))


def _detect_courier_from_text(text: str) -> Optional[str]:
    low = text.lower()
    for courier, keywords in COURIER_KEYWORDS.items():
        if any(k in low for k in keywords):
            return courier
    return None


def _extract_candidate_awbs(text: str) -> List[str]:
    candidates = set()
    for patterns in COURIER_AWB_PATTERNS.values():
        for pattern in patterns:
            for match in pattern.findall(text):
                candidates.add(match)
    return sorted(candidates, key=len, reverse=True)


def _extract_awb_for_courier(text: str, courier: Optional[str]) -> Optional[str]:
    courier_key = (courier or "").lower().strip()
    if courier_key in COURIER_AWB_PATTERNS:
        for pattern in COURIER_AWB_PATTERNS[courier_key]:
            m = pattern.search(text)
            if m:
                return m.group(0)

    candidates = _extract_candidate_awbs(text)
    return candidates[0] if candidates else None


def _render_first_page(doc: fitz.Document, zoom: float = 2.0) -> Image.Image:
    page = doc.load_page(0)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _decode_barcodes_from_image(img: Image.Image) -> List[str]:
    if not zbar_decode:
        return []

    decoded = []
    for item in zbar_decode(img):
        try:
            decoded.append(item.data.decode("utf-8", errors="ignore").strip())
        except Exception:
            continue
    return decoded


def _pick_likely_awb_from_barcodes(barcodes: List[str]) -> Optional[str]:
    cleaned = []
    for code in barcodes:
        val = re.sub(r"[^A-Za-z0-9]", "", code)
        if len(val) >= 8:
            cleaned.append(val)
    return max(cleaned, key=len) if cleaned else None


# --- glyph / font consistency -------------------------------------------
#
# A genuine label is drawn by one tool using a small, stable set of font
# subsets. Text patched in afterwards by a different editor tends to give
# itself away as either a font/style that doesn't match the rest of the
# page, a font subset that was re-embedded under a different tag than an
# already-used font of the "same" name, or - the most direct tell - a
# single field (e.g. the AWB run) that switches fonts partway through
# because only part of it was replaced.

FONT_SUBSET_RE = re.compile(r"^([A-Z]{6})\+(.+)$")


def _parse_font_name(font_name: str) -> "tuple[Optional[str], str]":
    # Embedded subsets are named "<6 uppercase letters>+<FontName>" per the
    # PDF spec, and that prefix changes every time a font is subsetted -
    # even when it's subsetting the exact same source font again. Two
    # different tags for what looks like the same font/style is a strong
    # signal that font was embedded by two separate tools/events.
    if not font_name:
        return None, ""
    m = FONT_SUBSET_RE.match(font_name)
    if m:
        return m.group(1), m.group(2)
    return None, font_name


def _font_key(name: str) -> str:
    # get_text('dict') reports a font's PostScript name (e.g.
    # "DMMono-Regular") while get_page_fonts() reports its full name (e.g.
    # "DM Mono Regular") for the SAME font. Stripping everything but
    # alphanumerics lets the two be joined reliably.
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _collect_font_layout(doc: fitz.Document, max_pages: int = 3) -> Dict[str, Any]:
    spans: List[Dict[str, Any]] = []
    lines: List[Dict[str, Any]] = []

    for pno in range(min(len(doc), max_pages)):
        try:
            raw = doc.load_page(pno).get_text("dict")
        except Exception:
            continue

        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                line_spans = []
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text or not text.strip():
                        continue
                    subset_tag, family = _parse_font_name(span.get("font", ""))
                    span_info = {
                        "page": pno,
                        "text": text,
                        "font": span.get("font", ""),
                        "family": family,
                        "subset_tag": subset_tag,
                        "size": round(span.get("size", 0), 1),
                        "bbox": span.get("bbox"),
                    }
                    spans.append(span_info)
                    line_spans.append(span_info)

                if line_spans:
                    lines.append({
                        "page": pno,
                        "text": _normalize_text("".join(s["text"] for s in line_spans)),
                        "spans": line_spans,
                    })

    return {"spans": spans, "lines": lines}


def _collect_embedded_font_info(doc: fitz.Document, max_pages: int = 3) -> Dict[str, Dict[str, Any]]:
    info: Dict[str, Dict[str, Any]] = {}
    for pno in range(min(len(doc), max_pages)):
        try:
            font_list = doc.get_page_fonts(pno, full=True)
        except Exception:
            continue
        for f in font_list:
            try:
                xref, ext, ftype, basefont, resource_name, encoding = f[:6]
            except Exception:
                continue
            embedded = bool(ext) and ext != "n/a"
            info[_font_key(basefont)] = {
                "embedded": embedded,
                "type": ftype,
                "encoding": encoding,
                "ext": ext,
            }
    return info


def _extract_glyph_profile(
    doc: fitz.Document,
    awb_text: Optional[str],
    max_pages: int = 3,
) -> Dict[str, Any]:
    layout = _collect_font_layout(doc, max_pages=max_pages)
    spans, lines = layout["spans"], layout["lines"]

    if not spans:
        return {
            "style_counts": {},
            "dominant_style": None,
            "duplicate_subset_styles": {},
            "non_embedded_fonts": [],
            "awb_line_text": None,
            "awb_fonts": [],
            "awb_sizes": [],
            "awb_font_matches_dominant": None,
        }

    # Usage counts and per-page subset-tag tracking, keyed by the full
    # style-qualified name (e.g. "Arial" and "Arial-Bold" stay separate -
    # a doc legitimately using both isn't suspicious on its own).
    style_counts: Dict[str, int] = {}
    subset_tags_by_page_style: Dict[int, Dict[str, set]] = {}
    for s in spans:
        style = s["family"] or "(unknown)"
        style_counts[style] = style_counts.get(style, 0) + len(s["text"])
        page_map = subset_tags_by_page_style.setdefault(s["page"], {})
        tags = page_map.setdefault(style, set())
        if s["subset_tag"]:
            tags.add(s["subset_tag"])

    dominant_style = max(style_counts, key=style_counts.get)

    duplicate_subset_styles: Dict[str, List[str]] = {}
    for pno, style_map in subset_tags_by_page_style.items():
        for style, tags in style_map.items():
            if len(tags) > 1:
                duplicate_subset_styles[f"page {pno + 1}: {style}"] = sorted(tags)

    embedded_info = _collect_embedded_font_info(doc, max_pages=max_pages)
    non_embedded_fonts = sorted({
        s["font"] for s in spans
        if _font_key(s["font"]) in embedded_info
        and not embedded_info[_font_key(s["font"])]["embedded"]
    })

    # Does the field holding the AWB number use a single consistent font?
    awb_line_text = None
    awb_fonts: List[str] = []
    awb_sizes: List[float] = []
    if awb_text:
        target = re.sub(r"\s+", "", awb_text)
        for line in lines:
            line_compact = re.sub(r"\s+", "", line["text"])
            if target and target in line_compact:
                awb_line_text = line["text"]
                awb_fonts = sorted({sp["family"] or "(unknown)" for sp in line["spans"]})
                awb_sizes = sorted({sp["size"] for sp in line["spans"]})
                break

    awb_font_matches_dominant = (
        dominant_style in awb_fonts if awb_fonts and dominant_style else None
    )

    return {
        "style_counts": style_counts,
        "dominant_style": dominant_style,
        "duplicate_subset_styles": duplicate_subset_styles,
        "non_embedded_fonts": non_embedded_fonts,
        "awb_line_text": awb_line_text,
        "awb_fonts": awb_fonts,
        "awb_sizes": awb_sizes,
        "awb_font_matches_dominant": awb_font_matches_dominant,
    }


# --- creation vs. editing source ----------------------------------------
#
# PDFs are usually saved incrementally: an edit doesn't rewrite the file,
# it appends a new revision after the previous one, each ending in its own
# "startxref <offset> %%EOF". A file that has never been touched since it
# was made has exactly one revision. A file that was reopened and resaved
# - by anything, Acrobat, a script, a stamping tool - has more than one,
# and each revision's own Info dictionary (title/producer/creator) still
# reflects whatever tool wrote *that* revision. So the very first revision
# tells you the creation source, and the latest tells you the last editing
# source - if they differ, something other than the original generator
# touched this file after it was made.

REVISION_BOUNDARY_RE = re.compile(rb"startxref\s+(\d+)\s+%%EOF", re.MULTILINE)


def _split_pdf_revisions(raw: bytes, max_revisions: int = 10) -> List[bytes]:
    # Matches on the actual spec-required boundary structure rather than a
    # bare "%%EOF" - a bare marker could coincidentally appear inside
    # unrelated binary stream data (e.g. a large embedded image) in a
    # file someone uploaded, and re-parsing every such false hit as a
    # candidate revision would be an easy way to slow this endpoint down.
    revisions = []
    for m in REVISION_BOUNDARY_RE.finditer(raw):
        revisions.append(raw[: m.end()])
        if len(revisions) >= max_revisions:
            break
    return revisions


def _extract_revision_profile(pdf_file_path: str) -> Dict[str, Any]:
    try:
        with open(pdf_file_path, "rb") as f:
            raw = f.read()
    except Exception:
        return {
            "revision_count": 1,
            "first_producer": None,
            "last_producer": None,
            "first_creator": None,
            "last_creator": None,
            "producer_changed_across_revisions": False,
            "creator_changed_across_revisions": False,
        }

    revision_chunks = _split_pdf_revisions(raw)
    revision_count = len(revision_chunks) or 1

    revision_metadata = []
    for chunk in revision_chunks:
        try:
            rdoc = fitz.open(stream=chunk, filetype="pdf")
            revision_metadata.append(_extract_pdf_metadata(rdoc))
            rdoc.close()
        except Exception:
            continue

    first_producer = first_creator = last_producer = last_creator = None
    if revision_metadata:
        first_producer = revision_metadata[0].get("producer")
        first_creator = revision_metadata[0].get("creator")
        last_producer = revision_metadata[-1].get("producer")
        last_creator = revision_metadata[-1].get("creator")

    return {
        "revision_count": revision_count,
        "first_producer": first_producer,
        "last_producer": last_producer,
        "first_creator": first_creator,
        "last_creator": last_creator,
        "producer_changed_across_revisions": bool(
            first_producer and last_producer and first_producer != last_producer
        ),
        "creator_changed_across_revisions": bool(
            first_creator and last_creator and first_creator != last_creator
        ),
    }


def _score_metadata(meta: Dict[str, Any], reasons: List[str]) -> int:
    score = 0
    hit_fields = []
    for field, pattern in SUSPICIOUS_METADATA_PATTERNS:
        value = meta.get(field) or ""
        if value and pattern.search(value):
            score += 12
            hit_fields.append(field)
            reasons.append(f"Suspicious metadata: {field}='{value}'")

    if len(hit_fields) == len(SUSPICIOUS_METADATA_PATTERNS):
        # producer + creator + title all suspicious at once looks much more
        # like bare script output than three independent coincidences
        score += 15
        reasons.append(
            "Producer, creator, and title are all suspicious simultaneously - "
            "consistent with an unedited script-generated PDF rather than a "
            "real courier export"
        )

    # Weak on its own (some legitimate tools are sparse with metadata), but
    # nearly every real PDF-writing library sets Producer automatically -
    # a completely empty one is itself a mild anomaly worth a small nudge.
    if not (meta.get("producer") or "").strip():
        score += 8
        reasons.append("Producer field is completely empty - unusual for a genuinely tool-generated PDF")

    return score


def _score_disclaimer_language(text: str, reasons: List[str]) -> int:
    score = 0
    hits = []
    for pattern in FAKE_DISCLAIMER_PATTERNS:
        m = pattern.search(text or "")
        if m:
            hits.append(m.group(0))

    if hits:
        score += 45
        reasons.append(
            f"Document text explicitly disclaims itself as a sample/demo, not a real "
            f"shipment: {hits}"
        )
    return score


def _score_text_presence(text: str, reasons: List[str]) -> int:
    score = 0
    if not text:
        score += 20
        reasons.append("No extractable text found in PDF")
        return score

    required_signals = [
        ("awb", re.compile(r"\bawb\b|\btracking\b", re.I)),
        ("address", re.compile(r"\baddress\b|\bship\s*to\b|\bdeliver\s*to\b|\bconsignee\b", re.I)),
        ("barcode text", re.compile(r"\bbarcode\b|\bqr\b", re.I)),
    ]

    hits = sum(1 for _, pattern in required_signals if pattern.search(text))
    if hits == 0:
        score += 10
        reasons.append("Text lacks common shipping-label fields")
    elif hits == 1:
        score += 5
        reasons.append("Text contains very few common shipping-label fields")
    return score


def _score_awb_consistency(courier_hint, awb_text, awb_barcode, reasons):
    score = 0

    if not awb_text:
        score += 30
        reasons.append("No AWB/tracking-like number found in text")
    if not awb_barcode:
        score += 20
        reasons.append("No barcode/QR value could be decoded")
    if awb_text and awb_barcode and awb_text not in awb_barcode and awb_barcode not in awb_text:
        score += 40
        reasons.append(f"Mismatch between visible AWB '{awb_text}' and barcode '{awb_barcode}'")

    courier_key = (courier_hint or "").lower().strip()
    if courier_key and awb_text:
        patterns = COURIER_AWB_PATTERNS.get(courier_key, [])
        if patterns and not any(p.fullmatch(awb_text) for p in patterns):
            score += 20
            reasons.append(f"AWB '{awb_text}' does not fit expected pattern for {courier_hint}")

    return score


def _score_visual_structure(doc: fitz.Document, reasons: List[str]) -> int:
    score = 0
    page = doc.load_page(0)

    if page.rect.width < 200 or page.rect.height < 200:
        score += 10
        reasons.append("Very small page dimensions for a shipment label")

    drawings = page.get_drawings()
    images = page.get_images(full=True)

    if len(images) == 0:
        score += 8
        reasons.append("No embedded images/logos found")
    if len(drawings) < 3:
        score += 5
        reasons.append("Very little drawn structure in page layout")

    return score


def _score_glyph_consistency(glyph_profile: Dict[str, Any], reasons: List[str]) -> int:
    score = 0

    duplicate_subset_styles = glyph_profile.get("duplicate_subset_styles") or {}
    if duplicate_subset_styles:
        score += 30
        for key, tags in duplicate_subset_styles.items():
            reasons.append(
                f"Font '{key}' is embedded under {len(tags)} different subsets {tags} "
                "on the same page - usually means a same-looking font was embedded "
                "by two different tools"
            )

    distinct_styles = glyph_profile.get("style_counts") or {}
    if len(distinct_styles) > 6:
        score += 8
        reasons.append(
            f"Unusually high number of distinct font styles on the label ({len(distinct_styles)})"
        )

    # Weak signal on its own - standard 14 fonts are legitimately never
    # embedded, so plenty of clean labels mix embedded and non-embedded.
    non_embedded = glyph_profile.get("non_embedded_fonts") or []
    if non_embedded and len(distinct_styles) > len(non_embedded):
        score += 8
        reasons.append(
            f"Mix of embedded and non-embedded fonts on the label: non_embedded={non_embedded}"
        )

    awb_fonts = glyph_profile.get("awb_fonts") or []
    awb_sizes = glyph_profile.get("awb_sizes") or []

    if len(awb_fonts) > 1:
        score += 35
        reasons.append(
            f"AWB text is rendered using more than one font on the same line {awb_fonts} "
            "- a strong sign the number was edited in place"
        )
    if len(awb_sizes) > 1:
        score += 10
        reasons.append(f"AWB text mixes multiple font sizes on the same line {awb_sizes}")
    if glyph_profile.get("awb_font_matches_dominant") is False:
        score += 15
        reasons.append(
            f"AWB text font {awb_fonts} differs from the label's dominant font "
            f"'{glyph_profile.get('dominant_style')}'"
        )

    return score


def _score_revision_consistency(
    revision_profile: Dict[str, Any],
    metadata: Dict[str, Any],
    reasons: List[str],
) -> int:
    score = 0

    revision_count = revision_profile.get("revision_count") or 1
    if revision_count > 1:
        score += 20
        reasons.append(
            f"PDF contains {revision_count} incremental revisions - it was reopened "
            "and resaved by some tool after it was first created"
        )

    if revision_profile.get("producer_changed_across_revisions"):
        score += 35
        reasons.append(
            f"Creation source '{revision_profile.get('first_producer')}' differs from "
            f"the final-save source '{revision_profile.get('last_producer')}' - the file "
            "was edited by a different tool than the one that created it"
        )

    if revision_profile.get("creator_changed_across_revisions"):
        score += 15
        reasons.append(
            f"Creation creator '{revision_profile.get('first_creator')}' differs from "
            f"the final-save creator '{revision_profile.get('last_creator')}'"
        )

    creation_date = metadata.get("creationDate")
    mod_date = metadata.get("modDate")
    if creation_date and mod_date and creation_date != mod_date:
        score += 10
        reasons.append(
            f"CreationDate '{creation_date}' differs from ModDate '{mod_date}' - the "
            "file was saved again after it was first created"
        )

    return score


def analyze_shipping_label_pdf(
    pdf_file_path: str,
    expected_courier: Optional[str] = None,
    expected_awb: Optional[str] = None,
) -> Dict[str, Any]:
    """Run all forgery-risk checks on a shipping-label PDF and return a RiskResult dict.

    Combines several independent, additive signals (metadata, disclaimer
    language, AWB/barcode consistency, page structure, font/glyph
    consistency, and revision history - see the `_score_*` helpers above)
    into one score, then buckets it into a verdict:
        score >= 70  -> "highly_suspicious"
        score >= 40  -> "needs_review"
        otherwise    -> "likely_valid"
    `expected_courier`/`expected_awb`, when supplied by the caller, are
    cross-checked against what's actually extracted from the PDF.
    """
    reasons: List[str] = []

    doc = fitz.open(pdf_file_path)

    metadata = _extract_pdf_metadata(doc)
    text = _extract_text(doc)
    courier_hint = _detect_courier_from_text(text) or expected_courier
    awb_text = _extract_awb_for_courier(text, courier_hint)

    try:
        page_image = _render_first_page(doc)
        decoded_values = _decode_barcodes_from_image(page_image)
    except Exception:
        decoded_values = []

    awb_barcode = _pick_likely_awb_from_barcodes(decoded_values)

    try:
        glyph_profile = _extract_glyph_profile(doc, awb_text)
    except Exception as exc:
        glyph_profile = {"error": str(exc)}

    try:
        revision_profile = _extract_revision_profile(pdf_file_path)
    except Exception as exc:
        revision_profile = {"error": str(exc)}

    score = 0
    score += _score_metadata(metadata, reasons)
    score += _score_text_presence(text, reasons)
    score += _score_disclaimer_language(text, reasons)
    score += _score_awb_consistency(courier_hint, awb_text, awb_barcode, reasons)
    score += _score_visual_structure(doc, reasons)
    score += _score_glyph_consistency(glyph_profile, reasons)
    score += _score_revision_consistency(revision_profile, metadata, reasons)

    if expected_awb:
        if awb_text != expected_awb and awb_barcode != expected_awb:
            score += 35
            reasons.append(f"Expected AWB '{expected_awb}' does not match extracted values")

    if courier_hint is None:
        score += 10
        reasons.append("Courier could not be identified from text")

    if score >= 70:
        verdict = "highly_suspicious"
    elif score >= 40:
        verdict = "needs_review"
    else:
        verdict = "likely_valid"

    result = RiskResult(
        verdict=verdict,
        score=score,
        courier_hint=courier_hint,
        awb_text=awb_text,
        awb_barcode=awb_barcode,
        reasons=reasons,
        metadata=metadata,
        extracted_text_preview=text[:1000],
        glyph_profile=glyph_profile,
        revision_profile=revision_profile,
    )
    return asdict(result)
