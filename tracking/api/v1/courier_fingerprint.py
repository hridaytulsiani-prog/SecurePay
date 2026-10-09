from __future__ import annotations

from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .label_structure_fingerprint import BaselineProfile, HiddenFingerprint, analyze_pdf, build_baseline


DEFAULT_BASELINE_ROOT = Path("test_labels")
LEGACY_CARRIER_DIRS = {"real": "delhivery"}
MIN_STRONG_SCORE = 70
MIN_WIN_MARGIN = 18


@dataclass(frozen=True)
class CourierMatch:
    courier: str
    score: int
    verdict: str
    sample_count: int
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CourierFingerprintResult:
    best_courier: str | None
    confidence: str
    is_strong_match: bool
    scores: list[CourierMatch]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "best_courier": self.best_courier,
            "confidence": self.confidence,
            "is_strong_match": self.is_strong_match,
            "reason": self.reason,
            "scores": [item.to_dict() for item in self.scores],
        }


def detect_courier_family(
    path: str | Path,
    *,
    baseline_root: str | Path = DEFAULT_BASELINE_ROOT,
    claimed_courier: str | None = None,
) -> CourierFingerprintResult:
    """Identify a courier from low-level PDF construction, not visible text.

    Visible fields such as "Courier: Blue Dart" are intentionally treated only
    as claims. The decision comes from per-courier real-sample baselines.
    """
    baseline_map = _load_baselines(str(Path(baseline_root)))
    if not baseline_map:
        return CourierFingerprintResult(
            best_courier=None,
            confidence="none",
            is_strong_match=False,
            scores=[],
            reason="No courier fingerprint baselines were found.",
        )

    fp = analyze_pdf(path)
    matches: list[CourierMatch] = []
    for courier, baseline in baseline_map.items():
        score, verdict, reasons = _score_family_similarity(fp, baseline)
        matches.append(
            CourierMatch(
                courier=courier,
                score=score,
                verdict=verdict,
                sample_count=baseline.sample_count,
                reasons=reasons[:5],
            )
        )

    matches.sort(key=lambda item: item.score, reverse=True)
    best = matches[0]
    runner_up = matches[1] if len(matches) > 1 else None
    margin = best.score - (runner_up.score if runner_up else 0)
    strong = best.score >= MIN_STRONG_SCORE and (runner_up is None or margin >= MIN_WIN_MARGIN)
    claimed = normalize_courier(claimed_courier)

    if strong:
        confidence = "strong"
        reason = f"Best hidden PDF fingerprint match is {best.courier} with score {best.score}."
        if runner_up:
            reason += f" Next best is {runner_up.courier} with score {runner_up.score}."
    elif best.score >= 60:
        confidence = "weak"
        reason = (
            f"Best hidden PDF fingerprint match is {best.courier}, but score/margin is not strong enough "
            "to trust as a courier identity."
        )
    else:
        confidence = "none"
        reason = "No courier baseline matched the PDF construction strongly."

    if claimed and strong and claimed != best.courier:
        reason += f" Visible text claims {claimed_courier}, which conflicts with the hidden fingerprint."

    return CourierFingerprintResult(
        best_courier=best.courier if strong else None,
        confidence=confidence,
        is_strong_match=strong,
        scores=matches,
        reason=reason,
    )


def normalize_courier(value: str | None, *, allow_unknown: bool = False) -> str | None:
    if not value:
        return None
    compact = "".join(ch for ch in value.casefold() if ch.isalnum())
    aliases = {
        "bluedart": "bluedart",
        "bluedartair": "bluedart",
        "bluedartsurface": "bluedart",
        "delhivery": "delhivery",
        "xpressbees": "xpressbees",
        "xpressbeessurface": "xpressbees",
        "dtdc": "dtdc",
        "shadowfax": "shadowfax",
        "ekart": "ekart",
        "ekartlogistics": "ekart",
        "ecomexpress": "ecomexpress",
    }
    for key, courier in aliases.items():
        if key in compact:
            return courier
    return compact if allow_unknown and compact else None


