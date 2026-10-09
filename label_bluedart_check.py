from __future__ import annotations

import argparse
import json
from pathlib import Path

from tracking.api.v1.label_structure_fingerprint import (
    analyze_pdf,
    build_baseline,
)
from pdf_internal_segments_loader import (
    analyze_pdf as analyze_internal_pdf,
    _barcode_text_summary,
    _barcode_values_summary,
    _build_barcode_baseline,
    _build_content_grammar_baseline,
    _build_writer_behavior_baseline,
    _document_id_detail,
    _label_name_summary,
    _object_serialization_detail,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a Blue Dart-specific hidden PDF fingerprint check with separate inputs and report output."
    )
    parser.add_argument(
        "--real-dir",
        default="test_labels/bluedart/real",
        help="Directory containing genuine Blue Dart PDFs.",
    )
    parser.add_argument(
        "--candidate-dir",
        default="test_labels/bluedart/candidate",
        help="Directory containing candidate Blue Dart PDFs.",
    )
    parser.add_argument(
        "--html-out",
        default="output/bluedart_label_report.html",
        help="Blue Dart HTML report path.",
    )
    parser.add_argument(
        "--json-out",
        default="output/bluedart_label_report.json",
        help="Blue Dart JSON report path.",
    )
    args = parser.parse_args()

    real_files = _pdfs_in(args.real_dir)
    candidate_files = _pdfs_in(args.candidate_dir)
    if not real_files:
        raise SystemExit("No PDF files found in Blue Dart --real-dir.")
    if not candidate_files:
        raise SystemExit("No PDF files found in Blue Dart --candidate-dir.")

    baseline = build_baseline(real_files)
    grammar_baseline = _build_content_grammar_baseline(real_files)
    barcode_baseline = _build_barcode_baseline(real_files)
    writer_baseline = _build_writer_behavior_baseline(real_files)

    real_rows = []
    for path in real_files:
        row = analyze_pdf(path).to_dict()
        row["internal"] = _internal_report(path, "real", grammar_baseline, barcode_baseline, writer_baseline)
        real_rows.append(row)

    candidate_rows = [
        _score_bluedart_candidate(path, baseline, real_rows, grammar_baseline, barcode_baseline, writer_baseline)
        for path in candidate_files
    ]

    payload = {
        "carrier": "bluedart",
        "focus_note": (
            "Blue Dart labels in this set are largely non-tagged PDFs, so structure-hash and MCID checks are "
            "secondary. Focus more on content operators, XObjects, image usage, object graph size, and path-op drift."
        ),
        "baseline_note": (
            "Use at least two or more genuine Blue Dart PDFs covering each generator family. "
            "If only one real sample is present, candidates from another genuine family may score as suspicious."
        ),
        "supportive_signals_note": (
            "Rule-based gate check. BLUE DART PASSED only when every selected Blue Dart hard gate matches a real "
            "sample. Any failed gate makes the candidate BLUE DART SUSPICIOUS."
        ),
        "baseline": baseline.to_dict(),
        "real": real_rows,
        "candidates": candidate_rows,
    }

    _write_json(args.json_out, payload)
    _write_html(args.html_out, payload)

    print("BLUE DART REAL BASELINE")
    print(f"samples={baseline.sample_count}")
    print(f"structure_trees={baseline.structure_tree_required_count}/{baseline.sample_count}")
    print("scoring=rule gates; XObjects, Image ops, TJ/Tj, TD/Tf/Tj, or Object serialization mismatch makes candidate BLUE DART SUSPICIOUS")
    print("")

    print("REAL SAMPLES")
    for row in real_rows:
        print(_sample_summary(row))
    print("")

    print("CANDIDATES")
    for row in candidate_rows:
        fp = row.get("fingerprint") or {}
        print(f"{Path(row['path']).name}: {row['verdict']}  score={row['score']}")
        print(
            "  primary="
            f"xobjects={fp.get('xobject_count', 0)}  "
            f"image_ops={fp.get('image_draw_ops', 0)}  "
            f"path_ops={fp.get('path_draw_ops', 0)}  "
            f"Tj={fp.get('t_func_operator_count', 0)}  "
            f"TJ={fp.get('t_array_operator_count', 0)}  "
            f"Td/Tf/TJ={fp.get('td_tf_tj_patterns', 0)}  "
            f"objects={fp.get('object_count', 0)}  "
            f"span={fp.get('object_id_span', 0)}"
        )
        print(
            "  marking="
            f"failed_gates={int(row.get('failed_gate_count', 0) or 0)}"
        )
        print(
            "  supportive="
            f"producer={fp.get('producer') or '-'}  "
            f"creator={fp.get('creator') or '-'}  "
            f"fonts={', '.join(fp.get('base_fonts', [])) or '-'}"
        )
        for reason in row.get("notes", [])[:10]:
            print(f"  - {reason}")
        print("")

    return 0


