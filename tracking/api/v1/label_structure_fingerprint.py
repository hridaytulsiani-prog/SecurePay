from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NullObject, NumberObject


PDF_OPERATORS = {
    "q", "Q", "cm", "BT", "ET", "Tf", "Tm", "Td", "TD", "Tj", "TJ", "'", '"',
    "Do", "re", "m", "l", "c", "v", "y", "h", "S", "s", "f", "F", "f*", "B",
    "B*", "b", "b*", "n", "rg", "RG", "g", "G", "k", "K", "w", "J", "j", "M",
    "d", "W", "W*", "BI", "ID", "EI", "cs", "CS", "sc", "SC", "scn", "SCN",
}
TEXT_GRAMMAR_OPERATORS = {"BT", "ET", "Tf", "Tm", "Td", "TD", "Tj", "TJ", "'", '"'}
CONTENT_OPERATOR_RE = re.compile(r"(?<!/)(?:\b([A-Za-z][A-Za-z0-9\*'\"]{0,2})\b|(['\"]))")


def _resolve(obj: Any) -> Any:
    while isinstance(obj, IndirectObject):
        obj = obj.get_object()
    return obj


def _pdf_name(value: Any) -> str | None:
    value = _resolve(value)
    if value is None or isinstance(value, NullObject):
        return None
    text = str(value)
    return text[1:] if text.startswith("/") else text


def _iter_kids(value: Any) -> list[Any]:
    value = _resolve(value)
    if value is None or isinstance(value, NullObject):
        return []
    if isinstance(value, ArrayObject):
        return list(value)
    return [value]


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


@dataclass(frozen=True)
class TableShape:
    rows: int
    cells: int
    row_cells: tuple[int, ...] = ()

    def short(self) -> str:
        if not self.row_cells:
            return f"({self.rows},{self.cells})"
        return f"({self.rows} rows->{','.join(str(item) for item in self.row_cells)}, cells->{self.cells})"


@dataclass(frozen=True)
class FontFingerprint:
    raw_name: str
    base_name: str
    subtype: str
    encoding: str
    embedded: bool
    subset: bool

    def short(self) -> str:
        flags = []
        flags.append("embedded" if self.embedded else "not_embedded")
        flags.append("subset" if self.subset else "full")
        enc = self.encoding or "none"
        sub = self.subtype or "unknown"
        return f"{self.raw_name}|{sub}|{enc}|{'+'.join(flags)}"


@dataclass(frozen=True)
class HiddenFingerprint:
    path: str
    has_struct_tree: bool
    nodes: int
    max_depth: int
    role_counts: dict[str, int]
    tables: int
    rows: int
    cells: int
    table_shapes: list[TableShape]
    fingerprint_sha256: str
    canonical_json: str = ""
    font_fingerprints: list[FontFingerprint] = field(default_factory=list)
    font_signature_sha256: str = ""
    base_fonts: list[str] = field(default_factory=list)
    embedded_font_count: int = 0
    subset_font_count: int = 0
    content_operator_counts: dict[str, int] = field(default_factory=dict)
    content_operator_signature_sha256: str = ""
    content_grammar_sequence_sha256: str = ""
    content_grammar_trigrams: dict[str, int] = field(default_factory=dict)
    content_grammar_sample: list[str] = field(default_factory=list)
    td_tf_tj_patterns: int = 0
    t_func_operator_count: int = 0
    t_array_operator_count: int = 0
    text_show_ops: int = 0
    image_draw_ops: int = 0
    path_draw_ops: int = 0
    marked_content_ops: int = 0
    mcid_count: int = 0
    mcid_sequence_sha256: str = ""
    xobject_count: int = 0
    object_count: int = 0
    object_id_span: int = 0
    object_id_deltas: list[int] = field(default_factory=list)
    object_number_signature_sha256: str = ""
    producer: str = ""
    creator: str = ""
    metadata_signature_sha256: str = ""
    page_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["table_shapes"] = [asdict(shape) for shape in self.table_shapes]
        data["font_fingerprints"] = [asdict(item) for item in self.font_fingerprints]
        return data


@dataclass(frozen=True)
class BaselineProfile:
    sample_paths: list[str]
    sample_count: int
    exact_hashes: list[str]
    nodes_min: int
    nodes_max: int
    depth_min: int
    depth_max: int
    nonstruct_min: int
    nonstruct_max: int
    tables_min: int
    tables_max: int
    rows_min: int
    rows_max: int
    cells_min: int
    cells_max: int
    allowed_shapes: list[str]
    role_ranges: dict[str, dict[str, int]]
    structure_tree_required_count: int
    font_signature_hashes: list[str]
    base_font_sets: list[str]
    embedded_font_count_min: int
    embedded_font_count_max: int
    subset_font_count_min: int
    subset_font_count_max: int
    operator_signature_hashes: list[str]
    grammar_sequence_hashes: list[str]
    grammar_trigram_ranges: dict[str, dict[str, int]]
    td_tf_tj_patterns_min: int
    td_tf_tj_patterns_max: int
    t_func_operator_count_min: int
    t_func_operator_count_max: int
    t_array_operator_count_min: int
    t_array_operator_count_max: int
    operator_count_ranges: dict[str, dict[str, int]]
    text_show_ops_min: int
    text_show_ops_max: int
    image_draw_ops_min: int
    image_draw_ops_max: int
    path_draw_ops_min: int
    path_draw_ops_max: int
    marked_content_ops_min: int
    marked_content_ops_max: int
    mcid_count_min: int
    mcid_count_max: int
    mcid_signature_hashes: list[str]
    object_count_min: int
    object_count_max: int
    object_id_span_min: int
    object_id_span_max: int
    object_number_signature_hashes: list[str]
    metadata_signature_hashes: list[str]
    producer_values: list[str]
    creator_values: list[str]
    page_count_min: int
    page_count_max: int
    xobject_count_min: int
    xobject_count_max: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComparisonResult:
    path: str
    verdict: str
    score: int
    exact_hash_match: bool
    reasons: list[str] = field(default_factory=list)
    score_breakdown: list[dict[str, Any]] = field(default_factory=list)
    fingerprint: HiddenFingerprint | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.fingerprint is not None:
            data["fingerprint"] = self.fingerprint.to_dict()
        return data