def _score_family_similarity(fp: HiddenFingerprint, baseline: BaselineProfile) -> tuple[int, str, list[str]]:
    score = 0
    reasons: list[str] = []

    def add(points: int, detail: str) -> None:
        nonlocal score
        score += points
        reasons.append(detail)

    if fp.has_struct_tree and fp.fingerprint_sha256 in baseline.exact_hashes:
        add(18, "Hidden StructTree fingerprint exactly matches this courier family.")
    elif fp.has_struct_tree == (baseline.structure_tree_required_count > 0):
        add(8, "StructTree presence matches this courier family.")

    for label, value, low, high, tolerance, points in [
        ("page_count", fp.page_count, baseline.page_count_min, baseline.page_count_max, 0, 8),
        ("xobject_count", fp.xobject_count, baseline.xobject_count_min, baseline.xobject_count_max, 0, 14),
        ("image_draw_ops", fp.image_draw_ops, baseline.image_draw_ops_min, baseline.image_draw_ops_max, 0, 14),
        ("path_draw_ops", fp.path_draw_ops, baseline.path_draw_ops_min, baseline.path_draw_ops_max, 8, 12),
        ("text_show_ops", fp.text_show_ops, baseline.text_show_ops_min, baseline.text_show_ops_max, 5, 8),
        ("object_count", fp.object_count, baseline.object_count_min, baseline.object_count_max, 10, 12),
        ("object_id_span", fp.object_id_span, baseline.object_id_span_min, baseline.object_id_span_max, 10, 10),
        ("embedded_font_count", fp.embedded_font_count, baseline.embedded_font_count_min, baseline.embedded_font_count_max, 0, 5),
        ("subset_font_count", fp.subset_font_count, baseline.subset_font_count_min, baseline.subset_font_count_max, 0, 5),
    ]:
        if _within(value, low, high, tolerance):
            range_text = f"{low}..{high}"
            if low <= value <= high:
                add(points, f"{label} matches baseline range {range_text}: got {value}.")
            else:
                add(points, f"{label} is within baseline tolerance {range_text}: got {value}.")

    if fp.content_operator_signature_sha256 and fp.content_operator_signature_sha256 in baseline.operator_signature_hashes:
        add(12, "Content operator signature exactly matches this courier family.")
    if fp.object_number_signature_sha256 and fp.object_number_signature_sha256 in baseline.object_number_signature_hashes:
        add(10, "Object-number/xref signature exactly matches this courier family.")
    if fp.font_signature_sha256 and fp.font_signature_sha256 in baseline.font_signature_hashes:
        add(8, "Font signature exactly matches this courier family.")
    elif not fp.font_signature_sha256 and not baseline.font_signature_hashes:
        add(5, "Both candidate and baseline have no font signature.")

    if fp.base_fonts and baseline.base_font_sets:
        import json

        if json.dumps(fp.base_fonts) in baseline.base_font_sets:
            add(6, "Base font family set matches this courier family.")
    elif not fp.base_fonts and not baseline.base_font_sets:
        add(4, "Both candidate and baseline have no extracted base font families.")

    if fp.producer and fp.producer in baseline.producer_values:
        add(4, "PDF /Producer matches this courier family.")
    elif not fp.producer and not baseline.producer_values:
        add(2, "Both candidate and baseline have empty /Producer.")

    if fp.creator and fp.creator in baseline.creator_values:
        add(4, "PDF /Creator matches this courier family.")
    elif not fp.creator and not baseline.creator_values:
        add(2, "Both candidate and baseline have empty /Creator.")

    score = max(0, min(100, score))
    if score >= MIN_STRONG_SCORE:
        verdict = "COURIER FAMILY MATCH"
    elif score >= 45:
        verdict = "COURIER FAMILY PARTIAL"
    else:
        verdict = "COURIER FAMILY MISMATCH"
    return score, verdict, reasons


def _within(value: int, low: int, high: int, tolerance: int) -> bool:
    return (low - tolerance) <= value <= (high + tolerance)


@lru_cache(maxsize=8)
def _load_baselines(root_text: str) -> dict[str, BaselineProfile]:
    root = Path(root_text)
    if not root.exists():
        return {}

    baselines: dict[str, BaselineProfile] = {}
    for real_dir in _real_sample_dirs(root):
        courier = _courier_name_for_real_dir(root, real_dir)
        files = sorted(file for file in real_dir.rglob("*.pdf") if file.is_file())
        if not courier or not files:
            continue
        baselines[courier] = build_baseline(files)
    return baselines


def _real_sample_dirs(root: Path) -> list[Path]:
    dirs = []
    if (root / "real").is_dir():
        dirs.append(root / "real")
    dirs.extend(path for path in root.glob("*/real") if path.is_dir())
    return sorted(set(dirs))


def _courier_name_for_real_dir(root: Path, real_dir: Path) -> str | None:
    rel = real_dir.relative_to(root)
    first = rel.parts[0] if rel.parts else ""
    return LEGACY_CARRIER_DIRS.get(first) or normalize_courier(first, allow_unknown=True)
