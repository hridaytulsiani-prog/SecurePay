from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from label_shiprocket_check import _extract_shiprocket_stats as _extract_visual_pdf_stats
from pdf_internal_segments_loader import (
    analyze_pdf as analyze_internal_pdf,
    _build_barcode_baseline,
    _build_content_grammar_baseline,
    _build_writer_behavior_baseline,
    _label_name_summary,
    _object_serialization_detail,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a DTDC-specific PDF forgery table check.")
    parser.add_argument("--real-dir", default="test_labels/dtdc/real")
    parser.add_argument("--candidate-dir", default="test_labels/dtdc/candidate")
    parser.add_argument("--html-out", default="output/dtdc_label_report.html")
    parser.add_argument("--json-out", default="output/dtdc_label_report.json")
    args = parser.parse_args()

    real_files = _pdfs_in(args.real_dir)
    candidate_files = _pdfs_in(args.candidate_dir)
    if not real_files:
        raise SystemExit("No PDF files found in DTDC --real-dir.")
    if not candidate_files:
        raise SystemExit("No PDF files found in DTDC --candidate-dir.")

    grammar_baseline = _build_content_grammar_baseline(real_files)
    barcode_baseline = _build_barcode_baseline(real_files)
    writer_baseline = _build_writer_behavior_baseline(real_files)

    real_rows = [_internal_report(path, "real", grammar_baseline, barcode_baseline, writer_baseline) for path in real_files]
    candidate_rows = [
        _score_candidate(path, real_rows, grammar_baseline, barcode_baseline, writer_baseline)
        for path in candidate_files
    ]

    payload = {
        "carrier": "dtdc",
        "scoring": "Rule-based gate check. DTDC PASSED only when Page compression, Text positioning, and TL operators all match a real DTDC sample. Any one mismatch in those hard gates makes the candidate DTDC SUSPICIOUS. Score is 100 for pass and 0 for suspicious. Other columns are informational.",
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


def check_dtdc_pdf(candidate_path: str | Path, real_dir: str | Path = "test_labels/dtdc/real") -> dict[str, Any]:
    real_files = _pdfs_in(str(real_dir))
    if not real_files:
        raise RuntimeError("No PDF files found in DTDC real baseline directory.")

    grammar_baseline = _build_content_grammar_baseline(real_files)
    barcode_baseline = _build_barcode_baseline(real_files)
    writer_baseline = _build_writer_behavior_baseline(real_files)
    real_rows = [_internal_report(path, "real", grammar_baseline, barcode_baseline, writer_baseline) for path in real_files]
    return _score_candidate(Path(candidate_path), real_rows, grammar_baseline, barcode_baseline, writer_baseline)


def _internal_report(
    path: Path,
    kind: str,
    grammar_baseline: dict[str, Any],
    barcode_baseline: dict[str, Any],
    writer_baseline: dict[str, Any],
) -> dict[str, Any]:
    report = analyze_internal_pdf(
        path,
        grammar_baseline=grammar_baseline,
        barcode_baseline=barcode_baseline,
        writer_baseline=writer_baseline,
    ).to_dict()
    report["path"] = str(path)
    stats = report.setdefault("stats", {})
    stats["kind"] = kind
    stats.update(_extract_visual_pdf_stats(path))
    if kind == "real":
        report["score"] = 100
        report["verdict"] = "BASELINE REAL SAMPLE"
        report["score_breakdown"] = []
        report["notes"] = []
    return report


def _score_candidate(
    path: Path,
    real_rows: list[dict[str, Any]],
    grammar_baseline: dict[str, Any],
    barcode_baseline: dict[str, Any],
    writer_baseline: dict[str, Any],
) -> dict[str, Any]:
    report = _internal_report(path, "candidate", grammar_baseline, barcode_baseline, writer_baseline)
    best = None
    for real in real_rows:
        scored = _score_against_real(report, real)
        if best is None or scored["failed_gate_count"] < best["failed_gate_count"]:
            best = scored
    if best is None:
        raise RuntimeError("No DTDC real samples were available for candidate scoring.")
    return best


def _score_against_real(report: dict[str, Any], real: dict[str, Any]) -> dict[str, Any]:
    stats = report.get("stats") or {}
    real_stats = real.get("stats") or {}
    breakdown: list[dict[str, Any]] = []
    notes: list[str] = []

    def add(failed: bool, check: str, detail: str, gate: bool = False) -> None:
        gate_failed = bool(gate and failed)
        breakdown.append({"check": check, "gate": gate, "passed": not gate_failed, "delta": -1 if gate_failed else 0, "detail": detail})
        if gate_failed and detail:
            notes.append(detail)

    _score_value(add, "PDF header", stats.get("pdf_header") or "-", real_stats.get("pdf_header") or "-")
    _score_pair(
        add,
        "EOF",
        (int(stats.get("eof_count", 0) or 0), stats.get("bytes_after_final_eof")),
        (int(real_stats.get("eof_count", 0) or 0), real_stats.get("bytes_after_final_eof")),
    )
    _score_xobjects(add, stats, real_stats)
    _score_value(
        add,
        "Image ops",
        int(stats.get("image_draw_operator_count", 0) or 0) + int(stats.get("inline_image_operator_count", 0) or 0),
        int(real_stats.get("image_draw_operator_count", 0) or 0) + int(real_stats.get("inline_image_operator_count", 0) or 0),
    )
    _score_pair(
        add,
        "TJ values",
        (int(stats.get("t_func_operator_count", 0) or 0), int(stats.get("t_array_operator_count", 0) or 0)),
        (int(real_stats.get("t_func_operator_count", 0) or 0), int(real_stats.get("t_array_operator_count", 0) or 0)),
    )
    _score_value(add, "TD/Tf/TJ values", int(stats.get("td_tf_tj_patterns", 0) or 0), int(real_stats.get("td_tf_tj_patterns", 0) or 0))
    _score_value(add, "Object serialization", stats.get("object_serialization") or "-", real_stats.get("object_serialization") or "-")
    _score_value(add, "Page compression", stats.get("page_content_compression") or "-", real_stats.get("page_content_compression") or "-", gate=True)
    _score_value(add, "Graphics state", stats.get("graphics_state_summary") or "-", real_stats.get("graphics_state_summary") or "-")
    _score_assets(add, stats, real_stats)
    _score_value(add, "Text positioning", int(stats.get("td_count", 0) or 0), int(real_stats.get("td_count", 0) or 0), gate=True)
    _score_value(add, "TL operators", int(stats.get("tl_count", 0) or 0), int(real_stats.get("tl_count", 0) or 0), gate=True)
    _score_value(add, "Barcode placement", stats.get("barcode_placement") or "-", real_stats.get("barcode_placement") or "-")
    _score_value(add, "Barcode representation", stats.get("barcode_representation") or "-", real_stats.get("barcode_representation") or "-")
    _score_value(add, "Barcode payload", stats.get("barcode_payload_summary") or "-", real_stats.get("barcode_payload_summary") or "-")
    _score_value(add, "Branding representation", _branding_representation(stats), _branding_representation(real_stats))
    _score_value(add, "/ExtGState", bool(stats.get("has_extgstate_resource")), bool(real_stats.get("has_extgstate_resource")))
    _score_value(add, "/Pattern", bool(stats.get("has_pattern_resource")), bool(real_stats.get("has_pattern_resource")))
    _score_pair(
        add,
        "f*/b*",
        (int(stats.get("fill_f_star_count", 0) or 0), int(stats.get("operator_counts", {}).get("b*", 0) or 0)),
        (int(real_stats.get("fill_f_star_count", 0) or 0), int(real_stats.get("operator_counts", {}).get("b*", 0) or 0)),
    )

    failed_gate_count = sum(1 for item in breakdown if item.get("gate") and not item.get("passed"))
    passed = failed_gate_count == 0
    result = dict(report)
    result.update(
        {
            "score": 100 if passed else 0,
            "failed_gate_count": failed_gate_count,
            "verdict": "DTDC PASSED" if passed else "DTDC SUSPICIOUS",
            "score_breakdown": breakdown,
            "notes": notes,
            "matched_real_sample": real.get("path"),
        }
    )
    return result


def _score_value(add, check: str, value: Any, real_value: Any, gate: bool = False) -> None:
    if value == real_value:
        add(False, check, f"{check} matches real sample: {value}.", gate=gate)
    else:
        add(True, check, f"{check} differs from real sample: got {value}, real {real_value}.", gate=gate)


def _score_pair(add, check: str, value: tuple[Any, ...], real_value: tuple[Any, ...]) -> None:
    if value == real_value:
        add(False, check, f"{check} matches real sample: {value}.")
    else:
        add(True, check, f"{check} differs from real sample: got {value}, real {real_value}.")


def _score_xobjects(add, stats: dict[str, Any], real_stats: dict[str, Any]) -> None:
    value = (
        int(stats.get("xobject_count", 0) or 0),
        int(stats.get("image_xobject_count", 0) or 0),
        int(stats.get("form_xobject_count", 0) or 0),
        int(stats.get("image_draw_operator_count", 0) or 0),
        int(stats.get("inline_image_operator_count", 0) or 0),
    )
    real_value = (
        int(real_stats.get("xobject_count", 0) or 0),
        int(real_stats.get("image_xobject_count", 0) or 0),
        int(real_stats.get("form_xobject_count", 0) or 0),
        int(real_stats.get("image_draw_operator_count", 0) or 0),
        int(real_stats.get("inline_image_operator_count", 0) or 0),
    )
    if value == real_value:
        add(False, "XObjects", f"XObject/image profile matches real sample: {value}.")
    elif value[0] == 0 and real_value[0] > 0:
        add(True, "XObjects", "No reusable XObjects were found, while the real DTDC sample contains XObjects.")
    else:
        add(True, "XObjects", f"XObject/image profile differs from real sample: got {value}, real {real_value}.")


def _score_assets(add, stats: dict[str, Any], real_stats: dict[str, Any]) -> None:
    values = set(stats.get("asset_hashes") or [])
    real_values = set(real_stats.get("asset_hashes") or [])
    if values and values & real_values:
        add(False, "Approved branding assets", "At least one image/logo asset hash matches the real DTDC sample.")
    elif not real_values and not values:
        add(False, "Approved branding assets", "No image/logo asset hashes in candidate or real sample.")
    else:
        add(True, "Approved branding assets", f"No image/logo asset hash matches the real DTDC sample: got {_join(values)}, real {_join(real_values)}.")


def _write_json(path: str, payload: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_html(path: str, payload: dict[str, Any]) -> None:
    rows = []
    for row in payload["real"]:
        rows.append(_row_html(row, "BASELINE REAL SAMPLE", "100"))
    for row in payload["candidates"]:
        rows.append(_row_html(row, row["verdict"], str(row["score"])))

    columns = [
        "File",
        "Label name",
        "Score",
        "Verdict",
        "PDF header",
        "EOF",
        "XObjects",
        "Image ops",
        "TJ values",
        "TD/Tf/TJ values",
        "Object serialization",
        "Page compression",
        "Graphics state",
        "Approved branding assets",
        "Text positioning",
        "TL operators",
        "Barcode placement",
        "Barcode representation",
        "Barcode payload",
        "Branding representation",
        "/ExtGState",
        "/Pattern",
        "f*/b*",
        "Findings",
    ]
    headers = "".join(f"<th>{_esc(column)}</th>" for column in columns)
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DTDC PDF Label Report</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2937; background: #f8fafc; }}
.table-wrap {{ overflow-x: auto; border: 1px solid #dbe2ea; background: white; }}
table {{ min-width: 2600px; width: 100%; border-collapse: collapse; background: white; }}
th, td {{ border: 1px solid #dbe2ea; padding: 8px; vertical-align: top; text-align: left; font-size: 12px; line-height: 1.35; }}
th {{ background: #eef2f7; position: sticky; top: 0; }}
code {{ font-family: Consolas, monospace; font-size: 11px; word-break: break-all; }}
.verdict {{ font-weight: 700; white-space: nowrap; }}
.match {{ color: #166534; }}
.suspicious {{ color: #b91c1c; }}
.finding {{ display: block; margin-bottom: 4px; }}
.sev-high {{ color: #b91c1c; font-weight: 700; }}
</style>
</head>
<body>
<div class="table-wrap">
<table>
<thead><tr>{headers}</tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</div>
</body>
</html>
"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


def _row_html(row: dict[str, Any], verdict: str, score: str) -> str:
    stats = row.get("stats") or {}
    image_ops = int(stats.get("image_draw_operator_count", 0) or 0) + int(stats.get("inline_image_operator_count", 0) or 0)
    b_star = int((stats.get("operator_counts") or {}).get("b*", 0) or 0)
    cells = [
        Path(row["path"]).name,
        _label_name_summary(stats, "dtdc"),
        score,
        f"<span class=\"verdict {_verdict_class(verdict)}\">{_esc(verdict)}</span>",
        _cell(stats.get("pdf_header") or "-", row, "PDF header"),
        _cell(f"count={stats.get('eof_count', 0)}, after={stats.get('bytes_after_final_eof', '-')}", row, "EOF"),
        _cell(f"total={stats.get('xobject_count', 0)}; image/form={stats.get('image_xobject_count', 0)}/{stats.get('form_xobject_count', 0)}; Do/BI={stats.get('image_draw_operator_count', 0)}/{stats.get('inline_image_operator_count', 0)}", row, "XObjects"),
        _cell(str(image_ops), row, "Image ops"),
        _cell(f"Tj={stats.get('t_func_operator_count', 0)}, TJ={stats.get('t_array_operator_count', 0)}", row, "TJ values"),
        _cell(str(stats.get("td_tf_tj_patterns", 0)), row, "TD/Tf/TJ values"),
        _cell(_object_serialization_detail(stats), row, "Object serialization"),
        _cell(stats.get("page_content_compression") or "-", row, "Page compression"),
        _cell(stats.get("graphics_state_summary") or "-", row, "Graphics state"),
        _cell(_asset_summary(stats), row, "Approved branding assets"),
        _cell(f"Td={stats.get('td_count', 0)}", row, "Text positioning"),
        _cell(f"TL={stats.get('tl_count', 0)}", row, "TL operators"),
        _cell(stats.get("barcode_placement") or "-", row, "Barcode placement"),
        _cell(stats.get("barcode_representation") or "-", row, "Barcode representation"),
        _cell(stats.get("barcode_payload_summary") or "-", row, "Barcode payload"),
        _cell(_branding_representation(stats), row, "Branding representation"),
        _cell(str(bool(stats.get("has_extgstate_resource"))), row, "/ExtGState"),
        _cell(str(bool(stats.get("has_pattern_resource"))), row, "/Pattern"),
        _cell(f"f*={stats.get('fill_f_star_count', 0)}, b*={b_star}", row, "f*/b*"),
        _findings_html(row),
    ]
    return "<tr>" + "".join(f"<td>{value}</td>" for value in cells) + "</tr>"


def _cell(value: Any, row: dict[str, Any], check: str) -> str:
    return f"{_esc(value)}<br>{_esc(_breakdown_detail(row, check))}"


def _breakdown_detail(row: dict[str, Any], check: str) -> str:
    details = []
    for item in row.get("score_breakdown") or []:
        if item.get("check") == check:
            if item.get("gate"):
                status = "PASS" if item.get("passed") else "FAIL"
            else:
                status = "INFO"
            details.append(f"{status}; {item.get('detail') or '-'}")
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
    print("DTDC REAL BASELINE")
    print(f"samples={len(real_rows)}")
    print("scoring=rule gates; Page compression, Text positioning, or TL operators mismatch makes candidate DTDC SUSPICIOUS")
    print("")
    print("REAL SAMPLES")
    for row in real_rows:
        stats = row.get("stats") or {}
        print(
            f"{Path(row['path']).name}: score=100 "
            f"header={stats.get('pdf_header') or '-'} "
            f"objects={stats.get('object_serialization') or '-'} "
            f"compression={stats.get('page_content_compression') or '-'} "
            f"xobjects={stats.get('xobject_count', 0)}"
        )
    print("")
    print("CANDIDATES")
    for row in candidate_rows:
        failed = int(row.get("failed_gate_count", 0) or 0)
        print(f"{Path(row['path']).name}: {row['verdict']} score={row['score']} failed_gates={failed}")
        for note in row.get("notes", [])[:12]:
            print(f"  - {note}")
        print("")


def _verdict_class(verdict: str) -> str:
    return "suspicious" if "suspicious" in verdict.lower() else "match"


def _asset_summary(stats: dict[str, Any]) -> str:
    return _join(stats.get("asset_hashes") or [])


def _branding_representation(stats: dict[str, Any]) -> str:
    return (
        f"resources={stats.get('page_resources_summary') or '-'}; "
        f"approved_assets={int(stats.get('approved_asset_count', 0) or 0)}; "
        f"hashes={_asset_summary(stats)}"
    )


def _join(values: Any) -> str:
    items = sorted(str(value) for value in values if str(value))
    return ", ".join(items) if items else "-"


def _esc(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


if __name__ == "__main__":
    raise SystemExit(main())
