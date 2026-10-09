"""Automatic order report for a merchant issue.

When a merchant raises an issue, `build_order_report` collects, for each order they ticked, everything the admin would
otherwise look up by hand, and checks it against what the merchant complained about:

  * what the merchant saw on their dashboard (payment result, current stage, delivery status, amount, courier)
  * what our system holds (order, payment state changes, ledger, shipment history, label checks, verification)
  * what the tracking source says (ShipSagar / TrackParcel / Delhivery Public: latest answer and scan events)
  * what the customer says (enquiries about the order, with notes)
  * automatic findings for that kind of issue, e.g. "tracking source says Delivered but our record says In transit"

The result is stored on MerchantIssue.report at the moment the ticket is raised, so the admin opens a finished report
instead of re-checking each system. Every list in it is JSON-serialisable.
"""

import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone

from payments.models.enquirydata import EnquiryData, EnquiryNote
from payments.models.payment_lifecycle import PaymentLedgerEntry, PaymentStateTransition
from tracking.models.delhivery_verification_decision import DelhiveryVerificationDecision
from tracking.models.pdf_validation import PdfValidationRecord
from tracking.models.tracking_snapshot import TrackingSnapshot

MAX_RAW_CHARS = 8000

# How the payment_state values group together.
PAYMENT_NOT_PAID = {"CREATED", "PAYMENT_PENDING", "AWAITING_PAYMENT", "PENDING", "FAILED", "PAYMENT_FAILED"}
PAYMENT_RECEIVED = {"PAYMENT_CAPTURED", "CAPTURED", "FUNDS_HELD", "HELD", "PAID"}
PAYMENT_PAID_OUT = {"SETTLED", "RELEASED", "PAYOUT_COMPLETED"}
PAYMENT_REFUNDED = {"REFUNDED", "REFUND", "REFUND_COMPLETED"}

STALE_TRACKING_HOURS = 72
STALE_CHECK_HOURS = 24


def _iso(value):
    return value.isoformat() if value else None


def _pretty(value):
    text = str(value or "-").replace("_", " ").lower().capitalize()
    return text.replace("Rto", "RTO").replace(" rto", " RTO")


def _money(value, currency="INR"):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    symbol = "₹" if (currency or "INR") == "INR" else f"{currency} "
    return f"{symbol}{amount:,.2f}"


def _parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)
        except ValueError:
            continue
    return None


def _hours_since(value):
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return (timezone.now() - parsed).total_seconds() / 3600


def _ago(hours):
    if hours is None:
        return "an unknown time ago"
    if hours < 1:
        return "less than an hour ago"
    if hours < 48:
        return f"{int(hours)} hours ago"
    return f"{int(hours // 24)} days ago"


def _first_text(lowered, words, skip=()):
    for key, value in lowered.items():
        if any(word in key for word in skip):
            continue
        if any(word in key for word in words) and isinstance(value, (str, int, float)):
            return str(value)
    return ""


def extract_events(raw, depth=0):
    """Best effort: find the list of scan / event dicts inside a tracking provider's raw response."""
    if depth > 5 or raw is None:
        return []
    if isinstance(raw, list):
        dicts = [item for item in raw if isinstance(item, dict)]
        if dicts and len(dicts) * 2 >= len(raw):
            keys = " ".join(str(key).lower() for item in dicts[:3] for key in item.keys())
            if any(word in keys for word in ("status", "activity", "scan", "remark", "event")):
                events = []
                for item in dicts:
                    lowered = {str(key).lower(): value for key, value in item.items()}
                    events.append(
                        {
                            "time": _first_text(lowered, ("date", "time")),
                            "status": _first_text(lowered, ("activity", "status", "remark", "event", "scan"), skip=("date", "time", "location", "city")),
                            "location": _first_text(lowered, ("location", "city", "place", "hub")),
                        }
                    )
                return events[:40]
        for item in raw:
            found = extract_events(item, depth + 1)
            if found:
                return found
    elif isinstance(raw, dict):
        for value in raw.values():
            found = extract_events(value, depth + 1)
            if found:
                return found
    return []


