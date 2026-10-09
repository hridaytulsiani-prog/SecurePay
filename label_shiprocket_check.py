from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.generic import DictionaryObject, IndirectObject

from pdf_internal_segments_loader import (
    analyze_pdf as analyze_internal_pdf,
    _barcode_text_summary,
    _barcode_values_summary,
    _build_barcode_baseline,
    _build_content_grammar_baseline,
    _build_writer_behavior_baseline,
    _label_name_summary,
)

try:
    import fitz
except Exception:  # pragma: no cover
    fitz = None


PDF_OPERATOR_RE = re.compile(r"(?<!/)(?:\b([A-Za-z][A-Za-z0-9\*'\"]{0,2})\b|(['\"]))")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Shiprocket-specific PDF table-column check.")
    parser.add_argument("--real-dir", default="test_labels/shiprocket/real")
    parser.add_argument("--candidate-dir", default="test_labels/shiprocket/candidate")
    parser.add_argument("--html-out", default="output/shiprocket_label_report.html")
    parser.add_argument("--json-out", default="output/shiprocket_label_report.json")
    args = parser.parse_args()

    real_files = _pdfs_in(args.real_dir)
    candidate_files = _pdfs_in(args.candidate_dir)
    if not real_files:
        raise SystemExit("No PDF files found in Shiprocket --real-dir.")
    if not candidate_files:
        raise SystemExit("No PDF files found in Shiprocket --candidate-dir.")

    grammar_baseline = _build_content_grammar_baseline(real_files)
    barcode_baseline = _build_barcode_baseline(real_files)
    writer_baseline = _build_writer_behavior_baseline(real_files)

    real_rows = [_analyze_shiprocket(path, "real", grammar_baseline, barcode_baseline, writer_baseline) for path in real_files]
    baseline = _build_shiprocket_baseline(real_rows)
    for row in real_rows:
        row["score"] = 100
        row["verdict"] = "BASELINE REAL SAMPLE"
        row["score_breakdown"] = []
        row["notes"] = []

    candidate_rows = [
        _score_candidate(_analyze_shiprocket(path, "candidate", grammar_baseline, barcode_baseline, writer_baseline), baseline)
        for path in candidate_files
    ]

    payload = {
        "carrier": "shiprocket",
        "scoring": "Rule-based gate check. SHIPROCKET PASSED only when Barcode placement, Approved branding assets, Text positioning, and Graphics state all match a real Shiprocket sample. Any one mismatch makes the candidate SHIPROCKET SUSPICIOUS.",
        "real": real_rows,
        "candidates": candidate_rows,
    }
    _write_json(args.json_out, payload)
    _write_html(args.html_out, payload)
    _print_summary(real_rows, candidate_rows)
    return 0


def _pdfs_in(path: str) -> list[Path]:
    root = Path(path)
    return sorted(file for file in root.rglob("*.pdf") if file.is_file())


def check_shiprocket_pdf(candidate_path: str | Path, real_dir: str | Path = "test_labels/shiprocket/real") -> dict[str, Any]:
    real_files = _pdfs_in(str(real_dir))
    if not real_files:
        raise RuntimeError("No PDF files found in Shiprocket real baseline directory.")

    grammar_baseline = _build_content_grammar_baseline(real_files)
    barcode_baseline = _build_barcode_baseline(real_files)
    writer_baseline = _build_writer_behavior_baseline(real_files)
    real_rows = [_analyze_shiprocket(path, "real", grammar_baseline, barcode_baseline, writer_baseline) for path in real_files]
    baseline = _build_shiprocket_baseline(real_rows)
    return _score_candidate(_analyze_shiprocket(Path(candidate_path), "candidate", grammar_baseline, barcode_baseline, writer_baseline), baseline)


def _analyze_shiprocket(
    path: Path,
    kind: str,
    grammar_baseline: dict[str, Any],
    barcode_baseline: dict[str, Any],
    writer_baseline: dict[str, Any],
) -> dict[str, Any]:
    internal = analyze_internal_pdf(
        path,
        grammar_baseline=grammar_baseline,
        barcode_baseline=barcode_baseline,
        writer_baseline=writer_baseline,
    ).to_dict()
    stats = internal.setdefault("stats", {})
    stats["kind"] = kind
    stats.update(_extract_shiprocket_stats(path))
    internal["path"] = str(path)
    return internal