def analyze_pdf(path: str | Path) -> HiddenFingerprint:
    file_path = str(Path(path))
    reader = PdfReader(file_path)
    root = reader.trailer["/Root"]
    struct_root = _resolve(root.get("/StructTreeRoot"))

    role_counts: Counter[str] = Counter()
    table_shapes: list[TableShape] = []
    nodes = 0
    max_depth = 0
    mcid_values: list[int] = []

    def walk(obj: Any, depth: int) -> Any:
        nonlocal nodes, max_depth
        obj = _resolve(obj)
        if obj is None or isinstance(obj, NullObject):
            return None

        if isinstance(obj, ArrayObject):
            children = []
            for item in obj:
                branch = walk(item, depth)
                if branch is not None:
                    children.append(branch)
            return children

        if isinstance(obj, NumberObject):
            value = int(obj)
            mcid_values.append(value)
            return {"kind": "mcid", "value": value}

        if isinstance(obj, DictionaryObject):
            role = _pdf_name(obj.get("/S"))
            if role:
                nodes += 1
                max_depth = max(max_depth, depth)
                role_counts[role] += 1
                kids = []
                for item in _iter_kids(obj.get("/K")):
                    branch = walk(item, depth + 1)
                    if branch is not None:
                        kids.append(branch)
                if role == "Table":
                    table_shapes.append(_table_shape(obj))
                return {"role": role, "kids": kids}

            if "/MCID" in obj:
                mcid = _safe_int(obj["/MCID"])
                mcid_values.append(mcid)
                return {"kind": "mcr", "mcid": mcid}

            obj_type = _pdf_name(obj.get("/Type"))
            if obj_type == "OBJR":
                return {"kind": "objr"}

            kids = []
            for item in _iter_kids(obj.get("/K")):
                branch = walk(item, depth)
                if branch is not None:
                    kids.append(branch)
            if kids:
                return {"kind": obj_type or "container", "kids": kids}
            return {"kind": obj_type or "dict"}

        return str(obj)

    has_struct_tree = isinstance(struct_root, DictionaryObject)
    tree = walk(struct_root.get("/K"), 1) if has_struct_tree else None
    canonical = json.dumps(tree, separators=(",", ":"), ensure_ascii=False) if tree is not None else ""
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest() if canonical else ""

    fonts = _collect_fonts(reader)
    font_signature_input = [item.short() for item in fonts]
    font_signature = _sha(font_signature_input)

    content_ops = _collect_content_operators(reader)
    content_counts = Counter(content_ops)
    operator_signature = _sha([f"{key}:{content_counts[key]}" for key in sorted(content_counts)])
    grammar_sequence = [op for op in content_ops if op in TEXT_GRAMMAR_OPERATORS]
    grammar_sequence_signature = _sha(grammar_sequence)
    grammar_trigrams = Counter(
        ">".join(grammar_sequence[index : index + 3])
        for index in range(max(0, len(grammar_sequence) - 2))
    )

    metadata = reader.metadata or {}
    producer = str(metadata.get("/Producer", "") or "")
    creator = str(metadata.get("/Creator", "") or "")
    metadata_signature = _sha(
        [
            producer,
            creator,
            str(metadata.get("/CreationDate", "") or ""),
            str(metadata.get("/ModDate", "") or ""),
        ]
    )

    object_ids = _collect_object_ids(reader)
    object_deltas = _object_deltas(object_ids)
    object_signature = _sha([str(item) for item in object_deltas])

    base_fonts = sorted({item.base_name for item in fonts})
    xobject_count = _count_xobjects(reader)

    return HiddenFingerprint(
        path=file_path,
        has_struct_tree=has_struct_tree,
        nodes=nodes,
        max_depth=max_depth,
        role_counts=dict(sorted(role_counts.items())),
        tables=role_counts.get("Table", 0),
        rows=role_counts.get("TR", 0),
        cells=role_counts.get("TD", 0) + role_counts.get("TH", 0),
        table_shapes=table_shapes,
        fingerprint_sha256=digest,
        canonical_json=canonical,
        font_fingerprints=fonts,
        font_signature_sha256=font_signature,
        base_fonts=base_fonts,
        embedded_font_count=sum(1 for item in fonts if item.embedded),
        subset_font_count=sum(1 for item in fonts if item.subset),
        content_operator_counts=dict(sorted(content_counts.items())),
        content_operator_signature_sha256=operator_signature,
        content_grammar_sequence_sha256=grammar_sequence_signature,
        content_grammar_trigrams=dict(sorted(grammar_trigrams.items())),
        content_grammar_sample=grammar_sequence[:80],
        td_tf_tj_patterns=grammar_trigrams.get("Td>Tf>TJ", 0) + grammar_trigrams.get("Tf>Td>TJ", 0),
        t_func_operator_count=content_counts.get("Tj", 0),
        t_array_operator_count=content_counts.get("TJ", 0),
        text_show_ops=content_counts.get("Tj", 0) + content_counts.get("TJ", 0) + content_counts.get("'", 0) + content_counts.get('"', 0),
        image_draw_ops=content_counts.get("Do", 0),
        path_draw_ops=sum(content_counts.get(key, 0) for key in ("re", "m", "l", "c", "v", "y", "h", "S", "s", "f", "F", "f*", "B", "B*", "b", "b*")),
        marked_content_ops=sum(content_counts.get(key, 0) for key in ("BDC", "BMC", "EMC", "MP", "DP")),
        mcid_count=len(mcid_values),
        mcid_sequence_sha256=_sha([str(item) for item in mcid_values]),
        xobject_count=xobject_count,
        object_count=len(object_ids),
        object_id_span=(object_ids[-1] - object_ids[0]) if object_ids else 0,
        object_id_deltas=object_deltas[:25],
        object_number_signature_sha256=object_signature,
        producer=producer,
        creator=creator,
        metadata_signature_sha256=metadata_signature,
        page_count=len(reader.pages),
    )