def _finding(level, title, detail=""):
    return {"level": level, "title": title, "detail": detail}


def _analyse(issue_type, ctx):
    """Automatic findings for the kind of issue the merchant raised. Levels: problem, warn, ok, info.

    The special type "overview" (a report opened from the admin search, with no ticket behind it) runs the checks for
    every kind of issue and keeps what matters, so the admin sees any status mismatch, delayed or mismatched payment,
    stuck tracking or customer enquiry for the order in one list.
    """
    if issue_type == "overview":
        merged = []
        seen_titles = set()
        for kind in ("status_mismatch", "payment_delayed", "payment_missing", "tracking_stuck", "rto_return", "customer_dispute"):
            for item in _analyse(kind, ctx):
                if item["title"] in seen_titles:
                    continue
                # Only results that say something (problem, warning or "checked and fine"); drop the neutral notes.
                if item["level"] == "info":
                    continue
                seen_titles.add(item["title"])
                merged.append(item)
        if not merged:
            merged.append(_finding("info", "Order at a glance", f"Payment {_pretty(ctx['payment_state'])}, delivery {_pretty(ctx['our_delivery'])}."))
        return merged

    findings = []
    ours = (ctx["our_delivery"] or "").upper()
    source_name = ctx["source_name"] or "the tracking source"
    source_status = (ctx["source_delivery"] or "").upper()
    delivered = "DELIVERED" in ours or "DELIVERED" in source_status
    payment_state = (ctx["payment_state"] or "").upper()
    seen = ctx["merchant_view"]

    if not ctx["has_shipment"]:
        findings.append(_finding("problem", "No shipment is attached to this order", "The merchant sees no AWB or courier for it, so there is nothing to track yet."))

    if issue_type == "status_mismatch":
        if ctx["has_snapshot"] and source_status:
            if ours and source_status != ours and not (source_status in ours or ours in source_status):
                findings.append(
                    _finding(
                        "problem",
                        f"{source_name} says {_pretty(source_status)}, but our record says {_pretty(ours)}",
                        f"The merchant's dashboard shows delivery status '{_pretty(seen['delivery_status'])}'. The two are out of sync; the tracking refresh has probably not updated our record.",
                    )
                )
            else:
                findings.append(
                    _finding("ok", f"Our record and {source_name} agree: {_pretty(source_status)}", f"The merchant's dashboard shows '{_pretty(seen['delivery_status'])}'. No mismatch was found in our system.")
                )
        elif ctx["has_shipment"]:
            findings.append(_finding("warn", "No saved answer from the tracking source", "We cannot compare our status with the courier's. Our record says " + _pretty(ours) + "."))
        if ctx["check_hours"] is not None and ctx["check_hours"] > STALE_CHECK_HOURS and not delivered:
            findings.append(_finding("warn", f"Tracking was last checked {_ago(ctx['check_hours'])}", "A refresh may be overdue."))

    elif issue_type == "payment_delayed":
        if not delivered:
            findings.append(_finding("info", f"Order is not delivered yet (our record: {_pretty(ours)})", "Payment release is not due until delivery is confirmed."))
        elif payment_state in PAYMENT_PAID_OUT:
            findings.append(_finding("ok", f"Payment is already {_pretty(payment_state)}", ctx["ledger_summary"]))
        elif payment_state in PAYMENT_REFUNDED:
            findings.append(_finding("info", "Payment was refunded", "The money went back to the customer, so no payout is due."))
        elif payment_state in PAYMENT_NOT_PAID:
            findings.append(_finding("problem", f"Delivered, but the payment was never received (state: {_pretty(payment_state)})", "There is no captured payment to release."))
        else:
            when = ctx["delivered_hours"]
            findings.append(
                _finding(
                    "problem",
                    f"Delivered {_ago(when)} but payment is still '{_pretty(payment_state)}'",
                    "The money is held and has not been settled to the merchant. " + ctx["ledger_summary"],
                )
            )
        if ctx["verification"] and ctx["verification"]["decision"] != "VERIFIED":
            findings.append(_finding("warn", "Delivery verification is not 'Verified'", f"Decision: {_pretty(ctx['verification']['decision'])}. Payment may be waiting on this."))
        if ctx["label_flagged"]:
            findings.append(_finding("warn", "The shipping label check has a concern", ctx["label_flagged"]))

    elif issue_type == "payment_missing":
        if payment_state in PAYMENT_NOT_PAID:
            findings.append(_finding("problem", f"No payment recorded for this order (state: {_pretty(payment_state)})", "The customer's payment was not captured."))
        elif payment_state in PAYMENT_REFUNDED:
            findings.append(_finding("info", "Payment was refunded", ctx["ledger_summary"]))
        else:
            amount = ctx["amount"]
            captured = ctx["captured_total"]
            if captured is None:
                findings.append(_finding("warn", "No ledger entries for this order", f"Order amount {_money(amount, ctx['currency'])}; payment state {_pretty(payment_state)}."))
            elif captured != amount:
                findings.append(_finding("problem", f"Captured {_money(captured, ctx['currency'])} but the order amount is {_money(amount, ctx['currency'])}", "The ledger and the order amount do not match."))
            else:
                findings.append(_finding("ok", f"Ledger matches the order amount ({_money(amount, ctx['currency'])})", f"State: {_pretty(payment_state)}. " + ctx["ledger_summary"]))
            if payment_state in PAYMENT_RECEIVED and not delivered:
                findings.append(_finding("info", "Money is held until delivery", "It is not paid out to the merchant before the order is delivered."))

    elif issue_type == "tracking_stuck":
        if ctx["source_error"]:
            findings.append(_finding("problem", "The tracking source returned an error", ctx["source_error"]))
        if delivered:
            findings.append(_finding("ok", "The order is delivered, so tracking is complete", f"Status: {_pretty(source_status or ours)}."))
        else:
            hours = ctx["movement_hours"]
            if hours is None:
                findings.append(_finding("warn", "No tracking movement has been recorded", f"Current status: {_pretty(source_status or ours)}."))
            elif hours > STALE_TRACKING_HOURS:
                findings.append(_finding("problem", f"No movement for {_ago(hours).replace(' ago', '')}", f"Last scan: {ctx['last_movement_text']}. Current status: {_pretty(source_status or ours)}."))
            else:
                findings.append(_finding("ok", f"Last movement {_ago(hours)}", f"{ctx['last_movement_text']}."))
            if ctx["check_hours"] is not None and ctx["check_hours"] > STALE_CHECK_HOURS:
                findings.append(_finding("warn", f"Tracking was last checked {_ago(ctx['check_hours'])}", "The refresh may not be running for this shipment."))

    elif issue_type == "rto_return":
        ours_rto = "RTO" in ours or "RETURN" in ours
        source_rto = "RTO" in source_status or "RETURN" in source_status
        if source_rto and not ours_rto:
            findings.append(_finding("problem", f"{source_name} says {_pretty(source_status)}, but our record says {_pretty(ours)}", "The return is not reflected in our system."))
        elif ours_rto:
            findings.append(_finding("ok", f"Our record shows the return: {_pretty(ours)}", ""))
            if payment_state not in PAYMENT_REFUNDED:
                findings.append(_finding("warn", f"Returned, but payment state is '{_pretty(payment_state)}' (no refund recorded)", ctx["ledger_summary"]))
        else:
            findings.append(_finding("info", "Neither our record nor the tracking source shows a return", f"Our record: {_pretty(ours)}. {source_name}: {_pretty(source_status)}."))

    elif issue_type == "customer_dispute":
        if ctx["delivered_at"]:
            findings.append(_finding("info", f"{source_name} marked it Delivered on {ctx['delivered_at_text']}", ""))
        elif delivered:
            findings.append(_finding("info", "The order is marked Delivered", f"Our record: {_pretty(ours)}."))
        else:
            findings.append(_finding("info", f"The order is not marked delivered ({_pretty(source_status or ours)})", ""))
        if ctx["enquiries"]:
            enquiry = ctx["enquiries"][0]
            detail = f"Enquiry {enquiry['enquiry_id']}: {_pretty(enquiry['receipt_status'])}."
            if enquiry.get("otp_shared") is not None:
                detail += f" OTP shared: {'Yes' if enquiry['otp_shared'] else 'No'}."
            if enquiry.get("agent_contacted") is not None:
                detail += f" Courier agent contacted: {'Yes' if enquiry['agent_contacted'] else 'No'}."
            findings.append(_finding("warn" if enquiry["receipt_status"] != "received" else "ok", "The customer raised an enquiry about this order", detail))
        else:
            findings.append(_finding("info", "The customer has not raised an enquiry about this order", ""))
        if ctx["verification"]:
            findings.append(_finding("ok" if ctx["verification"]["decision"] == "VERIFIED" else "warn", f"Delivery verification: {_pretty(ctx['verification']['decision'])}", ""))
        else:
            findings.append(_finding("info", "No manual delivery verification on record", ""))

    else:  # other
        findings.append(_finding("info", "Order at a glance", f"Payment {_pretty(payment_state)}, delivery {_pretty(ours)}, tracking source {_pretty(source_status)}."))

    return findings


