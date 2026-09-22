from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pdf_internal_segments_loader import (
    analyze_pdf as analyze_internal_pdf,
    _barcode_text_summary,
    _barcode_values_summary,
    _build_barcode_baseline,
    _build_content_grammar_baseline,
    _build_delhivery_specific_baseline,
    _build_writer_behavior_baseline,
    _label_name_summary,
    _object_serialization_detail,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a Delhivery-specific PDF table-column check with separate inputs and report output."
    )
    parser.add_argument(
        "--real-dir",
        default="test_labels/delhivery/real",
        help="Directory containing genuine Delhivery PDFs.",
    )
    parser.add_argument(
        "--candidate-dir",
        default="test_labels/delhivery/candidate",
        help="Directory containing candidate Delhivery PDFs.",
    )
    parser.add_argument(
        "--html-out",
        default="output/delhivery_label_report.html",
        help="Delhivery HTML report path.",
    )
    parser.add_argument(
        "--json-out",
        default="output/delhivery_label_report.json",
        help="Delhivery JSON report path.",
    )
    args = parser.parse_args()

    real_files = _pdfs_in(args.real_dir)
    candidate_files = _pdfs_in(args.candidate_dir)
    if not real_files:
        raise SystemExit("No PDF files found in Delhivery --real-dir.")
    if not candidate_files:
        raise SystemExit("No PDF files found in Delhivery --candidate-dir.")

    grammar_baseline = _build_content_grammar_baseline(real_files)
    barcode_baseline = _build_barcode_baseline(real_files)
    writer_baseline = _build_writer_behavior_baseline(real_files)
    delhivery_baseline = _build_delhivery_specific_baseline(real_files)

    real_rows = [
        _internal_report(path, "real", grammar_baseline, barcode_baseline, writer_baseline, delhivery_baseline)
        for path in real_files
    ]
    candidate_rows = [
        _score_candidate(
            path,
            real_rows,
            grammar_baseline,
            barcode_baseline,
            writer_baseline,
            delhivery_baseline,
        )
        for path in candidate_files
    ]

    payload = {
        "carrier": "delhivery",
        "scoring": "Rule-based gate check. DELHIVERY PASSED only when Page compression, f*/b*, XObjects, /Trans entry in page directory, and Image Ops all match a real Delhivery sample. Any one mismatch makes the candidate DELHIVERY SUSPICIOUS.",
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


def check_delhivery_pdf(candidate_path: str | Path, real_dir: str | Path = "test_labels/delhivery/real") -> dict[str, Any]:
    real_files = _pdfs_in(str(real_dir))
    if not real_files:
        raise RuntimeError("No PDF files found in Delhivery real baseline directory.")

    grammar_baseline = _build_content_grammar_baseline(real_files)
    barcode_baseline = _build_barcode_baseline(real_files)
    writer_baseline = _build_writer_behavior_baseline(real_files)
    delhivery_baseline = _build_delhivery_specific_baseline(real_files)
    real_rows = [
        _internal_report(path, "real", grammar_baseline, barcode_baseline, writer_baseline, delhivery_baseline)
        for path in real_files
    ]

    return _score_candidate(
        Path(candidate_path),
        real_rows,
        grammar_baseline,
        barcode_baseline,
        writer_baseline,
        delhivery_baseline,
    )


def _internal_report(
    path: Path,
    kind: str,
    grammar_baseline: dict[str, Any],
    barcode_baseline: dict[str, Any],
    writer_baseline: dict[str, Any],
    delhivery_baseline: dict[str, Any],
) -> dict[str, Any]:
    report = analyze_internal_pdf(
        path,
        grammar_baseline=grammar_baseline,
        barcode_baseline=barcode_baseline,
        writer_baseline=writer_baseline,
        delhivery_baseline=delhivery_baseline,
    ).to_dict()
    report.setdefault("stats", {})["kind"] = kind
    report["path"] = str(path)
    if kind == "real":
        report["score"] = 100
        report["verdict"] = "BASELINE REAL SAMPLE"
        report["score_breakdown"] = []
    return report


def _score_candidate(
    path: Path,
    real_rows: list[dict[str, Any]],
    grammar_baseline: dict[str, Any],
    barcode_baseline: dict[str, Any],
    writer_baseline: dict[str, Any],
    delhivery_baseline: dict[str, Any],
) -> dict[str, Any]:
    report = _internal_report(path, "candidate", grammar_baseline, barcode_baseline, writer_baseline, delhivery_baseline)
    best = None
    for real in real_rows:
        scored = _score_against_real(report, real)
        if best is None or scored["failed_gate_count"] < best["failed_gate_count"]:
            best = scored
    if best is None:
        raise RuntimeError("No Delhivery real samples were available for candidate scoring.")
    return best


def _score_against_real(report: dict[str, Any], real: dict[str, Any]) -> dict[str, Any]:
    stats = report.get("stats") or {}
    real_stats = real.get("stats") or {}
    breakdown: list[dict[str, Any]] = []
    notes: list[str] = []

    def add(penalty: int, check: str, detail: str, gate: bool = False) -> None:
        failed = bool(gate and penalty)
        breakdown.append({"check": check, "gate": gate, "passed": not failed, "delta": -1 if failed else 0, "detail": detail})
        if detail:
            notes.append(detail)

    header = stats.get("pdf_header") or ""
    real_header = real_stats.get("pdf_header") or ""
    header_offset_value = stats.get("pdf_header_offset")
    header_offset = int(header_offset_value) if header_offset_value is not None else -1
    if header == real_header and header_offset == 0:
        add(0, "Header", f"Header matches real sample: {header}, offset={header_offset}.")
    elif header_offset != 0:
        add(10, "Header", f"Header offset differs from real sample: got offset={header_offset}, expected 0.")
    else:
        add(4, "Header", f"PDF header version differs from real sample: got {header or '-'}, real {real_header or '-'}.")

    eof_count = int(stats.get("eof_count", 0) or 0)
    bytes_after_eof = stats.get("bytes_after_final_eof")
    real_eof_count = int(real_stats.get("eof_count", 0) or 0)
    real_bytes_after_eof = real_stats.get("bytes_after_final_eof")
    if eof_count == real_eof_count and bytes_after_eof == real_bytes_after_eof:
        add(0, "EOF", f"EOF pattern matches real sample: count={eof_count}, after={bytes_after_eof}.")
    elif eof_count != real_eof_count:
        add(12, "EOF", f"EOF count differs from real sample: got {eof_count}, real {real_eof_count}.")
    else:
        add(4, "EOF", f"Bytes after final EOF differ from real sample: got {bytes_after_eof}, real {real_bytes_after_eof}.")

    xobjects = int(stats.get("xobject_count", 0) or 0)
    real_xobjects = int(real_stats.get("xobject_count", 0) or 0)
    if xobjects == real_xobjects:
        add(0, "XObjects", f"XObject count matches real sample: {xobjects}.", gate=True)
    elif xobjects == 0 and real_xobjects > 0:
        add(1, "XObjects", "No reusable XObjects were found, while the real Delhivery sample contains reusable XObjects.", gate=True)
    else:
        add(1, "XObjects", f"XObject count differs from real sample: got {xobjects}, real {real_xobjects}.", gate=True)

    image_ops = int(stats.get("image_draw_operator_count", 0) or 0) + int(stats.get("inline_image_operator_count", 0) or 0)
    real_image_ops = int(real_stats.get("image_draw_operator_count", 0) or 0) + int(real_stats.get("inline_image_operator_count", 0) or 0)
    if image_ops == real_image_ops:
        add(0, "Image Ops", f"Image ops match real sample: {image_ops}.", gate=True)
    elif image_ops == 0 and real_image_ops > 0:
        add(1, "Image Ops", "No image draw operations were found, while the real Delhivery sample has image operations.", gate=True)
    else:
        add(1, "Image Ops", f"Image ops differ from real sample: got {image_ops}, real {real_image_ops}.", gate=True)

    _score_value(
        add,
        "Object serialization",
        stats.get("object_serialization") or "-",
        real_stats.get("object_serialization") or "-",
        12,
    )
    _score_value(
        add,
        "Page compression",
        stats.get("delhivery_page_stream_compression") or stats.get("page_content_compression") or "-",
        real_stats.get("delhivery_page_stream_compression") or real_stats.get("page_content_compression") or "-",
        8,
        gate=True,
    )
    _score_pair(
        add,
        "f*/b*",
        int(stats.get("delhivery_f_star_ops", 0) or 0),
        int(stats.get("delhivery_b_star_ops", 0) or 0),
        int(real_stats.get("delhivery_f_star_ops", 0) or 0),
        int(real_stats.get("delhivery_b_star_ops", 0) or 0),
        8,
        gate=True,
    )
    _score_value(
        add,
        "/Trans entry in page directory",
        int(stats.get("delhivery_trans_pages", 0) or 0),
        int(real_stats.get("delhivery_trans_pages", 0) or 0),
        8,
        gate=True,
    )
    _score_value(
        add,
        "/Rotate entry",
        int(stats.get("delhivery_rotate_zero_pages", 0) or 0),
        int(real_stats.get("delhivery_rotate_zero_pages", 0) or 0),
        6,
    )
    _score_value(
        add,
        "Font redundant /Name entry",
        int(stats.get("delhivery_redundant_font_name_count", 0) or 0),
        int(real_stats.get("delhivery_redundant_font_name_count", 0) or 0),
        6,
    )
    _score_barcode_textmatch(add, stats)
    _score_barcode_count(add, stats, real_stats)
    _score_barcode_contribution(add, stats)

    failed_gate_count = sum(1 for item in breakdown if item.get("gate") and not item.get("passed"))
    passed = failed_gate_count == 0
    score = 100 if passed else 0
    verdict = "DELHIVERY PASSED" if passed else "DELHIVERY SUSPICIOUS"

    result = dict(report)
    result.update(
        {
            "score": score,
            "verdict": verdict,
            "score_breakdown": breakdown,
            "notes": notes,
            "failed_gate_count": failed_gate_count,
            "matched_real_sample": real.get("path"),
        }
    )
    return result


def _score_value(add, check: str, value: Any, real_value: Any, penalty: int, gate: bool = False) -> None:
    if value == real_value:
        add(0, check, f"{check} matches real sample: {value}.", gate=gate)
    else:
        add(penalty, check, f"{check} differs from real sample: got {value}, real {real_value}.", gate=gate)


def _score_pair(add, check: str, first: int, second: int, real_first: int, real_second: int, penalty: int, gate: bool = False) -> None:
    if first == real_first and second == real_second:
        add(0, check, f"{check} matches real sample: {first}/{second}.", gate=gate)
    else:
        add(penalty, check, f"{check} differs from real sample: got {first}/{second}, real {real_first}/{real_second}.", gate=gate)


def _score_barcode_textmatch(add, stats: dict[str, Any]) -> None:
    if not stats.get("barcode_checker_available"):
        add(0, "Barcode text matches", "Barcode decoder was unavailable.")
        return
    if int(stats.get("barcode_count", 0) or 0) == 0:
        add(0, "Barcode text matches", "No decoded barcode was available for text matching.")
        return
    missing = stats.get("barcode_values_missing_from_text") or []
    if missing:
        add(0, "Barcode text matches", f"{len(missing)} decoded barcode value(s) are missing from extracted text.")
    else:
        add(0, "Barcode text matches", "All decoded barcode values are present in extracted text.")


def _score_barcode_count(add, stats: dict[str, Any], real_stats: dict[str, Any]) -> None:
    count = int(stats.get("barcode_count", 0) or 0)
    real_count = int(real_stats.get("barcode_count", 0) or 0)
    formats = stats.get("barcode_formats") or []
    real_formats = real_stats.get("barcode_formats") or []
    if count == real_count and formats == real_formats:
        add(0, "Barcode values", f"Barcode count/format matches real sample: count={count}, formats={', '.join(formats) or '-'}.")
    elif count == 0 and real_count > 0:
        add(16, "Barcode values", "No decoded barcode was found, while real Delhivery samples contain decoded barcodes.")
    else:
        add(8, "Barcode values", f"Barcode count/format differs from real sample: got count={count}, formats={', '.join(formats) or '-'}; real count={real_count}, formats={', '.join(real_formats) or '-'}.")


def _score_barcode_contribution(add, stats: dict[str, Any]) -> None:
    contribution = str(stats.get("barcode_contribution") or "")
    if "barcode/text mismatch" in contribution:
        add(24, "Barcode contributions", "Barcode/text mismatch.")
    elif "profile mismatch" in contribution:
        add(12, "Barcode contributions", "Barcode profile differs from real baseline.")
    elif "no decoded barcode" in contribution:
        add(24, "Barcode contributions", "Barcode contribution reports no decoded barcode.")
    elif "decoder unavailable" in contribution or "not checked" in contribution:
        add(4, "Barcode contributions", "Barcode contribution was not checked.")
    else:
        add(0, "Barcode contributions", contribution or "Barcode contribution has no penalty.")


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

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Delhivery PDF Label Report</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2937; background: #f8fafc; }}
.table-wrap {{ overflow-x: auto; border: 1px solid #dbe2ea; background: white; }}
table {{ min-width: 1500px; width: 100%; border-collapse: collapse; background: white; }}
th, td {{ border: 1px solid #dbe2ea; padding: 8px; vertical-align: top; text-align: left; font-size: 12px; line-height: 1.35; }}
th {{ background: #eef2f7; position: sticky; top: 0; }}
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
<thead>
<tr>
<th>File</th>
<th>Label name</th>
<th>Verdict</th>
<th>Score</th>
<th>Header</th>
<th>EOF</th>
<th>XObjects</th>
<th>Image Ops</th>
<th>Object serialization</th>
<th>Page compression</th>
<th>f*/b*</th>
<th>/Trans entry in page directory</th>
<th>/Rotate entry</th>
<th>Font redundant /Name entry</th>
<th>Barcode text matches</th>
<th>Barcode values</th>
<th>Barcode contributions</th>
<th>Findings</th>
</tr>
</thead>
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
    return (
        "<tr>"
        f"<td>{_esc(Path(row['path']).name)}</td>"
        f"<td>{_esc(_label_name_summary(stats, 'delhivery'))}</td>"
        f"<td class=\"verdict {_verdict_class(verdict)}\">{_esc(verdict)}</td>"
        f"<td>{_esc(score)}</td>"
        f"<td>{_esc(stats.get('pdf_header') or '-')}<br>offset={_esc(stats.get('pdf_header_offset', '-'))}<br>{_esc(_breakdown_detail(row, 'Header'))}</td>"
        f"<td>count={_esc(stats.get('eof_count', 0))}<br>after={_esc(stats.get('bytes_after_final_eof', '-'))}<br>{_esc(_breakdown_detail(row, 'EOF'))}</td>"
        f"<td>{_esc(stats.get('xobject_count', 0))}<br>{_esc(_breakdown_detail(row, 'XObjects'))}</td>"
        f"<td>Do={_esc(stats.get('image_draw_operator_count', 0))}<br>BI={_esc(stats.get('inline_image_operator_count', 0))}<br>{_esc(_breakdown_detail(row, 'Image Ops'))}</td>"
        f"<td>{_esc(_object_serialization_detail(stats))}<br>{_esc(_breakdown_detail(row, 'Object serialization'))}</td>"
        f"<td>{_esc(stats.get('delhivery_page_stream_compression') or stats.get('page_content_compression') or '-')}<br>{_esc(_breakdown_detail(row, 'Page compression'))}</td>"
        f"<td>f*={_esc(stats.get('delhivery_f_star_ops', 0))}<br>B*={_esc(stats.get('delhivery_b_star_ops', 0))}<br>{_esc(_breakdown_detail(row, 'f*/b*'))}</td>"
        f"<td>{_esc(stats.get('delhivery_trans_pages', 0))}<br>{_esc(_breakdown_detail(row, '/Trans entry in page directory'))}</td>"
        f"<td>{_esc(stats.get('delhivery_rotate_zero_pages', 0))}<br>{_esc(_breakdown_detail(row, '/Rotate entry'))}</td>"
        f"<td>{_esc(stats.get('delhivery_redundant_font_name_count', 0))}<br>{_esc(_breakdown_detail(row, 'Font redundant /Name entry'))}</td>"
        f"<td>{_esc(_barcode_text_summary(stats))}<br>{_esc(_breakdown_detail(row, 'Barcode text matches'))}</td>"
        f"<td>{_esc(_barcode_values_summary(stats))}<br>{_esc(_breakdown_detail(row, 'Barcode values'))}</td>"
        f"<td>{_esc(stats.get('barcode_contribution') or 'not checked')}<br>{_esc(_breakdown_detail(row, 'Barcode contributions'))}</td>"
        f"<td>{_findings_html(row)}</td>"
        "</tr>"
    )


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
    print("DELHIVERY REAL BASELINE")
    print(f"samples={len(real_rows)}")
    print("scoring=rule gates; Page compression, f*/b*, XObjects, /Trans, or Image Ops mismatch makes candidate DELHIVERY SUSPICIOUS")
    print("")
    print("REAL SAMPLES")
    for row in real_rows:
        stats = row.get("stats") or {}
        print(
            f"{Path(row['path']).name}: score=100 "
            f"objects={stats.get('object_serialization') or '-'} "
            f"compression={stats.get('delhivery_page_stream_compression') or stats.get('page_content_compression') or '-'} "
            f"f*/B*={stats.get('delhivery_f_star_ops', 0)}/{stats.get('delhivery_b_star_ops', 0)} "
            f"barcodes={stats.get('barcode_count', 0)}"
        )
    print("")
    print("CANDIDATES")
    for row in candidate_rows:
        failed = int(row.get("failed_gate_count", 0) or 0)
        print(f"{Path(row['path']).name}: {row['verdict']} score={row['score']} failed_gates={failed}")
        for note in row.get("notes", [])[:10]:
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


if __name__ == "__main__":
    raise SystemExit(main())