def _pdfs_in(path: str) -> list[Path]:
    root = Path(path)
    return sorted(file for file in root.rglob("*.pdf") if file.is_file())


def check_bluedart_pdf(candidate_path: str | Path, real_dir: str | Path = "test_labels/bluedart/real") -> dict:
    real_files = _pdfs_in(str(real_dir))
    if not real_files:
        raise RuntimeError("No PDF files found in Blue Dart real baseline directory.")

    baseline = build_baseline(real_files)
    grammar_baseline = _build_content_grammar_baseline(real_files)
    barcode_baseline = _build_barcode_baseline(real_files)
    writer_baseline = _build_writer_behavior_baseline(real_files)

    real_rows = []
    for path in real_files:
        row = analyze_pdf(path).to_dict()
        row["internal"] = _internal_report(path, "real", grammar_baseline, barcode_baseline, writer_baseline)
        real_rows.append(row)

    return _score_bluedart_candidate(
        Path(candidate_path),
        baseline,
        real_rows,
        grammar_baseline,
        barcode_baseline,
        writer_baseline,
    )


def _sample_summary(fp: dict) -> str:
    return (
        f"{Path(fp['path']).name}: "
        f"struct_tree={'yes' if fp.get('has_struct_tree') else 'no'}  "
        f"xobjects={fp.get('xobject_count', 0)}  "
        f"image_ops={fp.get('image_draw_ops', 0)}  "
        f"path_ops={fp.get('path_draw_ops', 0)}  "
        f"objects={fp.get('object_count', 0)}  "
        f"span={fp.get('object_id_span', 0)}  "
        f"producer={fp.get('producer') or '-'}  "
        f"creator={fp.get('creator') or '-'}"
    )


def _score_bluedart_candidate(
    path: Path,
    baseline,
    real_rows: list[dict],
    grammar_baseline: dict,
    barcode_baseline: dict,
    writer_baseline: dict,
) -> dict:
    fp = analyze_pdf(path).to_dict()
    internal = _internal_report(path, "candidate", grammar_baseline, barcode_baseline, writer_baseline)
    if any(_same_sample(fp, real_fp) for real_fp in real_rows):
        return {
            "path": str(path),
            "score": 100,
            "verdict": "BLUE DART PASSED",
            "summary": "The candidate exactly matches one of the genuine Blue Dart sample fingerprints.",
            "notes": [
                "Exact match with a genuine Blue Dart sample across the primary low-level PDF fingerprint signals."
            ],
            "failed_gate_count": 0,
            "score_breakdown": [{"check": "Exact sample match", "passed": True, "delta": 0, "detail": "Candidate matches a genuine sample."}],
            "fingerprint": fp,
            "internal": internal,
            "matched_real_sample": next(real_fp["path"] for real_fp in real_rows if _same_sample(fp, real_fp)),
        }

    best_result = None
    for real_fp in real_rows:
        result = _score_against_real_sample(fp, internal, real_fp)
        if best_result is None or result["failed_gate_count"] < best_result["failed_gate_count"]:
            best_result = result

    if best_result is None:
        raise RuntimeError("No Blue Dart real samples were available for candidate scoring.")

    best_result["path"] = str(path)
    best_result["fingerprint"] = fp
    best_result["internal"] = internal
    return best_result


