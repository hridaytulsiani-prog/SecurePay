from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

try:
    import pdf_internal_segments_check as pdf_internal_segments_check
except ModuleNotFoundError:
    pyc_path = Path(__file__).with_name("__pycache__") / f"pdf_internal_segments_check.cpython-{sys.version_info.major}{sys.version_info.minor}.pyc"
    spec = importlib.util.spec_from_file_location("pdf_internal_segments_check", pyc_path)
    pdf_internal_segments_check = importlib.util.module_from_spec(spec)
    sys.modules["pdf_internal_segments_check"] = pdf_internal_segments_check
    spec.loader.exec_module(pdf_internal_segments_check)

analyze_pdf = pdf_internal_segments_check.analyze_pdf
_barcode_text_summary = pdf_internal_segments_check._barcode_text_summary
_barcode_values_summary = pdf_internal_segments_check._barcode_values_summary
_build_barcode_baseline = pdf_internal_segments_check._build_barcode_baseline
_build_content_grammar_baseline = pdf_internal_segments_check._build_content_grammar_baseline
_build_writer_behavior_baseline = pdf_internal_segments_check._build_writer_behavior_baseline
_document_id_detail = pdf_internal_segments_check._document_id_detail
_label_name_summary = pdf_internal_segments_check._label_name_summary
_object_serialization_detail = pdf_internal_segments_check._object_serialization_detail

if hasattr(pdf_internal_segments_check, "_build_delhivery_specific_baseline"):
    _build_delhivery_specific_baseline = pdf_internal_segments_check._build_delhivery_specific_baseline
