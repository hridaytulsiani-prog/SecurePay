"""Order check: a different report from the issue report.

Opened from the admin search for any order, with no ticket behind it. It is organised around the order itself:
a checklist (payment, delivery, label and verification, customer and merchant), the same status in every system side
by side (merchant dashboard, our system, the tracking source, the payment ledger), and one timeline of everything that
has happened to the order. It is built live each time it is opened.
"""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from adminpanel.order_report import (
    PAYMENT_NOT_PAID,
    PAYMENT_PAID_OUT,
    PAYMENT_RECEIVED,
    PAYMENT_REFUNDED,
    STALE_CHECK_HOURS,
    STALE_TRACKING_HOURS,
    _ago,
    _hours_since,
    _money,
    _parse_time,
    _pretty,
    build_order_report,
    pa_status_for,
)


def _check(group, label, level, detail=""):
    return {"group": group, "label": label, "level": level, "detail": detail}


def build_order_check(order, issues=()):
    """Build the order-check payload for one OrderInfo. `issues` = merchant issues already raised about the order."""
    report = build_order_report("overview", order, None)
    order_bits = report["order"]
    courier = report["courier"]
    payment = report["payment"]
    customer = report["customer"]
    view = report["merchant_view"]

    ours = (order_bits["delivery_status"] or "").upper()
    source_name = courier["source"] or "Tracking source"
    source_status = (courier["normalized_status"] or courier["current_status"] or "").upper()
    state = (order_bits["payment_state"] or "").upper()
    delivered = "DELIVERED" in ours or "DELIVERED" in source_status
    has_shipment = bool(order_bits["awb"] or ours)
    amount = Decimal(str(order_bits["amount"]))
    currency = order_bits["currency"]

    ledger = payment["ledger"]
    paid_rows = [row for row in ledger if "capture" in (row["entry_type"] or "").lower() or "payment" in (row["entry_type"] or "").lower()]
    refund_rows = [row for row in ledger if "refund" in (row["entry_type"] or "").lower()]
    paid_total = sum((Decimal(str(row["amount"])) for row in paid_rows), Decimal("0")) if paid_rows else None
    refund_total = sum((Decimal(str(row["amount"])) for row in refund_rows), Decimal("0")) if refund_rows else None

    checks = []

    # ---- payment
    if state in PAYMENT_NOT_PAID:
        checks.append(_check("Payment", "Payment received", "problem" if has_shipment else "warn", f"State is {_pretty(state)}: no money captured for this order."))
    elif state in PAYMENT_REFUNDED:
        checks.append(_check("Payment", "Payment received", "info", "The payment was refunded to the customer."))
    else:
        checks.append(_check("Payment", "Payment received", "ok", f"State: {_pretty(state)}."))

    if paid_total is None:
        checks.append(_check("Payment", "Amount matches the ledger", "info" if state in PAYMENT_NOT_PAID else "warn", "No capture entry in the ledger."))
    elif paid_total == amount:
        checks.append(_check("Payment", "Amount matches the ledger", "ok", f"{_money(paid_total, currency)} captured for a {_money(amount, currency)} order."))
    else:
        checks.append(_check("Payment", "Amount matches the ledger", "problem", f"{_money(paid_total, currency)} captured but the order amount is {_money(amount, currency)}."))

    if state in PAYMENT_PAID_OUT:
        checks.append(_check("Payment", "Payout to the merchant", "ok", f"Payment is {_pretty(state)}."))
    elif state in PAYMENT_RECEIVED:
        if delivered:
            hours = _hours_since(courier["delivered_at"]) if courier["delivered_at"] else None
            checks.append(_check("Payment", "Payout to the merchant", "problem", f"Delivered {_ago(hours)} but the payment is still '{_pretty(state)}', not paid out."))
        else:
            checks.append(_check("Payment", "Payout to the merchant", "info", "Not due yet: the order is not delivered."))
    if refund_total is not None:
        checks.append(_check("Payment", "Refund recorded", "info", f"{_money(refund_total, currency)} refunded."))

    # ---- delivery
    if not has_shipment:
        checks.append(_check("Delivery", "Shipment attached", "problem", "No AWB or courier is attached to this order."))
    else:
        checks.append(_check("Delivery", "Shipment attached", "ok", f"{courier['courier'] or 'Courier'} · AWB {order_bits['awb'] or '-'}."))
        if source_status:
            same = ours == source_status or source_status in ours or ours in source_status
            detail = f"Our record: {_pretty(ours)}. {source_name}: {_pretty(source_status)}."
            checks.append(_check("Delivery", "Our status matches the tracking source", "ok" if same else "problem", detail + ("" if same else " They are out of sync.")))
        else:
            checks.append(_check("Delivery", "Our status matches the tracking source", "warn", f"No answer from the tracking source is saved. Our record: {_pretty(ours)}."))

        check_hours = _hours_since(courier["last_checked_at"]) if courier["last_checked_at"] else None
        if courier["error"]:
            checks.append(_check("Delivery", "Tracking refresh", "problem", f"The tracking source returned an error: {courier['error']}"))
        elif check_hours is None:
            checks.append(_check("Delivery", "Tracking refresh", "warn", "This shipment has never been refreshed."))
        elif check_hours > STALE_CHECK_HOURS and not delivered:
            checks.append(_check("Delivery", "Tracking refresh", "warn", f"Last refreshed {_ago(check_hours)}."))
        else:
            checks.append(_check("Delivery", "Tracking refresh", "ok", f"Last refreshed {_ago(check_hours)}."))

        times = [t for t in (_parse_time(item.get("time")) for item in courier["events"]) if t]
        times += [t for t in (_parse_time(item.get("time")) for item in courier["own_history"]) if t]
        if not delivered:
            moved = _hours_since(max(times)) if times else None
            if moved is None:
                checks.append(_check("Delivery", "Shipment is moving", "warn", "No movement has been recorded."))
            elif moved > STALE_TRACKING_HOURS:
                checks.append(_check("Delivery", "Shipment is moving", "problem", f"No movement for {_ago(moved).replace(' ago', '')}."))
            else:
                checks.append(_check("Delivery", "Shipment is moving", "ok", f"Last movement {_ago(moved)}."))

    # ---- label and verification
    labels = courier["labels"]
    if labels:
        latest = labels[0]
        text = f"{latest['status']} {latest['verdict']}".lower()
        bad = any(word in text for word in ("flag", "suspicious", "reject", "fail", "tamper", "mismatch"))
        checks.append(_check("Label and verification", "Shipping label", "warn" if bad else "ok", f"Status {_pretty(latest['status'])}, verdict {_pretty(latest['verdict'])}."))
    else:
        checks.append(_check("Label and verification", "Shipping label", "warn" if has_shipment else "info", "No shipping label has been uploaded."))
    verification = courier["verification"]
    if verification:
        level = "ok" if verification["decision"] == "VERIFIED" else "warn"
        checks.append(_check("Label and verification", "Delivery verification", level, f"{_pretty(verification['decision'])} by {verification['decided_by'] or 'an admin'}."))
    else:
        checks.append(_check("Label and verification", "Delivery verification", "info", "No manual decision on record."))

    # ---- customer and merchant
    enquiries = customer["enquiries"]
    if enquiries:
        open_ones = [item for item in enquiries if item["receipt_status"] != "received"]
        checks.append(_check("Customer and merchant", "Customer enquiries", "warn" if open_ones else "ok", f"{len(enquiries)} raised; latest: {_pretty(enquiries[0]['receipt_status'])} ({enquiries[0]['enquiry_id']})."))
    else:
        checks.append(_check("Customer and merchant", "Customer enquiries", "ok", "None raised."))
    open_issues = [item for item in issues if item["status"] != "resolved"]
    if issues:
        checks.append(_check("Customer and merchant", "Merchant issues", "warn" if open_issues else "ok", f"{len(issues)} raised, {len(open_issues)} still open."))
    else:
        checks.append(_check("Customer and merchant", "Merchant issues", "ok", "None raised."))

    levels = [item["level"] for item in checks]
    problems = levels.count("problem")
    warns = levels.count("warn")
    if problems:
        verdict = {"level": "problem", "summary": f"{problems} problem{'s' if problems != 1 else ''} found"}
    elif warns:
        verdict = {"level": "warn", "summary": f"{warns} thing{'s' if warns != 1 else ''} to look at"}
    else:
        verdict = {"level": "ok", "summary": "Everything checks out"}

    # ---- the same status in every system, side by side
    comparison = [
        {"system": "Merchant dashboard", "delivery": view["delivery_status"], "payment": view["current_stage"], "note": f"Payment result: {_pretty(view['payment_result'])}"},
        {"system": "Our system", "delivery": order_bits["delivery_status"], "payment": order_bits["payment_state"], "note": f"Order: {_pretty(order_bits['order_status'])}"},
        {"system": source_name, "delivery": courier["current_status"] or courier["normalized_status"], "payment": "", "note": ""},
        {
            "system": "Payment ledger",
            "delivery": "",
            "payment": ("Captured " + _money(paid_total, currency)) if paid_total is not None else "No entries",
            "note": f"Refunded {_money(refund_total, currency)}" if refund_total is not None else "",
        },
    ]

    # ---- one timeline of everything that happened to the order, newest first
    timeline = [{"time": order_bits["order_date"], "kind": "Order", "title": "Order placed", "detail": f"{_money(amount, currency)} · {order_bits['merchant_order_id']}"}]
    for row in payment["transitions"]:
        timeline.append({"time": row["created_at"], "kind": "Payment", "title": f"Payment {_pretty(row['from_state'])} to {_pretty(row['to_state'])}", "detail": f"Source: {row['source']}"})
    for row in ledger:
        timeline.append({"time": row["created_at"], "kind": "Payment", "title": f"Ledger: {_pretty(row['entry_type'])} {_money(row['amount'], row['currency'])}", "detail": row["reference"] or ""})
    for row in courier["own_history"]:
        timeline.append({"time": row["time"], "kind": "Delivery", "title": f"Shipment {_pretty(row['status'])}", "detail": row["note"] or "Our record"})
    for row in courier["events"]:
        timeline.append({"time": row["time"], "kind": "Delivery", "title": row["status"] or "Scan", "detail": f"{source_name} · {row['location']}" if row["location"] else source_name})
    for row in labels:
        timeline.append({"time": row["uploaded_at"], "kind": "Label", "title": f"Label uploaded: {row['file_name']}", "detail": f"{_pretty(row['status'])} · {_pretty(row['verdict'])}"})
    if verification:
        timeline.append({"time": verification["decided_at"], "kind": "Verification", "title": f"Delivery {_pretty(verification['decision'])}", "detail": verification["decided_by"] or ""})
    for item in enquiries:
        timeline.append({"time": item["created_at"], "kind": "Customer", "title": f"Customer enquiry: {_pretty(item['receipt_status'])}", "detail": item["text"]})
    for item in issues:
        timeline.append({"time": item["created_at"], "kind": "Merchant", "title": f"Merchant raised {item['reference']}", "detail": item["issue_type_label"]})
    epoch = datetime.min.replace(tzinfo=dt_timezone.utc)
    timeline = [item for item in timeline if item["time"]]
    timeline.sort(key=lambda item: _parse_time(item["time"]) or epoch, reverse=True)

    return {
        "generated_at": report["generated_at"],
        "report": report,
        "pa_status": pa_status_for(order),
        "verdict": verdict,
        "order": order_bits,
        "merchant": {"name": order.merchant.merchant_name, "email": order.merchant.merchant_email},
        "customer": {"name": customer["name"], "phone": customer["phone"], "email": customer["email"], "address": customer["address"]},
        "checks": checks,
        "comparison": comparison,
        "timeline": timeline,
        "paid_total": str(paid_total) if paid_total is not None else None,
        "refund_total": str(refund_total) if refund_total is not None else None,
        "issues": list(issues),
    }