def _internal_report(
    path: Path,
    kind: str,
    grammar_baseline: dict,
    barcode_baseline: dict,
    writer_baseline: dict,
) -> dict:
    report = analyze_internal_pdf(
        path,
        grammar_baseline=grammar_baseline,
        barcode_baseline=barcode_baseline,
        writer_baseline=writer_baseline,
    ).to_dict()
    report.setdefault("stats", {})["kind"] = kind
    return report


def _score_against_real_sample(fp: dict, internal: dict, real_fp: dict) -> dict:
    notes: list[str] = []
    breakdown: list[dict[str, object]] = []
    stats = internal.get("stats") or {}
    real_stats = (real_fp.get("internal") or {}).get("stats") or {}

    def add(penalty: int, label: str, detail: str, gate: bool = False) -> None:
        failed = bool(gate and penalty)
        breakdown.append({"check": label, "gate": gate, "passed": not failed, "delta": -1 if failed else 0, "detail": detail})
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

    xobject_count = int(fp.get("xobject_count", 0) or 0)
    image_ops = int(fp.get("image_draw_ops", 0) or 0)
    path_ops = int(fp.get("path_draw_ops", 0) or 0)
    object_count = int(fp.get("object_count", 0) or 0)
    object_span = int(fp.get("object_id_span", 0) or 0)
    t_func_ops = int(fp.get("t_func_operator_count", 0) or 0)
    t_array_ops = int(fp.get("t_array_operator_count", 0) or 0)
    td_tf_tj_patterns = int(fp.get("td_tf_tj_patterns", 0) or 0)

    real_xobject_count = int(real_fp.get("xobject_count", 0) or 0)
    if xobject_count == real_xobject_count:
        add(0, "Xobjects", f"XObject count matches real sample: {xobject_count}.", gate=True)
    elif xobject_count == 0 and real_xobject_count > 0:
        add(1, "Xobjects", "No reusable XObjects were found, while the real Blue Dart sample contains internal reusable objects.", gate=True)
    else:
        add(1, "Xobjects", f"XObject count differs from real sample: got {xobject_count}, real {real_xobject_count}.", gate=True)

    real_image_ops = int(real_fp.get("image_draw_ops", 0) or 0)
    if image_ops == real_image_ops:
        add(0, "Image ops", f"Image draw ops match real sample: {image_ops}.", gate=True)
    elif image_ops == 0 and real_image_ops > 0:
        add(1, "Image ops", "No image/object draw operations were found, while the real Blue Dart sample draws internal objects on page.", gate=True)
    else:
        add(1, "Image ops", f"Image draw ops differ from real sample: got {image_ops}, real {real_image_ops}.", gate=True)

    producer_chain = stats.get("apparent_producer_chain") or fp.get("producer") or "-"
    real_producer_chain = real_stats.get("apparent_producer_chain") or real_fp.get("producer") or "-"
    if producer_chain == real_producer_chain:
        add(0, "Producer chain", f"Producer chain matches real sample: {producer_chain}.")
    else:
        add(12, "Producer chain", f"Producer chain differs from real sample: got {producer_chain}, real {real_producer_chain}.")

    for label, value, real_value, penalty in [
        ("Tj Text Operator Count", t_func_ops, int(real_fp.get("t_func_operator_count", 0) or 0), 8),
        ("TJ Text Operator Count", t_array_ops, int(real_fp.get("t_array_operator_count", 0) or 0), 8),
    ]:
        if value == real_value:
            add(0, "TJ and Tj value", f"{label} matches real sample: {value}.", gate=True)
        else:
            add(1, "TJ and Tj value", f"{label} differs from real sample: got {value}, real {real_value}.", gate=True)

    real_td_tf_tj_patterns = int(real_fp.get("td_tf_tj_patterns", 0) or 0)
    if td_tf_tj_patterns == real_td_tf_tj_patterns:
        add(0, "TD/Tf/Tj value", f"Td/Tf/TJ grammar count matches real sample: {td_tf_tj_patterns}.", gate=True)
    else:
        add(1, "TD/Tf/Tj value", f"Td/Tf/TJ grammar count differs from real sample: got {td_tf_tj_patterns}, real {real_td_tf_tj_patterns}.", gate=True)

    if fp.get("content_grammar_sequence_sha256") == real_fp.get("content_grammar_sequence_sha256"):
        add(0, "Grammar contribution", "Content grammar hash matches real sample.")
    else:
        add(1, "Grammar contribution", "Content grammar hash differs from real sample.")

    prev_count = int(stats.get("prev_chain_count", 0) or 0)
    real_prev_count = int(real_stats.get("prev_chain_count", 0) or 0)
    if prev_count == real_prev_count:
        add(0, "/Prev", f"/Prev count matches real sample: {prev_count}.")
    else:
        add(12, "/Prev", f"/Prev count differs from real sample: got {prev_count}, real {real_prev_count}.")

    object_serialization = stats.get("object_serialization") or "-"
    real_object_serialization = real_stats.get("object_serialization") or "-"
    if object_serialization == real_object_serialization:
        add(0, "Object serialization", f"Object serialization matches real sample: {object_serialization}.", gate=True)
    else:
        add(1, "Object serialization", f"Object serialization differs from real sample: got {object_serialization}, real {real_object_serialization}.", gate=True)

    document_id_pattern = stats.get("document_id_pattern") or "-"
    real_document_id_pattern = real_stats.get("document_id_pattern") or "-"
    if document_id_pattern == real_document_id_pattern:
        add(0, "Document IDs", f"Document ID pattern matches real sample: {document_id_pattern}.")
    else:
        add(8, "Document IDs", f"Document ID pattern differs from real sample: got {document_id_pattern}, real {real_document_id_pattern}.")

    compression = stats.get("page_content_compression") or "-"
    real_compression = real_stats.get("page_content_compression") or "-"
    if compression == real_compression:
        add(0, "Page content compression", f"Page content compression matches real sample: {compression}.")
    else:
        add(1, "Page content compression", f"Page content compression differs from real sample: got {compression}, real {real_compression}.")

    trailer_comment = stats.get("trailer_comment") or "-"
    real_trailer_comment = real_stats.get("trailer_comment") or "-"
    if trailer_comment == real_trailer_comment:
        add(0, "Trailer comment", f"Trailer comment pattern matches real sample: {trailer_comment}.")
    else:
        add(6, "Trailer comment", f"Trailer comment pattern differs from real sample: got {trailer_comment}, real {real_trailer_comment}.")

    barcode_count = int(stats.get("barcode_count", 0) or 0)
    real_barcode_count = int(real_stats.get("barcode_count", 0) or 0)
    barcode_formats = stats.get("barcode_formats") or []
    real_barcode_formats = real_stats.get("barcode_formats") or []
    if barcode_count == real_barcode_count and barcode_formats == real_barcode_formats:
        add(0, "Barcode values", f"Barcode count/format matches real sample: count={barcode_count}, formats={', '.join(barcode_formats) or '-'}.")
    elif barcode_count == 0 and real_barcode_count > 0:
        add(1, "Barcode values", "No decoded barcode was found, while real Blue Dart samples contain decoded barcodes.")
    else:
        add(1, "Barcode values", f"Barcode count/format differs from real sample: got count={barcode_count}, formats={', '.join(barcode_formats) or '-'}; real count={real_barcode_count}, formats={', '.join(real_barcode_formats) or '-'}.")

    missing_barcodes = stats.get("barcode_values_missing_from_text") or []
    if not stats.get("barcode_checker_available"):
        add(0, "Barcode textmatch", "Barcode decoder was unavailable.")
    elif not missing_barcodes:
        add(0, "Barcode textmatch", "All decoded barcode values are present in extracted text.")
    else:
        add(0, "Barcode textmatch", f"{len(missing_barcodes)} decoded barcode value(s) are missing from extracted text.")

    barcode_contribution = str(stats.get("barcode_contribution") or "")
    if "barcode/text mismatch" in barcode_contribution:
        add(1, "Barcode contributions", "Barcode/text mismatch.")
    elif "profile mismatch" in barcode_contribution:
        add(1, "Barcode contributions", "Barcode profile differs from real baseline.")
    elif "no decoded barcode" in barcode_contribution:
        add(1, "Barcode contributions", "Barcode contribution reports no decoded barcode.")
    elif "decoder unavailable" in barcode_contribution or "not checked" in barcode_contribution:
        add(1, "Barcode contributions", "Barcode contribution was not checked.")
    else:
        add(0, "Barcode contributions", barcode_contribution or "Barcode contribution has no penalty.")

    failed_gate_count = sum(1 for item in breakdown if not item.get("passed"))
    passed = failed_gate_count == 0
    score = 100 if passed else 0
    verdict = "BLUE DART PASSED" if passed else "BLUE DART SUSPICIOUS"
    summary = (
        "The candidate matches every obvious Blue Dart gate."
        if passed
        else "The candidate failed at least one obvious Blue Dart gate."
    )

    return {
        "score": score,
        "verdict": verdict,
        "summary": summary,
        "notes": notes,
        "score_breakdown": breakdown,
        "failed_gate_count": failed_gate_count,
        "matched_real_sample": real_fp.get("path"),
    }