def _table_shape(table_obj: DictionaryObject) -> TableShape:
    row_nodes = _collect_rows(table_obj)
    row_cells: list[int] = []
    total_cells = 0
    for row in row_nodes:
        count = _count_row_cells(row)
        row_cells.append(count)
        total_cells += count
    return TableShape(rows=len(row_nodes), cells=total_cells, row_cells=tuple(row_cells))


def _collect_rows(node: Any) -> list[DictionaryObject]:
    node = _resolve(node)
    rows: list[DictionaryObject] = []

    if isinstance(node, DictionaryObject):
        role = _pdf_name(node.get("/S"))
        if role == "TR":
            rows.append(node)
        for item in _iter_kids(node.get("/K")):
            rows.extend(_collect_rows(item))
    elif isinstance(node, ArrayObject):
        for item in node:
            rows.extend(_collect_rows(item))

    return rows


def _count_row_cells(row_obj: DictionaryObject) -> int:
    count = 0

    def visit(node: Any) -> None:
        nonlocal count
        node = _resolve(node)
        if isinstance(node, DictionaryObject):
            role = _pdf_name(node.get("/S"))
            if role in {"TD", "TH"}:
                count += 1
            for item in _iter_kids(node.get("/K")):
                visit(item)
        elif isinstance(node, ArrayObject):
            for item in node:
                visit(item)

    visit(row_obj.get("/K"))
    return count


def _collect_fonts(reader: PdfReader) -> list[FontFingerprint]:
    seen: dict[str, FontFingerprint] = {}
    for page in reader.pages:
        resources = _resolve(page.get("/Resources"))
        if not isinstance(resources, DictionaryObject):
            continue
        font_dict = _resolve(resources.get("/Font"))
        if not isinstance(font_dict, DictionaryObject):
            continue
        for key, value in font_dict.items():
            font_obj = _resolve(value)
            if not isinstance(font_obj, DictionaryObject):
                continue
            raw_name = _pdf_name(font_obj.get("/BaseFont")) or _pdf_name(key) or "unknown"
            subset = bool(re.match(r"^[A-Z]{6}\+", raw_name))
            base_name = raw_name.split("+", 1)[1] if subset and "+" in raw_name else raw_name
            subtype = _pdf_name(font_obj.get("/Subtype")) or ""
            encoding = _pdf_name(font_obj.get("/Encoding")) or ""
            descriptor = _resolve(font_obj.get("/FontDescriptor"))
            embedded = isinstance(descriptor, DictionaryObject) and any(
                descriptor.get(name) is not None for name in ("/FontFile", "/FontFile2", "/FontFile3")
            )
            signature = f"{base_name}|{subtype}|{encoding}|{embedded}|{subset}"
            if signature not in seen:
                seen[signature] = FontFingerprint(
                    raw_name=raw_name,
                    base_name=base_name,
                    subtype=subtype,
                    encoding=encoding,
                    embedded=embedded,
                    subset=subset,
                )
    return sorted(seen.values(), key=lambda item: item.short())


def _collect_content_operators(reader: PdfReader) -> list[str]:
    operators: list[str] = []
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
        text = data.decode("latin-1", errors="ignore")
        for match in CONTENT_OPERATOR_RE.finditer(text):
            op = match.group(1) or match.group(2) or ""
            if op in PDF_OPERATORS or op in {"BDC", "BMC", "EMC", "MP", "DP"}:
                operators.append(op)
    return operators


def _collect_content_operator_counts(reader: PdfReader) -> Counter[str]:
    return Counter(_collect_content_operators(reader))


def _collect_object_ids(reader: PdfReader) -> list[int]:
    object_ids: set[int] = set()
    xref = getattr(reader, "xref", None)
    if isinstance(xref, dict):
        for _, section in xref.items():
            if isinstance(section, dict):
                for key in section.keys():
                    try:
                        object_ids.add(int(key))
                    except Exception:
                        continue
    return sorted(item for item in object_ids if item > 0)


def _object_deltas(object_ids: list[int]) -> list[int]:
    if len(object_ids) < 2:
        return []
    return [current - previous for previous, current in zip(object_ids, object_ids[1:])]


def _count_xobjects(reader: PdfReader) -> int:
    count = 0
    for page in reader.pages:
        resources = _resolve(page.get("/Resources"))
        if not isinstance(resources, DictionaryObject):
            continue
        xobjects = _resolve(resources.get("/XObject"))
        if isinstance(xobjects, DictionaryObject):
            count += len(xobjects)
    return count


