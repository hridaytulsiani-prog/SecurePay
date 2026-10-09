import os
import re
import tempfile
from dataclasses import asdict, is_dataclass
from typing import Any

import fitz
from pypdf import PdfReader

from tracking.api.v1.label_validator import validate_label
from tracking.api.v1.label_validator.pdf_model import LabelPDF


KEYWORD_PATTERNS = [
    ("OTP", re.compile(r"\botp\b", re.IGNORECASE)),
    ("OTPBasedDelivery", re.compile(r"\botpbaseddelivery\b", re.IGNORECASE)),
    ("OTP Based Delivery", re.compile(r"\botp\s*based\s*delivery\b", re.IGNORECASE)),
    ("Dynamic OTP", re.compile(r"\bdynamic\s*otp\b", re.IGNORECASE)),
    ("Fixed OTP", re.compile(r"\bfixed\s*otp\b", re.IGNORECASE)),
    ("LastMileDynamicOTP", re.compile(r"\blastmile\s*dynamic\s*otp\b", re.IGNORECASE)),
    ("LastMileFixedOTP", re.compile(r"\blastmile\s*fixed\s*otp\b", re.IGNORECASE)),
    ("Secure Delivery", re.compile(r"\bsecure\s*delivery\b", re.IGNORECASE)),
    ("Verified Delivery", re.compile(r"\bverified\s*delivery\b", re.IGNORECASE)),
    ("OTP Code", re.compile(r"\botp\s*code\b", re.IGNORECASE)),
]

NORMALIZED_KEYWORDS = {
    "otpbaseddelivery": "OTPBasedDelivery",
    "otpbaseddeliveryservice": "OTPBasedDelivery",
    "lastmiledynamicotp": "LastMileDynamicOTP",
    "lastmilefixedotp": "LastMileFixedOTP",
    "lastmilefixedotpautogenerate": "LastMileFixedOTPAutoGenerate",
    "lastmilefixedotppredefined": "LastMileFixedOTPpredefined",
    "dynamicotp": "Dynamic OTP",
    "fixedotp": "Fixed OTP",
    "otpcode": "OTP Code",
    "securedelivery": "Secure Delivery",
    "verifieddelivery": "Verified Delivery",
}


def analyze_bluedart_label_otp_evidence(uploaded_file) -> dict[str, Any]:
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
            for chunk in uploaded_file.chunks():
                temp_pdf.write(chunk)
            temp_path = temp_pdf.name

        return analyze_bluedart_label_otp_evidence_path(temp_path, uploaded_file.name)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def analyze_bluedart_label_otp_evidence_path(path: str, file_name: str = "") -> dict[str, Any]:
    sources = _collect_sources(path)
    evidence = []

    for source_name, value in sources.items():
        if isinstance(value, list):
            text = "\n".join(str(item) for item in value)
        elif isinstance(value, dict):
            text = "\n".join(f"{key}: {child}" for key, child in value.items())
        else:
            text = str(value or "")

        evidence.extend(_find_keyword_evidence(source_name, text))

    readable_sources = [
        name
        for name in ("visible_text", "metadata", "xmp", "content_streams", "barcode_values")
        if _has_content(sources.get(name))
    ]

    if evidence:
        status = "OTP_LABEL_INDICATION_FOUND"
        result_text = "OTP indication found on label"
    elif readable_sources:
        status = "NO_OTP_LABEL_INDICATION"
        result_text = "No OTP indication found on label"
    else:
        status = "LABEL_TEXT_UNREADABLE"
        result_text = "Label text/internal content could not be read"

    return {
        "file_name": file_name,
        "status": status,
        "result": result_text,
        "matched_keyword": evidence[0]["keyword"] if evidence else "",
        "evidence": evidence[:25],
        "readable_sources": readable_sources,
        "source_counts": {
            "visible_text_chars": len(sources.get("visible_text") or ""),
            "metadata_fields": len(sources.get("metadata") or {}),
            "xmp_chars": len(sources.get("xmp") or ""),
            "content_stream_chars": len(sources.get("content_streams") or ""),
            "barcode_count": len(sources.get("barcode_values") or []),
        },
        "detected": sources.get("detected") or {},
        "metadata": sources.get("metadata") or {},
        "barcode_values": sources.get("barcode_values") or [],
        "visible_text_preview": _preview(sources.get("visible_text") or "", limit=3000),
        "internal_text_preview": _preview(sources.get("content_streams") or "", limit=3000),
        "note": (
            "No keyword found does not prove OTP is disabled. It only means the uploaded label "
            "does not expose OTP evidence in readable PDF text, metadata, content streams, or decoded barcodes."
        ),
    }


