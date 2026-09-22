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
    parser = argparse.ArgumentParser(
        description="Run a Shadowfax-specific PDF forgery table check with separate report output."
    )
    parser.add_argument("--real-dir", default="test_labels/shadowfax/real")
    parser.add_argument("--candidate-dir", default="test_labels/shadowfax/candidate")
    parser.add_argument("--html-out", default="output/shadowfax_label_report.html")
    parser.add_argument("--json-out", default="output/shadowfax_label_report.json")
    args = parser.parse_args()

    real_files = _pdfs_in(args.real_dir)
    candidate_files = _pdfs_in(args.candidate_dir)
    if not real_files:
        raise SystemExit("No PDF files found in Shadowfax --real-dir.")
    if not candidate_files:
        raise SystemExit("No PDF files found in Shadowfax --candidate-dir.")

    grammar_baseline = _build_content_grammar_baseline(real_files)
    barcode_baseline = _build_barcode_baseline(real_files)
    writer_baseline = _build_writer_behavior_baseline(real_files)

    real_rows = [_internal_report(path, "real", grammar_baseline, barcode_baseline, writer_baseline) for path in real_files]
    candidate_rows = [
        _score_candidate(path, real_rows, grammar_baseline, barcode_baseline, writer_baseline)
        for path in candidate_files
    ]

    payload = {
        "carrier": "shadowfax",
        "scoring": "Rule-based gate check. SHADOWFAX PASSED only when every visible check column matches a real Shadowfax sample. Any one mismatch makes the candidate SHADOWFAX SUSPICIOUS. Score is 100 for pass and 0 for suspicious.",
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


def check_shadowfax_pdf(candidate_path: str | Path, real_dir: str | Path = "test_labels/shadowfax/real") -> dict[str, Any]:
    real_files = _pdfs_in(str(real_dir))
    if not real_files:
        raise RuntimeError("No PDF files found in Shadowfax real baseline directory.")

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
        raise RuntimeError("No Shadowfax real samples were available for candidate scoring.")
    return best


def _score_against_real(report: dict[str, Any], real: dict[str, Any]) -> dict[str, Any]:
    stats = report.get("stats") or {}
    real_stats = real.get("stats") or {}
    breakdown: list[dict[str, Any]] = []
    notes: list[str] = []

    def add(penalty: int, check: str, detail: str) -> None:
        failed = bool(penalty)
        breakdown.append({"check": check, "passed": not failed, "delta": -1 if failed else 0, "detail": detail})
        if detail:
            notes.append(detail)

    _score_value(
        add,
        "Object serialization",
        stats.get("object_serialization") or "-",
        real_stats.get("object_serialization") or "-",
    )
    _score_value(
        add,
        "Page compression",
        stats.get("page_content_compression") or "-",
        real_stats.get("page_content_compression") or "-",
    )
    _score_xobjects(add, stats, real_stats)
    _score_value(
        add,
        "Barcode representation",
        stats.get("barcode_representation") or "-",
        real_stats.get("barcode_representation") or "-",
    )
    _score_assets(add, stats, real_stats)
    _score_value(
        add,
        "TL operators",
        int(stats.get("tl_count", 0) or 0),
        int(real_stats.get("tl_count", 0) or 0),
    )
    _score_value(add, "Graphics state", stats.get("graphics_state_summary") or "-", real_stats.get("graphics_state_summary") or "-")
    _score_value(add, "Text positioning", int(stats.get("td_count", 0) or 0), int(real_stats.get("td_count", 0) or 0))

    failed_gate_count = sum(1 for item in breakdown if not item.get("passed"))
    passed = failed_gate_count == 0
    result = dict(report)
    result.update(
        {
            "score": 100 if passed else 0,
            "failed_gate_count": failed_gate_count,
            "verdict": "SHADOWFAX PASSED" if passed else "SHADOWFAX SUSPICIOUS",
            "score_breakdown": breakdown,
            "notes": notes,
            "matched_real_sample": real.get("path"),
        }
    )
    return result


def _score_value(add, check: str, value: Any, real_value: Any) -> None:
    if value == real_value:
        add(0, check, f"{check} matches real sample: {value}.")
    else:
        add(1, check, f"{check} differs from real sample: got {value}, real {real_value}.")


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
        add(0, "XObjects", f"XObject/image profile matches real sample: {value}.")
    elif value[0] == 0 and real_value[0] > 0:
        add(1, "XObjects", "No reusable XObjects were found, while the real Shadowfax sample contains XObjects.")
    else:
        add(1, "XObjects", f"XObject/image profile differs from real sample: got {value}, real {real_value}.")


def _score_assets(add, stats: dict[str, Any], real_stats: dict[str, Any]) -> None:
    values = set(stats.get("asset_hashes") or [])
    real_values = set(real_stats.get("asset_hashes") or [])
    if values and values & real_values:
        add(0, "Approved branding assets", "At least one image/logo asset hash matches the real Shadowfax sample.")
    elif not real_values and not values:
        add(0, "Approved branding assets", "No image/logo asset hashes in candidate or real sample.")
    else:
        add(1, "Approved branding assets", f"No image/logo asset hash matches the real Shadowfax sample: got {_join(values)}, real {_join(real_values)}.")


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
<title>Shadowfax PDF Label Report</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2937; background: #f8fafc; }}
.table-wrap {{ overflow-x: auto; border: 1px solid #dbe2ea; background: white; }}
table {{ min-width: 2500px; width: 100%; border-collapse: collapse; background: white; }}
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
<thead>
<tr>
<th>File</th>
<th>Label name</th>
<th>Score</th>
<th>Verdict</th>
<th>Object serialization</th>
<th>Page compression</th>
<th>XObjects</th>
<th>Barcode representation</th>
<th>Approved branding assets</th>
<th>TL operators</th>
<th>Graphics state</th>
<th>Text positioning</th>
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
        f"<td>{_esc(_label_name_summary(stats, 'shadowfax'))}</td>"
        f"<td>{_esc(score)}</td>"
        f"<td class=\"verdict {_verdict_class(verdict)}\">{_esc(verdict)}</td>"
        f"<td>{_esc(_object_serialization_detail(stats))}<br>{_esc(_breakdown_detail(row, 'Object serialization'))}</td>"
        f"<td>{_esc(stats.get('page_content_compression') or '-')}<br>{_esc(_breakdown_detail(row, 'Page compression'))}</td>"
        f"<td>total={_esc(stats.get('xobject_count', 0))}<br>image/form={_esc(stats.get('image_xobject_count', 0))}/{_esc(stats.get('form_xobject_count', 0))}<br>Do/BI={_esc(stats.get('image_draw_operator_count', 0))}/{_esc(stats.get('inline_image_operator_count', 0))}<br>{_esc(_breakdown_detail(row, 'XObjects'))}</td>"
        f"<td>{_esc(stats.get('barcode_representation') or '-')}<br>{_esc(_breakdown_detail(row, 'Barcode representation'))}</td>"
        f"<td>{_esc(_asset_summary(stats))}<br>{_esc(_breakdown_detail(row, 'Approved branding assets'))}</td>"
        f"<td>TL={_esc(stats.get('tl_count', 0))}<br>{_esc(_breakdown_detail(row, 'TL operators'))}</td>"
        f"<td>{_esc(stats.get('graphics_state_summary') or '-')}<br>{_esc(_breakdown_detail(row, 'Graphics state'))}</td>"
        f"<td>Td={_esc(stats.get('td_count', 0))}<br>{_esc(_breakdown_detail(row, 'Text positioning'))}</td>"
        f"<td>{_findings_html(row)}</td>"
        "</tr>"
    )


def _breakdown_detail(row: dict[str, Any], check: str) -> str:
    details = []
    for item in row.get("score_breakdown") or []:
        if item.get("check") == check:
            detail = item.get("detail") or "-"
            status = "PASS" if item.get("passed") else "FAIL"
            details.append(f"{status}; {detail}")
    return " | ".join(details) if details else "-"


def _findings_html(row: dict[str, Any]) -> str:
    failures = [item for item in row.get("score_breakdown") or [] if item.get("passed") is False]
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
    print("SHADOWFAX REAL BASELINE")
    print(f"samples={len(real_rows)}")
    print("scoring=rule gates; any visible-column mismatch makes candidate SHADOWFAX SUSPICIOUS")
    print("")
    print("REAL SAMPLES")
    for row in real_rows:
        stats = row.get("stats") or {}
        print(
            f"{Path(row['path']).name}: score=100 "
            f"objects={stats.get('object_serialization') or '-'} "
            f"compression={stats.get('page_content_compression') or '-'} "
            f"xobjects={stats.get('xobject_count', 0)} "
            f"td={stats.get('td_count', 0)}"
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