def _extract_shiprocket_stats(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    reader = PdfReader(str(path), strict=False)
    metadata = reader.metadata or {}
    page_resource = _page_resource_stats(reader)
    content = _content_stats(reader)
    fonts = _font_stats(reader)
    geometry = _fitz_geometry(path)
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    barcode_values = _safe_list_from_internal(path)

    stats: dict[str, Any] = {
        "producer": str(metadata.get("/Producer", "") or ""),
        "creator": str(metadata.get("/Creator", "") or ""),
        "metadata_keys": sorted(str(key) for key in metadata.keys()),
        "metadata_default_keys": sorted(key for key in ("author", "subject", "keywords", "trapped") if f"/{key.title()}" in metadata),
        "creation_timezone": _creation_timezone(str(metadata.get("/CreationDate", "") or "")),
        "file_size": len(data),
        "xref_entry_count": len(re.findall(rb"(?m)^\s*\d{10}\s+\d{5}\s+[nf]\s*$", data)),
        "has_unexpected_overlay": bool(re.search(rb"/Prev\b|/Subtype\s*/Form\b", data)),
        "shiprocket_literal_count": len(re.findall(rb"Shiprocket", data, re.IGNORECASE)),
        "approved_asset_hashes": page_resource["asset_hashes"],
        "approved_asset_count": len(page_resource["asset_hashes"]),
        "known_fake_font_hashes": [],
        "legacy_logo_present": page_resource["xobject_count"] > 0,
        **page_resource,
        **content,
        **fonts,
        **geometry,
    }
    stats["barcode_payload_summary"] = _barcode_payload_summary(text, barcode_values)
    return stats


def _safe_list_from_internal(path: Path) -> list[str]:
    try:
        report = analyze_internal_pdf(path).to_dict()
        return [str(item) for item in (report.get("stats") or {}).get("barcode_values") or []]
    except Exception:
        return []


def _page_resource_stats(reader: PdfReader) -> dict[str, Any]:
    has_xobject = False
    has_extgstate = False
    has_pattern = False
    xobject_count = 0
    asset_hashes: list[str] = []
    image_xobjects = 0
    form_xobjects = 0
    for page in reader.pages:
        resources = _resolve(page.get("/Resources"))
        if not isinstance(resources, DictionaryObject):
            continue
        xobjects = _resolve(resources.get("/XObject"))
        extgstate = _resolve(resources.get("/ExtGState"))
        pattern = _resolve(resources.get("/Pattern"))
        has_xobject = has_xobject or isinstance(xobjects, DictionaryObject)
        has_extgstate = has_extgstate or isinstance(extgstate, DictionaryObject)
        has_pattern = has_pattern or isinstance(pattern, DictionaryObject)
        if isinstance(xobjects, DictionaryObject):
            xobject_count += len(xobjects)
            for obj in xobjects.values():
                resolved = _resolve(obj)
                if not isinstance(resolved, DictionaryObject):
                    continue
                subtype = str(resolved.get("/Subtype") or "")
                if subtype == "/Image":
                    image_xobjects += 1
                elif subtype == "/Form":
                    form_xobjects += 1
                try:
                    raw = resolved.get_data()
                    asset_hashes.append(hashlib.sha256(raw).hexdigest()[:16])
                except Exception:
                    pass
    parts = []
    if has_xobject:
        parts.append("/XObject")
    if has_extgstate:
        parts.append("/ExtGState")
    if has_pattern:
        parts.append("/Pattern")
    return {
        "page_resources_summary": ", ".join(parts) if parts else "none",
        "has_xobject_resource": has_xobject,
        "has_extgstate_resource": has_extgstate,
        "has_pattern_resource": has_pattern,
        "xobject_count": xobject_count,
        "image_xobject_count": image_xobjects,
        "form_xobject_count": form_xobjects,
        "asset_hashes": sorted(set(asset_hashes)),
    }


def _content_stats(reader: PdfReader) -> dict[str, Any]:
    raw_parts = []
    ops: dict[str, int] = {}
    stream_sizes: list[int] = []
    for page in reader.pages:
        try:
            contents = page.get_contents()
            raw = contents.get_data() if contents is not None else b""
        except Exception:
            raw = b""
        raw_parts.append(raw)
        stream_sizes.append(len(raw))
        text = raw.decode("latin-1", errors="ignore")
        for match in PDF_OPERATOR_RE.finditer(text):
            op = match.group(1) or match.group(2) or ""
            ops[op] = ops.get(op, 0) + 1
    raw_text = b"\n".join(raw_parts).decode("latin-1", errors="ignore")
    rect_count = ops.get("re", 0)
    line_count = ops.get("l", 0)
    f_count = ops.get("f", 0) + ops.get("F", 0)
    f_star_count = ops.get("f*", 0)
    return {
        "content_stream_size": sum(stream_sizes),
        "operation_count": sum(ops.values()),
        "operator_counts": dict(sorted(ops.items())),
        "tj_count": ops.get("Tj", 0),
        "tj_array_count": ops.get("TJ", 0),
        "td_count": ops.get("Td", 0),
        "tl_count": ops.get("TL", 0),
        "t_star_count": ops.get("T*", 0),
        "fill_f_count": f_count,
        "fill_f_star_count": f_star_count,
        "graphics_state_summary": _graphics_state_summary(ops),
        "rect_count": rect_count,
        "line_count": line_count,
        "border_summary": _border_summary(rect_count, line_count, f_count, f_star_count),
        "blue_exterior_strip_count": len(re.findall(r"0(?:\.\d+)?\s+0(?:\.\d+)?\s+1(?:\.0+)?\s+rg", raw_text)),
        "known_fake_barcode_geometry": _known_fake_barcode_geometry(raw_text),
        "barcode_representation": _barcode_representation(ops, rect_count),
    }


def _font_stats(reader: PdfReader) -> dict[str, Any]:
    fonts: list[str] = []
    raw_fonts: list[str] = []
    redundant_names = 0
    fake_subsets: list[str] = []
    for page in reader.pages:
        resources = _resolve(page.get("/Resources"))
        if not isinstance(resources, DictionaryObject):
            continue
        font_dict = _resolve(resources.get("/Font"))
        if not isinstance(font_dict, DictionaryObject):
            continue
        for key, value in font_dict.items():
            font = _resolve(value)
            if not isinstance(font, DictionaryObject):
                continue
            raw = str(font.get("/BaseFont") or key).lstrip("/")
            raw_fonts.append(raw)
            base = raw.split("+", 1)[1] if "+" in raw else raw
            fonts.append(base)
            if "/Name" in font:
                redundant_names += 1
            if re.match(r"^[A-Z]{6}\+Arial", raw):
                fake_subsets.append(raw)
    return {
        "font_families": sorted(set(fonts)),
        "raw_font_names": sorted(set(raw_fonts)),
        "fake_font_subsets": sorted(set(fake_subsets)),
        "font_redundant_name_entries": redundant_names,
        "legacy_font_set": ", ".join(sorted(set(fonts))) or "-",
    }


def _fitz_geometry(path: Path) -> dict[str, Any]:
    if fitz is None:
        return {
            "barcode_placement": "not checked; PyMuPDF unavailable",
            "text_colour_summary": "not checked; PyMuPDF unavailable",
        }
    try:
        doc = fitz.open(str(path))
    except Exception:
        return {"barcode_placement": "not checked; open failed", "text_colour_summary": "not checked; open failed"}
    colors: dict[str, int] = {}
    barcode_placement = "not located"
    for page in doc:
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    color = int(span.get("color", 0))
                    key = f"#{color:06x}"
                    colors[key] = colors.get(key, 0) + 1
        drawings = page.get_drawings()
        skinny = [item for item in drawings if _is_skinny_rect(item.get("rect"))]
        if len(skinny) >= 20:
            barcode_placement = "vector barcode-like bars found"
    if colors:
        top = sorted(colors.items(), key=lambda item: item[1], reverse=True)[:4]
        text_colour_summary = ", ".join(f"{color}:{count}" for color, count in top)
    else:
        text_colour_summary = "-"
    return {"barcode_placement": barcode_placement, "text_colour_summary": text_colour_summary}


def _is_skinny_rect(rect: Any) -> bool:
    if rect is None:
        return False
    try:
        width = float(rect.width)
        height = float(rect.height)
    except Exception:
        return False
    return height > 20 and 0 < width <= 3


def _resolve(value: Any) -> Any:
    while isinstance(value, IndirectObject):
        value = value.get_object()
    return value


def _creation_timezone(value: str) -> str:
    match = re.search(r"([+-]\d{2}'?\d{2}'?|Z|UTC)$", value)
    return match.group(1) if match else "-"


def _graphics_state_summary(ops: dict[str, int]) -> str:
    gs = ops.get("gs", 0)
    j_op = ops.get("J", 0)
    d_op = ops.get("d", 0)
    if gs == 0 and j_op == 0 and d_op == 0:
        return "absent"
    return f"gs={gs}, J={j_op}, d={d_op}"


def _border_summary(rect_count: int, line_count: int, f_count: int, f_star_count: int) -> str:
    if rect_count and f_count and not line_count:
        return "filled rectangles"
    if line_count:
        return "line objects"
    if f_star_count:
        return "star-fill drawing"
    return "-"


def _barcode_representation(ops: dict[str, int], rect_count: int) -> str:
    image_ops = ops.get("Do", 0) + ops.get("BI", 0)
    if image_ops:
        return f"image/object based; image_ops={image_ops}"
    if rect_count >= 20:
        return f"vector rectangles; rects={rect_count}"
    return "not clear"


def _known_fake_barcode_geometry(raw_text: str) -> str:
    if len(re.findall(r"\bre\b", raw_text)) >= 20 and re.search(r"173\.5|273\.5|312\.5|347\.5", raw_text):
        return "known fake-like barcode bars"
    return "absent"


def _barcode_payload_summary(text: str, barcode_values: list[str]) -> str:
    awb = _extract_awb(text, barcode_values)
    if not barcode_values:
        return f"no decoded barcode; printed_awb={awb or '-'}"
    matches = [value for value in barcode_values if awb and _norm(value) == _norm(awb)]
    if matches:
        return f"decoded matches printed AWB {awb}"
    return f"decoded={', '.join(barcode_values[:4])}; printed_awb={awb or '-'}"


def _extract_awb(text: str, barcode_values: list[str]) -> str:
    for pattern in (r"AWB\s*#?\s*:?\s*([A-Za-z0-9]+)", r"(?:Tracking|Waybill)\s*(?:No\.?|Number)?\s*#?\s*:?\s*([A-Za-z0-9]+)"):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    values = [value for value in barcode_values if re.fullmatch(r"[A-Za-z0-9]+", value or "")]
    return max(values, key=len) if values else ""


def _norm(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def _build_shiprocket_baseline(real_rows: list[dict[str, Any]]) -> dict[str, Any]:
    stats_rows = [row.get("stats") or {} for row in real_rows]
    return {
        "samples": len(stats_rows),
        "allowed": {
            key: sorted({_stable_value(stats.get(key)) for stats in stats_rows})
            for key in (
                "producer",
                "creator",
                "page_resources_summary",
                "barcode_representation",
                "barcode_placement",
                "barcode_payload_summary",
                "has_pattern_resource",
                "graphics_state_summary",
                "text_colour_summary",
                "border_summary",
                "known_fake_barcode_geometry",
                "has_unexpected_overlay",
            )
        },
        "sets": {
            "asset_hashes": sorted({item for stats in stats_rows for item in stats.get("asset_hashes") or []}),
            "font_families": sorted({item for stats in stats_rows for item in stats.get("font_families") or []}),
            "raw_font_names": sorted({item for stats in stats_rows for item in stats.get("raw_font_names") or []}),
        },
        "ranges": {
            key: _range(stats_rows, key)
            for key in (
                "tj_count",
                "tj_array_count",
                "td_count",
                "tl_count",
                "t_star_count",
                "fill_f_count",
                "fill_f_star_count",
                "rect_count",
                "line_count",
                "blue_exterior_strip_count",
                "content_stream_size",
                "operation_count",
                "xref_entry_count",
                "file_size",
                "font_redundant_name_entries",
                "xobject_count",
                "barcode_count",
            )
        },
    }


def _range(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    values = [int(row.get(key, 0) or 0) for row in rows]
    return {"min": min(values) if values else 0, "max": max(values) if values else 0}


def _score_candidate(row: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    breakdown: list[dict[str, Any]] = []
    notes: list[str] = []
    stats = row.get("stats") or {}

    def add(penalty: int, check: str, detail: str, gate: bool = False) -> None:
        failed = bool(gate and penalty)
        breakdown.append({"check": check, "gate": gate, "passed": not failed, "delta": -1 if failed else 0, "detail": detail})
        if detail:
            notes.append(detail)

    _score_allowed(add, baseline, stats, "Barcode placement", "barcode_placement", 1, gate=True)
    _score_assets(add, baseline, stats, gate=True)
    _score_range(add, baseline, stats, "Text positioning", "td_count", 1, tolerance=2, gate=True)
    _score_graphics_state(add, baseline, stats, gate=True)

    failed_gate_count = sum(1 for item in breakdown if item.get("gate") and not item.get("passed"))
    passed = failed_gate_count == 0
    row["score"] = 100 if passed else 0
    row["verdict"] = "SHIPROCKET PASSED" if passed else "SHIPROCKET SUSPICIOUS"
    row["failed_gate_count"] = failed_gate_count
    row["score_breakdown"] = breakdown
    row["notes"] = notes
    return row


def _score_allowed(add, baseline: dict[str, Any], stats: dict[str, Any], label: str, key: str, penalty: int, gate: bool = False) -> None:
    value = _stable_value(stats.get(key))
    allowed = set(baseline["allowed"].get(key) or [])
    if value in allowed:
        add(0, label, f"{label} matches real profile: {value}.", gate=gate)
    else:
        add(penalty, label, f"{label} differs from real profile: got {value}, real {', '.join(sorted(allowed)) or '-'}.", gate=gate)


def _score_range(add, baseline: dict[str, Any], stats: dict[str, Any], label: str, key: str, penalty: int, tolerance: int = 0, gate: bool = False) -> None:
    value = int(stats.get(key, 0) or 0)
    bounds = baseline["ranges"].get(key) or {"min": 0, "max": 0}
    low = int(bounds["min"])
    high = int(bounds["max"])
    if low - tolerance <= value <= high + tolerance:
        add(0, label, f"{label} is within real range: got {value}, real {low}..{high}.", gate=gate)
    else:
        add(penalty, label, f"{label} is outside real range: got {value}, real {low}..{high}.", gate=gate)


def _score_set_overlap(add, baseline: dict[str, Any], stats: dict[str, Any], label: str, key: str, penalty: int) -> None:
    values = set(stats.get(key) or [])
    real = set(baseline["sets"].get(key) or [])
    if values and values <= real:
        add(0, label, f"{label} matches real set: {', '.join(sorted(values))}.")
    else:
        add(penalty, label, f"{label} differs from real set: got {', '.join(sorted(values)) or '-'}, real {', '.join(sorted(real)) or '-'}.")


def _score_assets(add, baseline: dict[str, Any], stats: dict[str, Any], gate: bool = False) -> None:
    values = set(stats.get("asset_hashes") or [])
    real = set(baseline["sets"].get("asset_hashes") or [])
    if values and values & real:
        add(0, "Approved branding assets", "At least one image/logo asset hash matches a real sample.", gate=gate)
    else:
        add(1, "Approved branding assets", "No image/logo asset hash matches the real Shiprocket samples.", gate=gate)


def _score_presence(add, label: str, values: list[str], penalty: int, detail: str) -> None:
    if values:
        add(penalty, label, f"{detail}: {', '.join(values)}.")
    else:
        add(0, label, f"{label} absent.")


def _score_text_method(add, baseline: dict[str, Any], stats: dict[str, Any]) -> None:
    tj = int(stats.get("tj_count", 0) or 0)
    tj_array = int(stats.get("tj_array_count", 0) or 0)
    real_tj = baseline["ranges"].get("tj_count") or {"min": 0, "max": 0}
    real_tj_array = baseline["ranges"].get("tj_array_count") or {"min": 0, "max": 0}
    if real_tj["min"] <= tj <= real_tj["max"] and real_tj_array["min"] <= tj_array <= real_tj_array["max"]:
        add(0, "Text-showing method", f"Tj/TJ matches real range: Tj={tj}, TJ={tj_array}.")
    else:
        add(12, "Text-showing method", f"Tj/TJ differs from real range: got Tj={tj}, TJ={tj_array}; real Tj={real_tj['min']}..{real_tj['max']}, TJ={real_tj_array['min']}..{real_tj_array['max']}.")


def _score_reportlab_ops(add, stats: dict[str, Any]) -> None:
    tl = int(stats.get("tl_count", 0) or 0)
    t_star = int(stats.get("t_star_count", 0) or 0)
    if tl > 20 or t_star > 20:
        add(10, "ReportLab text operators", f"ReportLab-style TL/T* operators are high: TL={tl}, T*={t_star}.")
    else:
        add(0, "ReportLab text operators", f"ReportLab-style TL/T* operators are not high: TL={tl}, T*={t_star}.")


def _score_graphics_state(add, baseline: dict[str, Any], stats: dict[str, Any], gate: bool = False) -> None:
    value = stats.get("graphics_state_summary") or "absent"
    allowed = set(baseline["allowed"].get("graphics_state_summary") or [])
    has_real_graphics_state = any(item != "absent" for item in allowed)
    if value in allowed:
        add(0, "Graphics state", f"Graphics state matches real profile: {value}.", gate=gate)
    elif has_real_graphics_state and value != "absent":
        add(0, "Graphics state", f"Graphics state is present like real Shiprocket profiles: {value}.", gate=gate)
    else:
        add(1, "Graphics state", f"Graphics state differs from real profile: got {value}, real {', '.join(sorted(allowed)) or '-'}.", gate=gate)


def _score_fill(add, baseline: dict[str, Any], stats: dict[str, Any]) -> None:
    f_count = int(stats.get("fill_f_count", 0) or 0)
    f_star = int(stats.get("fill_f_star_count", 0) or 0)
    real_f = baseline["ranges"].get("fill_f_count") or {"min": 0, "max": 0}
    real_f_star = baseline["ranges"].get("fill_f_star_count") or {"min": 0, "max": 0}
    if real_f["min"] <= f_count <= real_f["max"] and real_f_star["min"] <= f_star <= real_f_star["max"]:
        add(0, "Fill operator", f"Fill operators match real range: f={f_count}, f*={f_star}.")
    else:
        add(8, "Fill operator", f"Fill operators differ from real range: got f={f_count}, f*={f_star}; real f={real_f['min']}..{real_f['max']}, f*={real_f_star['min']}..{real_f_star['max']}.")


def _score_extractable_branding(add, stats: dict[str, Any]) -> None:
    count = int(stats.get("shiprocket_literal_count", 0) or 0)
    if count:
        add(5, "Extractable branding", f"Literal Shiprocket text is extractable {count} time(s).")
    else:
        add(0, "Extractable branding", "No complete literal Shiprocket strings found in raw content.")


def _score_legacy_logo(add, stats: dict[str, Any]) -> None:
    if stats.get("legacy_logo_present"):
        add(0, "Legacy logo", "XObject/logo-like asset is present.")
    else:
        add(10, "Legacy logo", "No XObject/logo-like asset is present.")


def _score_legacy_text(add, baseline: dict[str, Any], stats: dict[str, Any]) -> None:
    tj = int(stats.get("tj_count", 0) or 0)
    tj_array = int(stats.get("tj_array_count", 0) or 0)
    real_tj = baseline["ranges"].get("tj_count") or {"min": 0, "max": 0}
    real_tj_array = baseline["ranges"].get("tj_array_count") or {"min": 0, "max": 0}
    if tj_array > 0 and tj == 0:
        add(0, "Legacy text grammar", f"Legacy-style TJ arrays present and line-level Tj absent: TJ={tj_array}, Tj={tj}.")
    elif real_tj["min"] <= tj <= real_tj["max"] and real_tj_array["min"] <= tj_array <= real_tj_array["max"]:
        add(0, "Legacy text grammar", f"Text grammar is within real range: Tj={tj}, TJ={tj_array}.")
    else:
        add(10, "Legacy text grammar", f"Legacy text grammar differs from real range: got Tj={tj}, TJ={tj_array}; real Tj={real_tj['min']}..{real_tj['max']}, TJ={real_tj_array['min']}..{real_tj_array['max']}.")


def _score_metadata_defaults(add, stats: dict[str, Any]) -> None:
    keys = stats.get("metadata_default_keys") or []
    if keys:
        add(6, "Metadata defaults", f"Default metadata fields present: {', '.join(keys)}.")
    else:
        add(0, "Metadata defaults", "No ReportLab-style default metadata fields found.")


def _write_json(path: str, payload: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_html(path: str, payload: dict[str, Any]) -> None:
    rows = [_row_html(row) for row in payload["real"]] + [_row_html(row) for row in payload["candidates"]]
    columns = [
        "File",
        "Label name",
        "Verdict",
        "Score",
        "Barcode placement",
        "Approved branding assets",
        "Text positioning",
        "Graphics state",
        "Findings",
    ]
    headers = "".join(f"<th>{_esc(column)}</th>" for column in columns)
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shiprocket PDF Label Report</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2937; background: #f8fafc; }}
.table-wrap {{ overflow-x: auto; border: 1px solid #dbe2ea; background: white; }}
table {{ min-width: 1300px; width: 100%; border-collapse: collapse; background: white; }}
th, td {{ border: 1px solid #dbe2ea; padding: 8px; vertical-align: top; text-align: left; font-size: 12px; line-height: 1.35; }}
th {{ background: #eef2f7; position: sticky; top: 0; }}
code {{ font-family: Consolas, monospace; font-size: 11px; word-break: break-all; }}
.verdict {{ font-weight: 700; white-space: nowrap; }}
.match {{ color: #166534; }}
.partial {{ color: #a16207; }}
.suspicious {{ color: #b91c1c; }}
.finding {{ display: block; margin-bottom: 4px; }}
.sev-high {{ color: #b91c1c; font-weight: 700; }}
.sev-medium {{ color: #a16207; font-weight: 700; }}
.sev-low {{ color: #166534; font-weight: 700; }}
</style>
</head>
<body>
<div class="table-wrap">
<table>
<thead><tr>{headers}</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</div>
</body>
</html>
"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


def _row_html(row: dict[str, Any]) -> str:
    stats = row.get("stats") or {}
    cells = [
        Path(row["path"]).name,
        _label_name_summary(stats, "shiprocket"),
        f"<span class=\"verdict {_verdict_class(row.get('verdict', ''))}\">{_esc(row.get('verdict', '-'))}</span>",
        str(row.get("score", "-")),
        _cell(stats.get("barcode_placement") or "-", row, "Barcode placement"),
        _cell(_asset_summary(stats), row, "Approved branding assets"),
        _cell(f"Td={stats.get('td_count', 0)}", row, "Text positioning"),
        _cell(stats.get("graphics_state_summary") or "-", row, "Graphics state"),
        _findings_html(row),
    ]
    return "<tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr>"


def _cell(value: Any, row: dict[str, Any], check: str) -> str:
    return f"{_esc(value)}<br>{_esc(_breakdown_detail(row, check))}"


def _asset_summary(stats: dict[str, Any]) -> str:
    hashes = stats.get("asset_hashes") or []
    if not hashes:
        return "none"
    shown = ", ".join(hashes[:5])
    if len(hashes) > 5:
        shown += f", +{len(hashes) - 5} more"
    return shown


def _breakdown_detail(row: dict[str, Any], check: str) -> str:
    details = []
    for item in row.get("score_breakdown") or []:
        if item.get("check") == check:
            detail = item.get("detail") or "-"
            if item.get("gate"):
                status = "PASS" if item.get("passed") else "FAIL"
                details.append(f"{status}; {detail}")
            else:
                details.append(f"INFO; {detail}")
    return " | ".join(details) if details else "-"


def _findings_html(row: dict[str, Any]) -> str:
    failures = [
        item
        for item in row.get("score_breakdown") or []
        if item.get("gate") and item.get("passed") is False
    ]
    if not failures:
        return '<span class="finding">No gate mismatches found.</span>'
    parts = []
    for item in failures:
        parts.append(
            f"<span class=\"finding\"><span class=\"sev-high\">FAIL</span> "
            f"{_esc(item.get('check') or '-')}: {_esc(item.get('detail') or '-')}</span>"
        )
    return "".join(parts)


def _print_summary(real_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]]) -> None:
    print("SHIPROCKET REAL BASELINE")
    print(f"samples={len(real_rows)}")
    print("scoring=rule gates; Barcode placement, Approved branding assets, Text positioning, or Graphics state mismatch makes candidate SHIPROCKET SUSPICIOUS")
    print("")
    print("REAL SAMPLES")
    for row in real_rows:
        stats = row.get("stats") or {}
        print(f"{Path(row['path']).name}: score=100 producer={stats.get('producer') or '-'} resources={stats.get('page_resources_summary') or '-'} barcodes={stats.get('barcode_count', 0)}")
    print("")
    print("CANDIDATES")
    for row in candidate_rows:
        failed = int(row.get("failed_gate_count", 0) or 0)
        print(f"{Path(row['path']).name}: {row['verdict']} score={row['score']} failed_gates={failed}")
        for note in row.get("notes", [])[:12]:
            print(f"  - {note}")
        print("")


def _verdict_class(verdict: str) -> str:
    text = verdict.lower()
    if "suspicious" in text:
        return "suspicious"
    if "partial" in text:
        return "partial"
    return "match"


def _esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _stable_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if value == "gs=0, J=0, d=0":
        return "absent"
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