def _same_sample(candidate_fp: dict, real_fp: dict) -> bool:
    keys = (
        "has_struct_tree",
        "xobject_count",
        "image_draw_ops",
        "path_draw_ops",
        "object_count",
        "object_id_span",
        "content_operator_signature_sha256",
        "content_grammar_sequence_sha256",
        "t_func_operator_count",
        "t_array_operator_count",
        "td_tf_tj_patterns",
        "object_number_signature_sha256",
        "producer",
        "creator",
    )
    return all(candidate_fp.get(key) == real_fp.get(key) for key in keys) and (
        candidate_fp.get("base_fonts") or []
    ) == (real_fp.get("base_fonts") or [])


def _write_json(path: str, payload: dict) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_html(path: str, payload: dict) -> None:
    baseline = payload["baseline"]
    real_rows = payload["real"]
    candidate_rows = payload["candidates"]
    rows: list[str] = []

    for row in real_rows:
        row["score"] = 100
        rows.append(_row_html(Path(row["path"]).name, "BASELINE REAL SAMPLE", "100", row, row))
    for row in candidate_rows:
        rows.append(
            _row_html(
                Path(row["path"]).name,
                row["verdict"],
                str(row["score"]),
                row.get("fingerprint") or {},
                row,
            )
        )

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blue Dart Hidden PDF Label Report</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2937; background: #f8fafc; }}
.card {{ background: white; border: 1px solid #dbe2ea; border-radius: 8px; padding: 16px 18px; margin-bottom: 18px; }}
.table-wrap {{ overflow-x: auto; border: 1px solid #dbe2ea; background: white; }}
table {{ min-width: 2200px; width: 100%; border-collapse: collapse; background: white; }}
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
<div class="card table-wrap">
  <table>
    <thead>
      <tr>
        <th>File</th>
        <th>Label name</th>
        <th>Verdict</th>
        <th>Score</th>
        <th>Header</th>
        <th>EOF</th>
        <th>Xobjects</th>
        <th>Image ops</th>
        <th>Producer chain</th>
        <th>TJ and Tj value</th>
        <th>TD/Tf/Tj value</th>
        <th>Grammar contribution</th>
        <th>/Prev</th>
        <th>Object serialization</th>
        <th>Document IDs</th>
        <th>Page content compression</th>
        <th>Trailer comment</th>
        <th>Barcode values</th>
        <th>Barcode textmatch</th>
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
</html>"""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


def _row_html(name: str, verdict: str, score: str, fp: dict, row: dict | None = None) -> str:
    internal = (row or {}).get("internal") or {}
    stats = internal.get("stats") or {}
    verdict_class = _verdict_class(verdict)
    findings = _findings_html(row or {}, internal)
    return (
        "<tr>"
        f"<td>{_esc(name)}</td>"
        f"<td>{_esc(_label_name_summary(stats, 'bluedart'))}</td>"
        f"<td class=\"verdict {verdict_class}\">{_esc(verdict)}</td>"
        f"<td>{_esc(score)}</td>"
        f"<td>{_esc(stats.get('pdf_header') or '-')}<br>offset={_esc(stats.get('pdf_header_offset', '-'))}<br>{_esc(_breakdown_detail(row, 'Header'))}</td>"
        f"<td>count={_esc(stats.get('eof_count', 0))}<br>after={_esc(stats.get('bytes_after_final_eof', '-'))}<br>{_esc(_breakdown_detail(row, 'EOF'))}</td>"
        f"<td>{fp.get('xobject_count', 0)}<br>{_esc(_breakdown_detail(row, 'Xobjects'))}</td>"
        f"<td>{fp.get('image_draw_ops', 0)}<br>{_esc(_breakdown_detail(row, 'Image ops'))}</td>"
        f"<td>{_esc(stats.get('apparent_producer_chain') or fp.get('producer') or '-')}<br>{_esc(_breakdown_detail(row, 'Producer chain'))}</td>"
        f"<td>TJ={fp.get('t_array_operator_count', 0)}<br>Tj={fp.get('t_func_operator_count', 0)}<br>{_esc(_breakdown_detail(row, 'TJ and Tj value'))}</td>"
        f"<td>Td/Tf/TJ={fp.get('td_tf_tj_patterns', 0)}<br>{_esc(_breakdown_detail(row, 'TD/Tf/Tj value'))}</td>"
        f"<td>{_esc(stats.get('content_grammar_contribution') or 'info only; no baseline')}<br>{_esc(_breakdown_detail(row, 'Grammar contribution'))}<br><code>{_esc(fp.get('content_grammar_sequence_sha256') or '-')}</code></td>"
        f"<td>{_esc(stats.get('prev_chain_count', 0))}<br>{_esc(_breakdown_detail(row, '/Prev'))}</td>"
        f"<td>{_esc(_object_serialization_detail(stats))}<br>{_esc(_breakdown_detail(row, 'Object serialization'))}</td>"
        f"<td>{_esc(_document_id_detail(stats))}<br>{_esc(_breakdown_detail(row, 'Document IDs'))}</td>"
        f"<td>{_esc(stats.get('page_content_compression') or '-')}<br>{_esc(_breakdown_detail(row, 'Page content compression'))}</td>"
        f"<td>{_esc(stats.get('trailer_comment') or '-')}<br>{_esc(_breakdown_detail(row, 'Trailer comment'))}</td>"
        f"<td>{_esc(_barcode_values_summary(stats))}<br>{_esc(_breakdown_detail(row, 'Barcode values'))}</td>"
        f"<td>{_esc(_barcode_text_summary(stats))}<br>{_esc(_breakdown_detail(row, 'Barcode textmatch'))}</td>"
        f"<td>{_esc(stats.get('barcode_contribution') or 'not checked')}<br>{_esc(_breakdown_detail(row, 'Barcode contributions'))}</td>"
        f"<td>{findings}</td>"
        "</tr>"
    )


def _breakdown_detail(row: dict | None, check: str) -> str:
    if not row:
        return "-"
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


def _findings_html(row: dict, internal: dict) -> str:
    breakdown = row.get("score_breakdown") or []
    failures = [item for item in breakdown if item.get("gate") and item.get("passed") is False]
    if not failures:
        return "<span class=\"finding\">No gate mismatches found.</span>"
    parts: list[str] = []
    for item in failures:
        parts.append(
            f"<span class=\"finding\"><span class=\"sev-high\">FAIL</span> "
            f"{_esc(item.get('check') or '-')}: {_esc(item.get('detail') or '-')}</span>"
        )
    return "".join(parts)


def _verdict_class(verdict: str) -> str:
    text = verdict.lower()
    if "suspicious" in text:
        return "suspicious"
    if "partial" in text:
        return "partial"
    return "match"


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


if __name__ == "__main__":
    raise SystemExit(main())