def _sha(parts: list[str]) -> str:
    if not parts:
        return ""
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()


def _is_fpdf_family(fp: HiddenFingerprint) -> bool:
    producer = (fp.producer or "").lower()
    creator = (fp.creator or "").lower()
    return "fpdf" in producer or "fpdf" in creator


def build_baseline(paths: list[str | Path]) -> BaselineProfile:
    fingerprints = [analyze_pdf(path) for path in paths]
    if not fingerprints:
        raise ValueError("at least one real label is required to build a baseline")

    role_keys = sorted({key for fp in fingerprints for key in fp.role_counts})
    role_ranges = {
        key: {
            "min": min(fp.role_counts.get(key, 0) for fp in fingerprints),
            "max": max(fp.role_counts.get(key, 0) for fp in fingerprints),
        }
        for key in role_keys
    }

    allowed_shapes = sorted(
        {json.dumps(asdict(shape), sort_keys=True) for fp in fingerprints for shape in fp.table_shapes}
    )

    op_keys = sorted({key for fp in fingerprints for key in fp.content_operator_counts})
    op_ranges = {
        key: {
            "min": min(fp.content_operator_counts.get(key, 0) for fp in fingerprints),
            "max": max(fp.content_operator_counts.get(key, 0) for fp in fingerprints),
        }
        for key in op_keys
    }
    grammar_trigram_keys = sorted({key for fp in fingerprints for key in fp.content_grammar_trigrams})
    grammar_trigram_ranges = {
        key: {
            "min": min(fp.content_grammar_trigrams.get(key, 0) for fp in fingerprints),
            "max": max(fp.content_grammar_trigrams.get(key, 0) for fp in fingerprints),
        }
        for key in grammar_trigram_keys
    }

    return BaselineProfile(
        sample_paths=[fp.path for fp in fingerprints],
        sample_count=len(fingerprints),
        exact_hashes=sorted({fp.fingerprint_sha256 for fp in fingerprints if fp.fingerprint_sha256}),
        nodes_min=min(fp.nodes for fp in fingerprints),
        nodes_max=max(fp.nodes for fp in fingerprints),
        depth_min=min(fp.max_depth for fp in fingerprints),
        depth_max=max(fp.max_depth for fp in fingerprints),
        nonstruct_min=min(fp.role_counts.get("NonStruct", 0) for fp in fingerprints),
        nonstruct_max=max(fp.role_counts.get("NonStruct", 0) for fp in fingerprints),
        tables_min=min(fp.tables for fp in fingerprints),
        tables_max=max(fp.tables for fp in fingerprints),
        rows_min=min(fp.rows for fp in fingerprints),
        rows_max=max(fp.rows for fp in fingerprints),
        cells_min=min(fp.cells for fp in fingerprints),
        cells_max=max(fp.cells for fp in fingerprints),
        allowed_shapes=allowed_shapes,
        role_ranges=role_ranges,
        structure_tree_required_count=sum(1 for fp in fingerprints if fp.has_struct_tree),
        font_signature_hashes=sorted({fp.font_signature_sha256 for fp in fingerprints if fp.font_signature_sha256}),
        base_font_sets=sorted({json.dumps(fp.base_fonts) for fp in fingerprints if fp.base_fonts}),
        embedded_font_count_min=min(fp.embedded_font_count for fp in fingerprints),
        embedded_font_count_max=max(fp.embedded_font_count for fp in fingerprints),
        subset_font_count_min=min(fp.subset_font_count for fp in fingerprints),
        subset_font_count_max=max(fp.subset_font_count for fp in fingerprints),
        operator_signature_hashes=sorted({fp.content_operator_signature_sha256 for fp in fingerprints if fp.content_operator_signature_sha256}),
        grammar_sequence_hashes=sorted({fp.content_grammar_sequence_sha256 for fp in fingerprints if fp.content_grammar_sequence_sha256}),
        grammar_trigram_ranges=grammar_trigram_ranges,
        td_tf_tj_patterns_min=min(fp.td_tf_tj_patterns for fp in fingerprints),
        td_tf_tj_patterns_max=max(fp.td_tf_tj_patterns for fp in fingerprints),
        t_func_operator_count_min=min(fp.t_func_operator_count for fp in fingerprints),
        t_func_operator_count_max=max(fp.t_func_operator_count for fp in fingerprints),
        t_array_operator_count_min=min(fp.t_array_operator_count for fp in fingerprints),
        t_array_operator_count_max=max(fp.t_array_operator_count for fp in fingerprints),
        operator_count_ranges=op_ranges,
        text_show_ops_min=min(fp.text_show_ops for fp in fingerprints),
        text_show_ops_max=max(fp.text_show_ops for fp in fingerprints),
        image_draw_ops_min=min(fp.image_draw_ops for fp in fingerprints),
        image_draw_ops_max=max(fp.image_draw_ops for fp in fingerprints),
        path_draw_ops_min=min(fp.path_draw_ops for fp in fingerprints),
        path_draw_ops_max=max(fp.path_draw_ops for fp in fingerprints),
        marked_content_ops_min=min(fp.marked_content_ops for fp in fingerprints),
        marked_content_ops_max=max(fp.marked_content_ops for fp in fingerprints),
        mcid_count_min=min(fp.mcid_count for fp in fingerprints),
        mcid_count_max=max(fp.mcid_count for fp in fingerprints),
        mcid_signature_hashes=sorted({fp.mcid_sequence_sha256 for fp in fingerprints if fp.mcid_sequence_sha256}),
        object_count_min=min(fp.object_count for fp in fingerprints),
        object_count_max=max(fp.object_count for fp in fingerprints),
        object_id_span_min=min(fp.object_id_span for fp in fingerprints),
        object_id_span_max=max(fp.object_id_span for fp in fingerprints),
        object_number_signature_hashes=sorted({fp.object_number_signature_sha256 for fp in fingerprints if fp.object_number_signature_sha256}),
        metadata_signature_hashes=sorted({fp.metadata_signature_sha256 for fp in fingerprints if fp.metadata_signature_sha256}),
        producer_values=sorted({fp.producer for fp in fingerprints if fp.producer}),
        creator_values=sorted({fp.creator for fp in fingerprints if fp.creator}),
        page_count_min=min(fp.page_count for fp in fingerprints),
        page_count_max=max(fp.page_count for fp in fingerprints),
        xobject_count_min=min(fp.xobject_count for fp in fingerprints),
        xobject_count_max=max(fp.xobject_count for fp in fingerprints),
    )


