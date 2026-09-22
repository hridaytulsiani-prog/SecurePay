"""Deterministic identification of a courier label.

Two fields a courier label always encodes unambiguously can be recovered without
any model:

  * **AWB / tracking number** — from an explicit ``AWB:`` / ``Tracking No:``
    label, or failing that the longest decoded barcode payload (the AWB is the
    label's primary scannable id).
  * **Delivery partner** — the carrier, matched against a fixed known-carrier
    list plus the ``Courier:`` label and the line printed just above the AWB.

Everything else a caller needs is already in the label's raw text (for lookup),
so nothing more is extracted here.
"""

from __future__ import annotations

import re
from typing import Optional


# Last-mile carriers seen on Indian courier labels. Matched case-insensitively
# as whole names so "Blue Dart Air" / "Xpressbees Surface" resolve even when the
# label prints no explicit "Courier:" tag.
_KNOWN_CARRIERS = [
    "Blue Dart", "Bluedart", "Delhivery", "Xpressbees", "Ekart", "DTDC",
    "Shadowfax", "Ecom Express", "Ekart Logistics", "India Post", "Gati",
    "Trackon", "Aramex", "FedEx", "Smartr", "Amazon Shipping", "Amazon",
    "Wow Express", "Professional Couriers",
]

# Service tier that often trails the carrier name ("Blue Dart Air").
_SERVICE_TIERS = ("Surface", "Air", "Express", "Standard", "Priority")
_GENERIC_COURIER_VALUES = {"document", "label", "shipment", "shipping", "courierlabel"}


def _first(patterns: list[str], text: str) -> Optional[str]:
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            val = m.group(1).strip(" :#-\t")
            if val:
                return val
    return None


def extract_awb(text: str, barcode_values: list[str]) -> Optional[str]:
    """The AWB from an explicit label, else the longest decoded barcode."""
    awb = _first(
        [
            r"AWB\s*(?:No\.?|Number|#)?\s*:?\s*([A-Za-z0-9][A-Za-z0-9-]{5,})",
            r"(?:Tracking|Waybill)\s*(?:No\.?|Number)?\s*#?\s*:?\s*([A-Za-z0-9][A-Za-z0-9-]{5,})",
        ],
        text,
    )
    if awb:
        return awb
    codes = [v for v in barcode_values if v and re.fullmatch(r"[A-Za-z0-9]+", v)]
    codes.sort(key=len, reverse=True)  # the AWB is the longest scannable id
    return codes[0] if codes else None


def extract_delivery_partner(text: str) -> Optional[str]:
    """The carrier, from a 'Courier:' label / line above AWB / known-carrier scan."""
    partner = _first([r"Courier\s*:?\s*([A-Za-z][A-Za-z .&']+)"], text)
    if partner and _normalise_name(partner) not in _GENERIC_COURIER_VALUES:
        return partner.strip()

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.match(r"^\s*AWB\b", ln, re.I) and i > 0:
            cand = lines[i - 1].strip()
            if any(c.lower() in cand.lower() for c in _KNOWN_CARRIERS):
                return cand

    for carrier in _KNOWN_CARRIERS:
        m = re.search(rf"{re.escape(carrier)}(?:\s+(?:{'|'.join(_SERVICE_TIERS)}))?",
                      text, re.I)
        if m:
            return m.group(0).strip()
    return None


def _normalise_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())