def _collect_sources(path: str) -> dict[str, Any]:
    sources: dict[str, Any] = {
        "visible_text": "",
        "metadata": {},
        "xmp": "",
        "content_streams": "",
        "barcode_values": [],
        "detected": {},
    }

    try:
        label_pdf = LabelPDF(path, render_dpi=300)
        try:
            sources["visible_text"] = label_pdf.full_text() or ""
            sources["metadata"] = label_pdf.doc_info() or {}
            xmp = label_pdf.xmp()
            sources["xmp"] = _dataclass_or_dict_to_text(xmp)
            sources["barcode_values"] = [barcode.value for barcode in label_pdf.barcodes()]
        finally:
            label_pdf.close()
    except Exception:
        pass

    if not sources["visible_text"]:
        try:
            doc = fitz.open(path)
            try:
                sources["visible_text"] = "\n".join(page.get_text() for page in doc)
                sources["metadata"] = sources["metadata"] or (doc.metadata or {})
            finally:
                doc.close()
        except Exception:
            pass

    try:
        result = validate_label(path)
        sources["detected"] = {
            "awb": result.awb,
            "delivery_partner": result.delivery_partner,
            "courier_partner": getattr(result, "courier_partner", None),
        }
        if result.raw_text and len(result.raw_text) > len(sources["visible_text"]):
            sources["visible_text"] = result.raw_text
        if result.barcodes:
            sources["barcode_values"] = sorted(set([*sources["barcode_values"], *result.barcodes]))
    except Exception:
        pass

    try:
        sources["content_streams"] = _extract_content_stream_text(path)
    except Exception:
        sources["content_streams"] = ""

    return sources


def _extract_content_stream_text(path: str, max_chars: int = 120000) -> str:
    reader = PdfReader(path)
    chunks = []

    for page in reader.pages:
        try:
            contents = page.get_contents()
        except Exception:
            contents = None
        if contents is None:
            continue
        try:
            data = contents.get_data()
        except Exception:
            continue
        chunks.append(data.decode("latin-1", errors="ignore"))
        if sum(len(chunk) for chunk in chunks) >= max_chars:
            break

    metadata = reader.metadata or {}
    for key, value in metadata.items():
        chunks.append(f"{key}: {value}")

    return "\n".join(chunks)[:max_chars]


def _find_keyword_evidence(source_name: str, text: str) -> list[dict[str, str]]:
    evidence = []
    if not text:
        return evidence

    for keyword, pattern in KEYWORD_PATTERNS:
        for match in pattern.finditer(text):
            evidence.append(
                {
                    "source": source_name,
                    "keyword": keyword,
                    "match": match.group(0),
                    "snippet": _snippet(text, match.start(), match.end()),
                }
            )

    compact = re.sub(r"[^a-z0-9]", "", text.casefold())
    for compact_keyword, label in NORMALIZED_KEYWORDS.items():
        if compact_keyword in compact and not any(item["source"] == source_name and item["keyword"] == label for item in evidence):
            evidence.append(
                {
                    "source": source_name,
                    "keyword": label,
                    "match": label,
                    "snippet": _snippet(text, 0, min(len(text), 1)),
                }
            )

    return evidence


def _snippet(text: str, start: int, end: int, radius: int = 90) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    snippet = text[left:right]
    return re.sub(r"\s+", " ", snippet).strip()


def _preview(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _has_content(value: Any) -> bool:
    if isinstance(value, (list, dict)):
        return bool(value)
    return bool(str(value or "").strip())


def _dataclass_or_dict_to_text(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return "\n".join(f"{key}: {child}" for key, child in value.items())
    return str(value or "")