def compare_to_baseline(path: str | Path, baseline: BaselineProfile) -> ComparisonResult:
    fp = analyze_pdf(path)
    reasons: list[str] = []
    breakdown: list[dict[str, Any]] = []
    score = 40

    def add_breakdown(check: str, delta: int, detail: str) -> None:
        breakdown.append({"check": check, "delta": delta, "detail": detail})

    if fp.has_struct_tree:
        if fp.fingerprint_sha256 in baseline.exact_hashes:
            detail = "StructTreeRoot exists and the structure fingerprint exactly matches a trusted real sample."
            reasons.append(detail)
            score += 30
            add_breakdown("StructTreeRoot", 30, detail)
        else:
            penalty = 12
            score -= penalty
            detail = "StructTreeRoot exists, but the hidden structure fingerprint does not exactly match the real baseline."
            reasons.append(detail)
            add_breakdown("StructTreeRoot", -penalty, detail)
    else:
        if baseline.structure_tree_required_count == baseline.sample_count:
            penalty = 45
            score -= penalty
            detail = "Real baseline PDFs all had StructTreeRoot, but this PDF has none."
            reasons.append(detail)
            add_breakdown("StructTreeRoot", -penalty, detail)
        else:
            penalty = 15
            score -= penalty
            detail = "No StructTreeRoot was found; this matches some real samples, so this signal is weak by itself."
            reasons.append(detail)
            add_breakdown("StructTreeRoot", -penalty, detail)

    for label, value, low, high, tolerance, cap in [
        ("Structure node count", fp.nodes, baseline.nodes_min, baseline.nodes_max, 3, 14),
        ("Structure depth", fp.max_depth, baseline.depth_min, baseline.depth_max, 1, 12),
        ("/NonStruct count", fp.role_counts.get("NonStruct", 0), baseline.nonstruct_min, baseline.nonstruct_max, 2, 10),
        ("Table count", fp.tables, baseline.tables_min, baseline.tables_max, 0, 14),
        ("Row count", fp.rows, baseline.rows_min, baseline.rows_max, 1, 10),
        ("Cell count", fp.cells, baseline.cells_min, baseline.cells_max, 1, 10),
    ]:
        penalty, detail = _range_penalty_detail(value, low, high, tolerance, cap, label)
        score -= penalty
        add_breakdown(label, -penalty, detail)
        if penalty:
            reasons.append(detail)

    candidate_shapes = {json.dumps(asdict(shape), sort_keys=True) for shape in fp.table_shapes}
    baseline_shapes = set(baseline.allowed_shapes)
    if candidate_shapes and baseline_shapes and not candidate_shapes.issubset(baseline_shapes):
        penalty = 16
        score -= penalty
        detail = "Table shape pattern differs from the real-label structure baseline."
        reasons.append(detail)
        add_breakdown("Table shape pattern", -penalty, detail)
    else:
        if candidate_shapes:
            score += 6
            add_breakdown("Table shape pattern", 6, "Table shape pattern is within the trusted real baseline.")
        else:
            add_breakdown("Table shape pattern", 0, "No table shape pattern was extracted.")

    penalty, detail = _hash_penalty_detail(
        fp.font_signature_sha256,
        baseline.font_signature_hashes,
        18,
        "Embedded-font fingerprint differs from the real generator pattern.",
        "Embedded-font fingerprint matches a trusted real sample.",
    )
    score -= penalty
    add_breakdown("Embedded font fingerprint", -penalty, detail)
    if penalty or "matches" in detail:
        reasons.append(detail)
    if penalty == 0 and fp.font_signature_sha256:
        score += 12
        add_breakdown("Embedded font fingerprint match bonus", 12, "Embedded-font fingerprint matches a trusted real sample.")

    if fp.base_fonts:
        candidate_font_set = json.dumps(fp.base_fonts)
        if baseline.base_font_sets and candidate_font_set not in baseline.base_font_sets:
            penalty = 12
            score -= penalty
            detail = f"Base font family set differs from the trusted real PDFs: {', '.join(fp.base_fonts)}."
            reasons.append(detail)
            add_breakdown("Base font family set", -penalty, detail)
        else:
            score += 8
            add_breakdown("Base font family set", 8, "Base font family set matches a trusted real family.")
    else:
        add_breakdown("Base font family set", 0, "No base font family names were extracted.")

    for label, value, low, high, tolerance, cap in [
        ("Embedded font count", fp.embedded_font_count, baseline.embedded_font_count_min, baseline.embedded_font_count_max, 0, 10),
        ("Subset font count", fp.subset_font_count, baseline.subset_font_count_min, baseline.subset_font_count_max, 0, 10),
    ]:
        penalty, detail = _range_penalty_detail(value, low, high, tolerance, cap, label)
        if _is_fpdf_family(fp):
            penalty = 0
            detail = f"{label} is informational for FPDF-family PDFs."
        score -= penalty
        add_breakdown(label, -penalty, detail)
        if penalty:
            reasons.append(detail)

    penalty, detail = _hash_penalty_detail(
        fp.content_operator_signature_sha256,
        baseline.operator_signature_hashes,
        20,
        "Content-stream drawing/text operator pattern differs from the real generator.",
        "Content-stream drawing/text operator pattern matches a trusted real sample.",
    )
    score -= penalty
    add_breakdown("Content-stream fingerprint", -penalty, detail)
    if penalty or "matches" in detail:
        reasons.append(detail)
    if penalty == 0 and fp.content_operator_signature_sha256:
        score += 14
        add_breakdown("Content-stream fingerprint match bonus", 14, "Content-stream drawing/text operator pattern matches a trusted real sample.")

    penalty, detail = _hash_penalty_detail(
        fp.content_grammar_sequence_sha256,
        baseline.grammar_sequence_hashes,
        24,
        "Ordered text grammar sequence differs from the real generator.",
        "Ordered text grammar sequence matches a trusted real sample.",
    )
    score -= penalty
    add_breakdown("Content grammar sequence", -penalty, detail)
    if penalty or "matches" in detail:
        reasons.append(detail)
    if penalty == 0 and fp.content_grammar_sequence_sha256:
        score += 16
        add_breakdown("Content grammar sequence match bonus", 16, "Ordered text grammar sequence matches a trusted real sample.")

    for label, value, low, high, tolerance, cap in [
        ("Td/Tf/TJ grammar pattern count", fp.td_tf_tj_patterns, baseline.td_tf_tj_patterns_min, baseline.td_tf_tj_patterns_max, 0, 16),
        ("Tj simple text operator count", fp.t_func_operator_count, baseline.t_func_operator_count_min, baseline.t_func_operator_count_max, 0, 18),
        ("TJ array text operator count", fp.t_array_operator_count, baseline.t_array_operator_count_min, baseline.t_array_operator_count_max, 0, 18),
    ]:
        penalty, detail = _range_penalty_detail(value, low, high, tolerance, cap, label)
        score -= penalty
        add_breakdown(label, -penalty, detail)
        if penalty:
            reasons.append(detail)

    for label, value, low, high, tolerance, cap in [
        ("Text-show operator count", fp.text_show_ops, baseline.text_show_ops_min, baseline.text_show_ops_max, 3, 10),
        ("Image draw operator count", fp.image_draw_ops, baseline.image_draw_ops_min, baseline.image_draw_ops_max, 1, 18),
        ("Path draw operator count", fp.path_draw_ops, baseline.path_draw_ops_min, baseline.path_draw_ops_max, 5, 14),
        ("Marked-content operator count", fp.marked_content_ops, baseline.marked_content_ops_min, baseline.marked_content_ops_max, 1, 6),
    ]:
        penalty, detail = _range_penalty_detail(value, low, high, tolerance, cap, label)
        score -= penalty
        add_breakdown(label, -penalty, detail)
        if penalty:
            reasons.append(detail)

    penalty, detail = _image_xobject_penalty(fp, baseline)
    score -= penalty
    add_breakdown("Image/XObject consistency", -penalty, detail)
    if penalty:
        reasons.append(detail)

    focus_ops = ["BT", "ET", "Tf", "Tm", "Td", "TD", "Tj", "TJ", "Do", "re", "cm", "q", "Q", "BDC", "EMC"]
    for op in focus_ops:
        span = baseline.operator_count_ranges.get(op)
        if not span:
            continue
        value = fp.content_operator_counts.get(op, 0)
        if value < span["min"] or value > span["max"]:
            penalty = 6
            score -= penalty
            detail = f"Content operator /{op} count drift: got {value}, baseline {span['min']}..{span['max']}."
            reasons.append(detail)
            add_breakdown(f"Operator /{op}", -penalty, detail)

    penalty, detail = _hash_penalty_detail(
        fp.mcid_sequence_sha256,
        baseline.mcid_signature_hashes,
        14,
        "MCID/marked-content sequence differs from the trusted real PDFs.",
        "MCID/marked-content sequence matches a trusted real sample.",
        only_if_value=True,
    )
    score -= penalty
    add_breakdown("MCID sequence", -penalty, detail)
    if penalty or "matches" in detail:
        reasons.append(detail)
    if penalty == 0 and fp.mcid_sequence_sha256:
        score += 8
        add_breakdown("MCID sequence match bonus", 8, "MCID/marked-content sequence matches a trusted real sample.")
    penalty, detail = _range_penalty_detail(fp.mcid_count, baseline.mcid_count_min, baseline.mcid_count_max, 1, 12, "MCID count")
    score -= penalty
    add_breakdown("MCID count", -penalty, detail)
    if penalty:
        reasons.append(detail)

    penalty, detail = _hash_penalty_detail(
        fp.object_number_signature_sha256,
        baseline.object_number_signature_hashes,
        14,
        "Object numbering / xref delta pattern differs from the real generator.",
        "Object numbering / xref delta pattern matches a trusted real sample.",
        only_if_value=True,
    )
    score -= penalty
    add_breakdown("Xref/object numbering hash", -penalty, detail)
    if penalty or "matches" in detail:
        reasons.append(detail)
    if penalty == 0 and fp.object_number_signature_sha256:
        score += 6
        add_breakdown("Xref/object numbering match bonus", 6, "Object numbering / xref delta pattern matches a trusted real sample.")
    for label, value, low, high, tolerance, cap in [
        ("Object count", fp.object_count, baseline.object_count_min, baseline.object_count_max, 10, 12),
        ("Object-id span", fp.object_id_span, baseline.object_id_span_min, baseline.object_id_span_max, 10, 12),
    ]:
        penalty, detail = _range_penalty_detail(value, low, high, tolerance, cap, label)
        score -= penalty
        add_breakdown(label, -penalty, detail)
        if penalty:
            reasons.append(detail)

    penalty, detail = _hash_penalty_detail(
        fp.metadata_signature_sha256,
        baseline.metadata_signature_hashes,
        10,
        "Producer/creator metadata signature differs from the real PDFs.",
        "Producer/creator metadata signature matches a trusted real sample.",
        only_if_value=True,
    )
    score -= penalty
    add_breakdown("Producer/creator metadata hash", -penalty, detail)
    if penalty or "matches" in detail:
        reasons.append(detail)
    if penalty == 0 and fp.metadata_signature_sha256:
        score += 6
        add_breakdown("Producer/creator metadata match bonus", 6, "Producer/creator metadata signature matches a trusted real sample.")

    if baseline.producer_values and fp.producer and fp.producer not in baseline.producer_values:
        penalty = 12
        score -= penalty
        detail = f"PDF /Producer differs from the real baseline: {fp.producer}."
        reasons.append(detail)
        add_breakdown("Producer", -penalty, detail)
    else:
        if fp.producer:
            score += 5
            add_breakdown("Producer", 5, "PDF /Producer is within the trusted real family set.")
        else:
            add_breakdown("Producer", 0, "PDF /Producer is empty.")
    if baseline.creator_values and fp.creator and fp.creator not in baseline.creator_values:
        penalty = 12
        score -= penalty
        detail = f"PDF /Creator differs from the real baseline: {fp.creator}."
        reasons.append(detail)
        add_breakdown("Creator", -penalty, detail)
    else:
        if fp.creator:
            score += 5
            add_breakdown("Creator", 5, "PDF /Creator is within the trusted real family set.")
        else:
            add_breakdown("Creator", 0, "PDF /Creator is empty.")

    for label, value, low, high, tolerance, cap in [
        ("Page count", fp.page_count, baseline.page_count_min, baseline.page_count_max, 0, 8),
        ("XObject count", fp.xobject_count, baseline.xobject_count_min, baseline.xobject_count_max, 0, 24),
    ]:
        penalty, detail = _range_penalty_detail(value, low, high, tolerance, cap, label)
        score -= penalty
        add_breakdown(label, -penalty, detail)
        if penalty:
            reasons.append(detail)

    score = max(0, min(100, score))
    if score >= 90:
        verdict = "MATCHES REAL HIDDEN FINGERPRINT"
    elif score >= 70:
        verdict = "PARTIALLY MATCHES REAL HIDDEN FINGERPRINT"
    else:
        verdict = "HIDDEN FINGERPRINT SUSPICIOUS"

    return ComparisonResult(
        path=fp.path,
        verdict=verdict,
        score=score,
        exact_hash_match=fp.fingerprint_sha256 in baseline.exact_hashes if fp.fingerprint_sha256 else False,
        reasons=reasons,
        score_breakdown=breakdown,
        fingerprint=fp,
    )