def build_order_report(issue_type, order, snapshot_row=None):
    """Build the report dict for one OrderInfo (see the module docstring)."""
    shipment = order.shipment_id
    customer = order.customer_info
    keys = [value for value in (order.merchant_order_id, order.pa_order_id) if value]
    awb = getattr(shipment, "awb", "") or ""

    snapshot = TrackingSnapshot.objects.filter(shipment=shipment).first() if shipment else None
    raw_text = ""
    events = []
    if snapshot and snapshot.raw_response is not None:
        events = extract_events(snapshot.raw_response)
        try:
            raw_text = json.dumps(snapshot.raw_response, indent=2, default=str)[:MAX_RAW_CHARS]
        except (TypeError, ValueError):
            raw_text = str(snapshot.raw_response)[:MAX_RAW_CHARS]

    own_history = []
    if shipment is not None and isinstance(shipment.history, list):
        own_history = [
            {"time": item.get("ts"), "status": item.get("status"), "note": item.get("note", "")}
            for item in shipment.history[-20:]
            if isinstance(item, dict)
        ]

    label_filter = Q(order_id__in=keys)
    if awb:
        label_filter |= Q(awb=awb)
    label_rows = list(PdfValidationRecord.objects.filter(label_filter, merchant_id=order.merchant_id).order_by("-uploaded_at")[:5])
    labels = [
        {
            "file_name": row.file_name,
            "status": row.status,
            "verdict": row.verdict or "",
            "courier": row.courier_partner or row.delivery_partner or "",
            "uploaded_at": _iso(row.uploaded_at),
        }
        for row in label_rows
    ]
    label_flagged = ""
    if label_rows:
        latest = label_rows[0]
        text = f"{latest.status} {latest.verdict or ''}".lower()
        if any(word in text for word in ("flag", "suspicious", "reject", "fail", "tamper", "mismatch")):
            label_flagged = f"Latest label: status {_pretty(latest.status)}, verdict {_pretty(latest.verdict)}."

    verification = None
    if awb:
        found = DelhiveryVerificationDecision.objects.filter(awb=awb).select_related("decided_by").first()
        if found:
            verification = {
                "decision": found.decision,
                "decided_by": found.decided_by.username if found.decided_by else "",
                "decided_at": _iso(found.decided_at),
            }

    enquiries = []
    for enquiry in EnquiryData.objects.filter(order_id__in=keys).order_by("-created_at")[:6]:
        enquiries.append(
            {
                "enquiry_id": enquiry.enquiry_id,
                "receipt_status": enquiry.receipt_status,
                "status": enquiry.status,
                "text": enquiry.enquiry_text,
                "someone_else_received": enquiry.someone_else_received,
                "agent_contacted": enquiry.agent_contacted,
                "otp_shared": enquiry.otp_shared,
                "unboxing_evidence": enquiry.unboxing_evidence,
                "resolution_status": enquiry.resolution_status or "",
                "resolution_reason": enquiry.resolution_reason or "",
                "created_at": _iso(enquiry.created_at),
                "notes": [
                    {"note": note.note, "by": note.created_by.username if note.created_by else "", "created_at": _iso(note.created_at)}
                    for note in EnquiryNote.objects.filter(enquiry=enquiry).select_related("created_by").order_by("created_at")
                ],
            }
        )

    transitions = [
        {"from_state": row.from_state, "to_state": row.to_state, "source": row.source, "created_at": _iso(row.created_at)}
        for row in PaymentStateTransition.objects.filter(order=order).order_by("created_at")[:20]
    ]
    ledger_rows = list(PaymentLedgerEntry.objects.filter(order=order).order_by("created_at")[:20])
    ledger = [
        {"entry_type": row.entry_type, "amount": str(row.amount), "currency": row.currency, "reference": row.reference, "created_at": _iso(row.created_at)}
        for row in ledger_rows
    ]
    captured_entries = [row for row in ledger_rows if "capture" in (row.entry_type or "").lower() or "payment" in (row.entry_type or "").lower()]
    captured_total = sum((row.amount for row in captured_entries), Decimal("0")) if captured_entries else None
    ledger_summary = "Ledger: " + "; ".join(f"{_pretty(row.entry_type)} {_money(row.amount, row.currency)}" for row in ledger_rows[:6]) + "." if ledger_rows else "No ledger entries."

    # What the merchant saw on their dashboard (same fields the Dashboard table shows).
    merchant_view = {
        "payment_result": order.order_status,
        "current_stage": order.payment_state,
        "delivery_status": getattr(shipment, "status", "") or "",
        "amount": str(order.order_amount),
        "currency": order.order_currency,
        "courier": getattr(shipment, "courier", "") or "",
        "awb": awb,
        "customer_name": customer.customer_name,
        "customer_phone": customer.customer_phone,
        "order_date": _iso(order.order_date),
        "status_when_raised": {
            "delivery_status": (snapshot_row or {}).get("delivery_status", ""),
            "payment_state": (snapshot_row or {}).get("payment_state", ""),
        },
    }

    # Movement = the newest scan time we can find.
    movement_times = [t for t in (_parse_time(item.get("time")) for item in events) if t] + [t for t in (_parse_time(item.get("time")) for item in own_history) if t]
    last_movement = max(movement_times) if movement_times else None
    last_movement_text = "no scan recorded"
    if events:
        newest = max(events, key=lambda item: _parse_time(item.get("time")) or datetime.min.replace(tzinfo=dt_timezone.utc))
        last_movement_text = f"{newest.get('status') or 'scan'} at {newest.get('location') or 'unknown location'} ({newest.get('time')})"
    elif own_history:
        last_movement_text = f"{_pretty(own_history[-1].get('status'))} ({own_history[-1].get('time')})"

    ctx = {
        "has_shipment": shipment is not None,
        "has_snapshot": snapshot is not None and bool(snapshot.current_status or snapshot.normalized_status),
        "our_delivery": getattr(shipment, "status", "") or "",
        "source_name": snapshot.get_source_display() if snapshot else "",
        "source_delivery": (snapshot.normalized_status or snapshot.current_status) if snapshot else "",
        "source_error": snapshot.error if snapshot and snapshot.error else "",
        "payment_state": order.payment_state,
        "amount": order.order_amount,
        "currency": order.order_currency,
        "captured_total": captured_total,
        "ledger_summary": ledger_summary,
        "merchant_view": merchant_view,
        "delivered_at": snapshot.delivered_at if snapshot else None,
        "delivered_at_text": snapshot.delivered_at.strftime("%d %b %Y, %I:%M %p") if snapshot and snapshot.delivered_at else "",
        "delivered_hours": _hours_since(snapshot.delivered_at) if snapshot and snapshot.delivered_at else _hours_since(last_movement),
        "check_hours": _hours_since(snapshot.last_checked_at) if snapshot and snapshot.last_checked_at else None,
        "movement_hours": _hours_since(last_movement),
        "last_movement_text": last_movement_text,
        "verification": verification,
        "label_flagged": label_flagged,
        "enquiries": enquiries,
    }
    findings = _analyse(issue_type, ctx)

    order_bits = {
        "merchant_order_id": order.merchant_order_id,
        "pa_order_id": order.pa_order_id or "",
        "pa_payment_id": order.pa_payment_id or "",
        "amount": str(order.order_amount),
        "currency": order.order_currency,
        "order_status": order.order_status,
        "payment_state": order.payment_state,
        "payment_provider": order.payment_provider or "",
        "order_date": _iso(order.order_date),
        "awb": awb,
        "courier": getattr(shipment, "courier", "") or "",
        "delivery_status": getattr(shipment, "status", "") or "",
    }

    levels = [item["level"] for item in findings]
    if "problem" in levels:
        verdict = {"level": "problem", "summary": next(item["title"] for item in findings if item["level"] == "problem")}
    elif "warn" in levels:
        verdict = {"level": "warn", "summary": next(item["title"] for item in findings if item["level"] == "warn")}
    else:
        verdict = {"level": "ok", "summary": "No problem found in our system for what the merchant reported."}

    return {
        "generated_at": _iso(timezone.now()),
        "issue_type": issue_type,
        "order": order_bits,
        "merchant_view": merchant_view,
        "findings": findings,
        "verdict": verdict,
        "courier": {
            "courier": order_bits["courier"],
            "awb": awb,
            "status": order_bits["delivery_status"],
            "source": snapshot.get_source_display() if snapshot else "",
            "current_status": snapshot.current_status if snapshot else "",
            "normalized_status": snapshot.normalized_status if snapshot else "",
            "expected_delivery_date": _iso(snapshot.expected_delivery_date) if snapshot else None,
            "delivered_at": _iso(snapshot.delivered_at) if snapshot else None,
            "last_checked_at": _iso(snapshot.last_checked_at) if snapshot else None,
            "error": snapshot.error if snapshot else "",
            "events": events,
            "own_history": own_history,
            "raw_response": raw_text,
            "labels": labels,
            "verification": verification,
        },
        "customer": {
            "name": customer.customer_name,
            "email": customer.customer_email,
            "phone": customer.customer_phone,
            "address": customer.customer_address,
            "enquiries": enquiries,
        },
        "payment": {"transitions": transitions, "ledger": ledger},
    }


def pa_status_for(order):
    """What the payment aggregator (PA) holds for this order: Held, Released or Refunded.

    A PA record only exists once the customer's money has actually reached the aggregator, so an order that was
    never paid (still created, pending or failed) has no PA status: an empty string, shown as "NA".
    """
    state = (order.payment_state or "").upper()
    if state in PAYMENT_NOT_PAID:
        return ""
    from payments.models.pa_control import PAProtectedOrder

    protected = PAProtectedOrder.objects.filter(order=order).first()
    if protected is not None:
        if protected.pa_command_state == PAProtectedOrder.COMMAND_COMPLETED:
            if protected.decision_state == PAProtectedOrder.DECISION_RELEASE:
                return "Released"
            if protected.decision_state == PAProtectedOrder.DECISION_REFUND:
                return "Refunded"
        if protected.payment_state == PAProtectedOrder.PAYMENT_HELD:
            return "Held"
    if state in PAYMENT_REFUNDED:
        return "Refunded"
    if state in PAYMENT_PAID_OUT:
        return "Released"
    if state in PAYMENT_RECEIVED:
        return "Held"
    return _pretty(state)