def render_html_report(
    baseline: BaselineProfile,
    real_results: list[HiddenFingerprint],
    candidate_results: list[ComparisonResult],
    out_path: str | Path,
) -> str:
    out_file = Path(out_path)
    rows: list[str] = []

    def add_row(kind: str, name: str, verdict: str, score: str, fp: HiddenFingerprint, reasons: list[str]) -> None:
        rows.append(
            "<tr>"
            f"<td>{_html_escape(kind)}</td>"
            f"<td>{_html_escape(name)}</td>"
            f"<td>{_html_escape(verdict)}</td>"
            f"<td>{_html_escape(score)}</td>"
            f"<td>{'yes' if fp.has_struct_tree else 'no'}</td>"
            f"<td>{fp.nodes}</td>"
            f"<td>{fp.max_depth}</td>"
            f"<td>{fp.embedded_font_count}</td>"
            f"<td>{fp.subset_font_count}</td>"
            f"<td>{fp.text_show_ops}</td>"
            f"<td>{_html_escape(_grammar_summary(fp))}</td>"
            f"<td>{fp.image_draw_ops}</td>"
            f"<td>{fp.path_draw_ops}</td>"
            f"<td>{fp.mcid_count}</td>"
            f"<td>{fp.object_count}</td>"
            f"<td>{fp.object_id_span}</td>"
            f"<td><code>{_html_escape(fp.fingerprint_sha256 or '-')}</code></td>"
            f"<td><code>{_html_escape(fp.font_signature_sha256 or '-')}</code></td>"
            f"<td><code>{_html_escape(fp.content_operator_signature_sha256 or '-')}</code></td>"
            f"<td><code>{_html_escape(fp.object_number_signature_sha256 or '-')}</code></td>"
            f"<td>{_html_escape(', '.join(fp.base_fonts) or '-')}</td>"
            f"<td>{_html_escape(', '.join(item.short() for item in fp.table_shapes) or '-')}</td>"
            f"<td>{'<br>'.join(_html_escape(item) for item in reasons) or '-'}</td>"
            "</tr>"
        )

    for fp in real_results:
        add_row("real", Path(fp.path).name, "baseline sample", "-", fp, [])
    for result in candidate_results:
        if result.fingerprint is not None:
            add_row("candidate", Path(result.path).name, result.verdict, str(result.score), result.fingerprint, result.reasons)

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shipment Label Hidden Fingerprint Report</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2937; background: #f8fafc; }}
h1, h2 {{ margin: 0 0 12px; }}
.card {{ background: white; border: 1px solid #dbe2ea; border-radius: 10px; padding: 16px 18px; margin-bottom: 18px; }}
table {{ width: 100%; border-collapse: collapse; background: white; }}
th, td {{ border: 1px solid #dbe2ea; padding: 10px; vertical-align: top; text-align: left; font-size: 13px; }}
th {{ background: #eef2f7; }}
code {{ font-family: Consolas, monospace; font-size: 12px; }}
</style>
</head>
<body>
<h1>Shipment Label Hidden Fingerprint Report</h1>
<div class="card">
  <h2>Baseline</h2>
  <div>Samples: {baseline.sample_count}</div>
  <div>Structure trees present: {baseline.structure_tree_required_count}/{baseline.sample_count}</div>
  <div>Embedded font count: {baseline.embedded_font_count_min}..{baseline.embedded_font_count_max}</div>
  <div>Subset font count: {baseline.subset_font_count_min}..{baseline.subset_font_count_max}</div>
  <div>Text-show ops: {baseline.text_show_ops_min}..{baseline.text_show_ops_max}</div>
  <div>MCID count: {baseline.mcid_count_min}..{baseline.mcid_count_max}</div>
  <div>Object count: {baseline.object_count_min}..{baseline.object_count_max}</div>
</div>
<div class="card">
  <table>
    <thead>
      <tr>
        <th>Kind</th>
        <th>File</th>
        <th>Verdict</th>
        <th>Score</th>
        <th>StructTreeRoot</th>
        <th>Nodes</th>
        <th>Depth</th>
        <th>Embedded Fonts</th>
        <th>Subset Fonts</th>
        <th>Text Ops</th>
        <th>Content Grammar</th>
        <th>Image Ops</th>
        <th>Path Ops</th>
        <th>MCIDs</th>
        <th>Objects</th>
        <th>Obj Span</th>
        <th>Structure Hash</th>
        <th>Font Hash</th>
        <th>Content Hash</th>
        <th>Xref Hash</th>
        <th>Base Fonts</th>
        <th>Table Shapes</th>
        <th>Notes</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
</div>
</body>
</html>"""

    out_file.write_text(html, encoding="utf-8")
    return str(out_file)


def _hash_penalty_detail(
    value: str,
    allowed: list[str],
    penalty: int,
    bad_message: str,
    good_message: str,
    *,
    only_if_value: bool = False,
) -> tuple[int, str]:
    if not value:
        if only_if_value:
            return 0, "No hash value was extracted for this check."
        return penalty, bad_message
    if allowed and value in allowed:
        return 0, good_message
    return penalty, bad_message


def _range_penalty_detail(
    value: int,
    low: int,
    high: int,
    tolerance: int,
    cap: int,
    label: str,
) -> tuple[int, str]:
    if low <= value <= high:
        return 0, f"{label} is within the real-label range: got {value}, baseline {low}..{high}."
    if value < low:
        delta = low - value
        if delta <= tolerance:
            return 0, f"{label} is slightly below the real-label range but within tolerance: got {value}, baseline {low}..{high}."
        penalty = min(cap, delta * 2)
        return penalty, f"{label} is below the real-label range: got {value}, baseline {low}..{high}."
    delta = value - high
    if delta <= tolerance:
        return 0, f"{label} is slightly above the real-label range but within tolerance: got {value}, baseline {low}..{high}."
    penalty = min(cap, delta * 2)
    return penalty, f"{label} is above the real-label range: got {value}, baseline {low}..{high}."


def _image_xobject_penalty(fp: HiddenFingerprint, baseline: BaselineProfile) -> tuple[int, str]:
    real_has_xobjects = baseline.xobject_count_max > 0
    real_has_image_ops = baseline.image_draw_ops_max > 0

    if real_has_xobjects and fp.xobject_count == 0 and fp.image_draw_ops == 0 and fp.path_draw_ops > 0:
        return 32, "No image ops and no XObjects, while real PDFs usually keep reusable image/XObject content."

    if real_has_xobjects and fp.xobject_count == 0:
        return 22, "XObjects missing, while trusted real PDFs usually contain reusable XObjects."

    if real_has_image_ops and fp.image_draw_ops == 0 and fp.xobject_count == 0:
        return 18, "Image-like content is likely flattened inline; real PDFs usually expose image ops or XObjects."

    if fp.xobject_count > 0 and fp.image_draw_ops == 0:
        return 0, "XObjects exist; image content may be wrapped in reusable objects instead of direct image ops."

    return 0, "Image/XObject pattern matches or is acceptable for the real baseline."


def _grammar_summary(fp: HiddenFingerprint) -> str:
    return (
        f"Td/Tf/TJ={fp.td_tf_tj_patterns}; "
        f"Tj={fp.t_func_operator_count}; "
        f"TJ={fp.t_array_operator_count}; "
        f"hash={fp.content_grammar_sequence_sha256[:12] or '-'}"
    )


def _html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
