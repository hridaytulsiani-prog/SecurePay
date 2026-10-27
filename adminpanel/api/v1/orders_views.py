"""Cross-merchant read views for the admin panel: every order and every
enquiry in the system, regardless of which merchant they belong to.

The merchant-facing views (payments.api.v1.generate_order.ShipmentListView)
scope everything to the logged-in merchant's own orders; these views
deliberately don't, since the whole point of the admin panel is to see
across merchants.
"""

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from math import ceil
import re

from rest_framework.views import Response
from rest_framework import status

from adminpanel.permissions import AdminAPIView
from adminpanel.models import CustomerIssue, MerchantIssue, MerchantRiskProfile
from adminpanel.merchant_risk import FLAG_THRESHOLD, is_flagged
from adminpanel.customer_performance import CUSTOMER_FLAG_THRESHOLD, customer_is_flagged, refresh_all_customer_scores
from payments.models.merchantinfo import MerchantInfo
from payments.models.orderinfo import OrderInfo
from payments.models.enquirydata import EnquiryData, EnquiryNote
from tracking.models.pdf_validation import PdfValidationRecord
from tracking.models.delhivery_verification_decision import DelhiveryVerificationDecision
from adminpanel.api.v1.payment_ids import numeric_payment_id


def attach_shipment_label_status(rows, request=None):
    def attach_without_pdf_record(row):
        has_linked_shipment = bool(row.get("shipment_id__awb"))
        row["shipment_label_status"] = "shipment_linked" if has_linked_shipment else "missing"
        row["shipment_label_verdict"] = None
        row["shipment_label_file_name"] = None
        row["shipment_label_pdf_url"] = None
        row["shipment_label_reason"] = None
        row["shipment_label_report"] = None

    merchant_ids = {row.get("merchant_id") for row in rows if row.get("merchant_id")}
    order_ids = {
        value
        for row in rows
        for value in (row.get("merchant_order_id"), row.get("pa_order_id"))
        if value
    }
    awbs = {row.get("shipment_id__awb") for row in rows if row.get("shipment_id__awb")}

    if not merchant_ids or (not order_ids and not awbs):
        for row in rows:
            attach_without_pdf_record(row)
        return

    records = PdfValidationRecord.objects.filter(merchant_id__in=merchant_ids).filter(
        Q(order_id__in=order_ids) | Q(awb__in=awbs)
    ).order_by("-uploaded_at")

    by_order_key = {}
    by_awb_key = {}
    for record in records:
        if record.order_id:
            by_order_key.setdefault((record.merchant_id, record.order_id), record)
        if record.awb:
            by_awb_key.setdefault((record.merchant_id, record.awb), record)

    for row in rows:
        merchant_id = row.get("merchant_id")
        record = (
            by_order_key.get((merchant_id, row.get("merchant_order_id")))
            or by_order_key.get((merchant_id, row.get("pa_order_id")))
            or by_awb_key.get((merchant_id, row.get("shipment_id__awb")))
        )
        if not record:
            attach_without_pdf_record(row)
            continue

        row["shipment_label_status"] = record.status
        row["shipment_label_verdict"] = record.verdict
        row["shipment_label_file_name"] = record.file_name
        stored_pdf = (record.details or {}).get("stored_pdf")
        row["shipment_label_pdf_url"] = (
            request.build_absolute_uri(f"{settings.MEDIA_URL}{stored_pdf}")
            if request and stored_pdf
            else None
        )
        row["shipment_label_reason"] = admin_visible_label_reason(record)
        row["shipment_label_report"] = admin_label_report(record) if record.status == "not_approved" else None


def admin_visible_label_reason(record):
    if record.status == "processing":
        return "Checking label"
    if record.status == "approved":
        return None

    details = record.details or {}
    error = str(details.get("error") or "").strip()
    if error:
        return error

    forgery_result = details.get("forgery_result") or {}
    failed_checks = [
        str(item.get("check") or "")
        for item in (forgery_result.get("score_breakdown") or [])
        if item.get("gate") and item.get("passed") is False
    ]
    courier_name = record.delivery_partner or record.courier_partner or "Courier"
    reason_map = (
        ("Approved branding assets", f"{courier_name} logo/design does not match verified label format"),
        ("Barcode placement", f"Barcode placement does not match {courier_name} label format"),
        ("Text positioning", f"Text layout does not match {courier_name} label format"),
        ("Graphics state", f"PDF drawing format does not match {courier_name} label"),
        ("Xobjects", f"Label design elements do not match {courier_name} format"),
        ("Image ops", f"Label image elements do not match {courier_name} format"),
        ("TJ and Tj value", f"Text format does not match {courier_name} label format"),
        ("TD/Tf/Tj value", f"Text layout does not match {courier_name} label format"),
        ("Object serialization", f"PDF structure does not match {courier_name} label format"),
    )
    for check in failed_checks:
        for key, message in reason_map:
            if key in check:
                return message
    if failed_checks:
        return "Label format could not be verified"
    return "Label could not be verified"


CHECK_EXPLANATIONS = {
    "Header": "Checks that the PDF starts like a genuine courier-generated file. A different header or offset can indicate the file was recreated or edited by another tool.",
    "EOF": "Checks the end-of-file structure. Real courier labels usually have a consistent EOF pattern; extra or missing markers can show manual editing.",
    "XObjects": "Checks reusable PDF design objects such as logo/image blocks. Genuine labels normally reuse these objects in a predictable way.",
    "Image Ops": "Checks image drawing operations used for logos, barcodes, and label artwork. Missing or extra image operations can reveal a fake layout.",
    "Object serialization": "Checks how PDF objects are written internally. Different serialization often means the PDF was produced by a non-courier generator.",
    "Page compression": "Checks the compression profile of page streams. Courier systems tend to emit a stable compression style.",
    "f*/b*": "Checks courier-specific drawing commands. These commands are part of the label's internal drawing fingerprint.",
    "/Trans entry in page directory": "Checks courier-specific page dictionary metadata used in genuine label output.",
    "/Rotate entry": "Checks page rotation metadata. Unexpected rotation entries can indicate regenerated PDF structure.",
    "Font redundant /Name entry": "Checks font dictionary structure. Genuine labels often keep consistent font metadata.",
    "Barcode text matches": "Checks whether decoded barcode values also appear in the visible/extracted label text.",
    "Barcode values": "Checks barcode count and barcode formats against genuine label samples.",
    "Barcode contributions": "Combines barcode-related mismatch signals into the fraud score.",
    "Approved branding assets": "Checks that courier logos and brand assets match the verified courier label format.",
    "Barcode placement": "Checks that barcode placement matches the expected courier label template.",
    "Text positioning": "Checks whether important text appears in expected label positions.",
    "Graphics state": "Checks low-level PDF drawing state that genuine labels usually keep consistent.",
    "TJ and Tj value": "Checks text rendering commands used by the PDF.",
    "TD/Tf/Tj value": "Checks text placement and font commands used by the PDF.",
}


def _split_expected_actual(detail):
    text = str(detail or "")
    patterns = (
        r"got (?P<actual>.*?), real (?P<expected>.*?)(?:\.|$)",
        r"got (?P<actual>.*?); real (?P<expected>.*?)(?:\.|$)",
        r"got (?P<actual>.*?), expected (?P<expected>.*?)(?:\.|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group("expected").strip(), match.group("actual").strip()
    if "matches real sample" in text:
        return "Matches real courier sample", "Matches uploaded PDF"
    return "Real courier baseline", text or "-"


def _admin_report_rows_from_breakdown(forgery_result):
    rows = []
    for item in (forgery_result or {}).get("score_breakdown") or []:
        check = str(item.get("check") or "Label property")
        detail = str(item.get("detail") or "")
        expected, actual = _split_expected_actual(detail)
        rows.append(
            {
                "property": check,
                "why": CHECK_EXPLANATIONS.get(check, "Compares the uploaded PDF against the courier's real label structure."),
                "expected": expected,
                "actual": actual,
                "result": "Passed" if item.get("passed") else "Failed",
                "is_gate": bool(item.get("gate")),
                "detail": detail,
            }
        )
    return rows


def _admin_report_rows_from_checks(result):
    rows = []
    for check in (result or {}).get("checks") or []:
        findings = check.get("findings") or []
        evidence = "; ".join(str(finding.get("evidence") or finding.get("message") or "") for finding in findings).strip("; ")
        rows.append(
            {
                "property": check.get("title") or check.get("key") or "Label check",
                "why": check.get("summary") or "Checks one fraud signal from the uploaded courier label.",
                "expected": "Check should pass against a genuine label",
                "actual": evidence or check.get("status") or "-",
                "result": "Passed" if check.get("status") == "PASS" else "Failed" if check.get("status") == "FAIL" else "Not checked",
                "is_gate": bool(any(finding.get("critical") for finding in findings)),
                "detail": evidence or check.get("summary") or "",
            }
        )
    return rows


def admin_label_report(record):
    details = record.details or {}
    result = details.get("result") or {}
    forgery_result = details.get("forgery_result") or {}
    risk_analysis = details.get("risk_analysis") or {}
    rows = _admin_report_rows_from_breakdown(forgery_result)
    if not rows:
        rows = _admin_report_rows_from_checks(result)

    failed_rows = [row for row in rows if row.get("result") == "Failed"]
    decisive_rows = [row for row in failed_rows if row.get("is_gate")]
    return {
        "record_id": record.id,
        "file_name": record.file_name,
        "courier": record.delivery_partner or record.courier_partner or "",
        "awb": record.awb,
        "order_id": record.order_id,
        "verdict": record.verdict,
        "risk_verdict": record.risk_verdict,
        "score": record.score,
        "risk_score": record.risk_score,
        "failure_summary": decisive_rows[0]["detail"] if decisive_rows else failed_rows[0]["detail"] if failed_rows else admin_visible_label_reason(record),
        "rows": rows,
        "risk_reasons": risk_analysis.get("reasons") or [],
        "barcodes": result.get("barcodes") or [],
    }


def attach_verification_decisions(rows):
    awbs = {row.get("shipment_id__awb") for row in rows if row.get("shipment_id__awb")}
    if not awbs:
        for row in rows:
            row["verification_status"] = None
            row["verification_decided_at"] = None
        return

    latest_by_awb = {}
    for decision in DelhiveryVerificationDecision.objects.filter(awb__in=awbs).select_related("decided_by"):
        latest_by_awb.setdefault(decision.awb, decision)

    for row in rows:
        decision = latest_by_awb.get(row.get("shipment_id__awb"))
        row["verification_status"] = decision.decision if decision else None
        row["verification_decided_at"] = decision.decided_at.isoformat() if decision else None


def attach_issue_status(rows):
    """Attach whether each order has open issues or only resolved issues."""
    merchant_ids = {row.get("merchant_id") for row in rows if row.get("merchant_id")}
    row_keys = {
        str(value).strip().lower()
        for row in rows
        for value in (row.get("merchant_order_id"), row.get("pa_order_id"), row.get("shipment_id__awb"))
        if value
    }
    merchant_issues = {}
    for issue in MerchantIssue.objects.filter(merchant_id__in=merchant_ids).values("id", "merchant_id", "orders", "status"):
        issue_info = {
            "id": issue["id"],
            "reference": f"ISS-{issue['id']:05d}",
            "status": issue["status"],
        }
        for linked_order in issue.get("orders") or []:
            for value in (
                linked_order.get("merchant_order_id"),
                linked_order.get("order_id"),
                linked_order.get("pa_order_id"),
                linked_order.get("awb"),
            ):
                if value:
                    key = (issue["merchant_id"], str(value).strip().lower())
                    merchant_issues.setdefault(key, []).append(issue_info)

    customer_issues = {}
    for issue in CustomerIssue.objects.all().values("id", "order_ref", "order", "status"):
        issue_info = {
            "id": issue["id"],
            "reference": f"CIS-{issue['id']:05d}",
            "status": issue["status"],
        }
        issue_keys = {str(issue.get("order_ref") or "").strip().lower()}
        linked_order = issue.get("order") or {}
        issue_keys.update(
            str(value).strip().lower()
            for value in (
                linked_order.get("merchant_order_id"),
                linked_order.get("order_id"),
                linked_order.get("pa_order_id"),
                linked_order.get("awb"),
            )
            if value
        )
        for key in issue_keys & row_keys:
            customer_issues.setdefault(key, []).append(issue_info)

    for row in rows:
        keys = {
            str(value).strip().lower()
            for value in (row.get("merchant_order_id"), row.get("pa_order_id"), row.get("shipment_id__awb"))
            if value
        }
        linked_merchant_issues = [
            issue
            for key in keys
            for issue in merchant_issues.get((row.get("merchant_id"), key), [])
        ]
        linked_customer_issues = [
            issue
            for key in keys
            for issue in customer_issues.get(key, [])
        ]
        linked_merchant_issues = list({issue["id"]: issue for issue in linked_merchant_issues}.values())
        linked_customer_issues = list({issue["id"]: issue for issue in linked_customer_issues}.values())
        preferred = lambda issues: next((issue for issue in issues if issue["status"] != "resolved"), issues[0] if issues else None)
        row["merchant_issue"] = preferred(linked_merchant_issues)
        row["customer_issue"] = preferred(linked_customer_issues)
        statuses = [issue["status"] for issue in linked_merchant_issues + linked_customer_issues]
        row["issue_status"] = (
            "open" if any(status == "new" for status in statuses)
            else "in_progress" if any(status == "in_progress" for status in statuses)
            else "resolved" if statuses
            else None
        )


def _normalized_order_state(value):
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _order_state_includes(value, terms):
    state = _normalized_order_state(value)
    return any(term in state for term in terms)


def order_row_tone(row):
    """Return the same four-state colour used by the admin Orders table."""
    failure_terms = (
        "failed", "rejected", "not_approved", "cancelled", "canceled",
        "lost", "damaged", "returned", "rto", "refund",
    )
    has_failure = any(
        _order_state_includes(row.get(field), failure_terms)
        for field in ("payment_state", "order_status", "shipment_id__status", "shipment_label_status")
    ) or _normalized_order_state(row.get("verification_status")) == "not_verified"

    if row.get("issue_status") == "open" or has_failure:
        return "failed"
    if row.get("issue_status") == "in_progress":
        return "pending"
    if row.get("issue_status") == "resolved":
        return "resolved"

    payment_complete = _order_state_includes(
        row.get("payment_state") or row.get("order_status"),
        ("paid", "success", "captured", "settled", "completed", "released"),
    )
    delivery_complete = _order_state_includes(row.get("shipment_id__status"), ("delivered", "completed"))
    label_approved = _normalized_order_state(row.get("shipment_label_status")) in ("approved", "shipment_linked")
    courier_verified = _normalized_order_state(row.get("verification_status")) == "verified"
    return "success" if payment_complete and delivery_complete and label_approved and courier_verified else "pending"


class AdminOrderListView(AdminAPIView):
    """Paginated, searchable list of every order across all merchants."""
    required_permission = "orders.view"

    def get(self, request):
        # limit/page/q all come from query params and are best-effort parsed —
        # bad input just falls back to sane defaults rather than erroring.
        try:
            limit = min(int(request.GET.get("limit", 25)), 200)
        except ValueError:
            limit = 25
        try:
            page = max(int(request.GET.get("page", 1)), 1)
        except ValueError:
            page = 1
        q = (request.GET.get("q") or "").strip().lower()
        merchant_id = request.GET.get("merchant_id")
        status_filter = request.GET.get("status")
        date_from = parse_date(request.GET.get("date_from") or "")
        date_to = parse_date(request.GET.get("date_to") or "")
        courier_filter = (request.GET.get("courier") or "").strip()
        delivery_status_filter = (request.GET.get("delivery_status") or "").strip()
        shipment_label_status = (request.GET.get("shipment_label_status") or "").strip().lower()
        row_tone = (request.GET.get("row_tone") or "").strip().lower()
        order_scope = (request.GET.get("scope") or "all").strip().lower()
        sort_order = (request.GET.get("sort") or "newest").strip().lower()
        order_by = "order_date" if sort_order == "oldest" else "-order_date"

        qs = OrderInfo.objects.select_related(
            "merchant", "customer_info", "shipment_id", "enquiry"
        ).order_by(order_by)

        if merchant_id:
            qs = qs.filter(merchant_id=merchant_id)
        if status_filter:
            qs = qs.filter(order_status__iexact=status_filter)
        if date_from:
            qs = qs.filter(order_date__date__gte=date_from)
        if date_to:
            qs = qs.filter(order_date__date__lte=date_to)
        if courier_filter:
            qs = qs.filter(shipment_id__courier__iexact=courier_filter)
        if delivery_status_filter:
            qs = qs.filter(shipment_id__status__iexact=delivery_status_filter)

        # .values(...) flattens related fields (merchant__merchant_name, etc.)
        # straight into dicts the frontend can render without extra joins.
        rows = list(qs.values(
            "id",
            "merchant_order_id",
            "pa_order_id",
            "pa_payment_id",
            "phonepe_payment_id",
            "order_amount",
            "order_currency",
            "order_status",
            "payment_state",
            "order_date",
            "payment_provider",
            "merchant_id",
            "merchant__merchant_name",
            "merchant__merchant_email",
            "customer_info__customer_name",
            "customer_info_id",
            "customer_info__customer_email",
            "customer_info__customer_phone",
            "customer_info__customer_address",
            "shipment_id__awb",
            "shipment_id__courier",
            "shipment_id__status",
            "enquiry_id",
            "enquiry__enquiry_id",
            "enquiry__status",
        ))

        for row in rows:
            row["display_payment_id"] = numeric_payment_id(
                f"order:{row.get('merchant_order_id')}:{row.get('id')}"
            )

        merchant_risk = {
            profile.merchant_id: profile
            for profile in MerchantRiskProfile.objects.filter(
                merchant_id__in={row["merchant_id"] for row in rows if row.get("merchant_id")}
            )
        }
        for row in rows:
            profile = merchant_risk.get(row.get("merchant_id"))
            row["merchant_is_flagged"] = bool(profile and is_flagged(profile))
            row["merchant_success_rate"] = (
                float(profile.success_rate) if profile and profile.scored_orders > 0 else None
            )
            row["merchant_review_threshold"] = float(FLAG_THRESHOLD)

        customer_risk = {profile.customer_id: profile for profile in refresh_all_customer_scores()}
        for row in rows:
            profile = customer_risk.get(row.get("customer_info_id"))
            row["customer_is_flagged"] = bool(profile and customer_is_flagged(profile))
            row["customer_success_rate"] = (
                float(profile.success_rate) if profile and profile.assessed_orders > 0 else None
            )
            row["customer_review_threshold"] = float(CUSTOMER_FLAG_THRESHOLD)

        # Free-text search is done in Python rather than the DB because it
        # spans multiple unrelated columns (order id, customer, merchant) —
        # simplest thing that works at this data volume.
        if q:
            rows = [
                r
                for r in rows
                if q
                in " ".join(
                    str(v) for v in [
                        r.get("merchant_order_id"),
                        r.get("pa_order_id"),
                        r.get("display_payment_id"),
                        r.get("shipment_id__awb"),
                        r.get("customer_info__customer_name"),
                        r.get("customer_info__customer_email"),
                        r.get("customer_info__customer_phone"),
                        r.get("merchant__merchant_name"),
                    ] if v
                ).lower()
            ]
        attach_shipment_label_status(rows, request=request)
        attach_verification_decisions(rows)
        attach_issue_status(rows)
        for row in rows:
            row["row_tone"] = order_row_tone(row)
        if shipment_label_status:
            rows = [
                row
                for row in rows
                if (row.get("shipment_label_status") or "").lower() == shipment_label_status
            ]
        if row_tone in {"success", "pending", "failed", "resolved"}:
            rows = [row for row in rows if row.get("row_tone") == row_tone]
        if order_scope == "completed":
            rows = [
                row for row in rows
                if row.get("row_tone") in {"success", "resolved"}
            ]

        total = len(rows)
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        end = start + limit

        return Response(
            {
                "results": rows[start:end],
                "page": page,
                "total_pages": total_pages,
                "total": total,
            }
        )


class AdminMerchantListView(AdminAPIView):
    """Small merchant list used by admin filters."""
    required_permission = "orders.view"

    def get(self, request):
        q = (request.GET.get("q") or "").strip().lower()
        merchants = list(
            MerchantInfo.objects.order_by("merchant_name").values(
                "id",
                "merchant_name",
                "merchant_email",
                "merchant_phone",
                "created_at",
            )
        )
        for merchant in merchants:
            email = merchant.get("merchant_email") or ""
            merchant["username"] = email.split("@", 1)[0] if email else f"merchant-{merchant['id']}"
        profiles = {
            profile.merchant_id: profile
            for profile in MerchantRiskProfile.objects.filter(
                merchant_id__in=[merchant["id"] for merchant in merchants]
            ).select_related("blocked_by")
        }
        for merchant in merchants:
            profile = profiles.get(merchant["id"])
            blocked = profile.currently_blocked() if profile else False
            merchant["is_blocked"] = blocked
            merchant["access_status"] = profile.access_status if profile else MerchantRiskProfile.ACCESS_ACTIVE
            merchant["block_reason"] = profile.block_reason if blocked else ""
            merchant["blocked_until"] = profile.blocked_until.isoformat() if blocked and profile.blocked_until else None
            if not blocked:
                merchant["restriction_period"] = "—"
            elif profile.access_status == MerchantRiskProfile.ACCESS_PERMANENT:
                merchant["restriction_period"] = "Permanent"
            else:
                remaining = max(1, ceil((profile.blocked_until - timezone.now()).total_seconds() / 86400))
                merchant["restriction_period"] = f"{remaining} day{'s' if remaining != 1 else ''} remaining"
        if q:
            merchants = [
                merchant
                for merchant in merchants
                if q
                in " ".join(
                    str(value)
                    for value in [
                        merchant.get("merchant_name"),
                        merchant.get("merchant_email"),
                        merchant.get("username"),
                        merchant.get("merchant_phone"),
                    ]
                    if value
                ).lower()
            ]
        return Response({"results": merchants})


class AdminMerchantDetailView(AdminAPIView):
    """Owner-only merchant deletion.

    Deleting MerchantInfo intentionally cascades merchant-owned orders,
    checkout sessions, notifications, and uploaded label records through the
    model relationships.
    """
    required_permission = "users.manage"

    def delete(self, request, merchant_id):
        try:
            merchant = MerchantInfo.objects.get(id=merchant_id)
        except MerchantInfo.DoesNotExist:
            return Response({"error": "Merchant not found."}, status=status.HTTP_404_NOT_FOUND)

        merchant_name = merchant.merchant_name
        merchant.delete()
        return Response({"deleted": True, "merchant_name": merchant_name}, status=status.HTTP_200_OK)


class AdminEnquiryListView(AdminAPIView):
    """Paginated, searchable list of every customer enquiry, enriched with
    the order/customer/shipment/notes/resolution context an admin needs to
    act on it without opening several other screens."""
    required_permission = "enquiries.view"

    def get(self, request):
        try:
            limit = min(int(request.GET.get("limit", 25)), 200)
        except ValueError:
            limit = 25
        try:
            page = max(int(request.GET.get("page", 1)), 1)
        except ValueError:
            page = 1
        q = (request.GET.get("q") or "").strip().lower()
        status_filter = request.GET.get("status")

        qs = EnquiryData.objects.all().order_by("-created_at")
        if status_filter:
            qs = qs.filter(status__iexact=status_filter)

        enquiries = list(
            qs.values(
                "id",
                "enquiry_id",
                "order_id",
                "enquiry_text",
                "receipt_status",
                "someone_else_received",
                "agent_contacted",
                "otp_shared",
                "unboxing_evidence",
                "evidence_file",
                "status",
                "created_at",
                "updated_at",
                "resolution_status",
                "resolution_reason",
                "resolved_by__username",
                "resolved_at",
            )
        )

        # NOTE: EnquiryData.order_id is a free-text field the enquiry form
        # captures from the customer — in practice it holds the payment
        # aggregator's order id (pa_order_id), not our merchant_order_id.
        # We match against both so enquiries resolve to an order either way.
        order_ids = [e["order_id"] for e in enquiries if e["order_id"]]
        matched_orders = list(
            OrderInfo.objects.filter(
                Q(pa_order_id__in=order_ids) | Q(merchant_order_id__in=order_ids)
            )
            .select_related("merchant", "customer_info", "shipment_id")
            .values(
                "merchant_order_id",
                "pa_order_id",
                "order_amount",
                "order_currency",
                "order_status",
                "merchant_id",
                "merchant__merchant_name",
                "merchant__merchant_email",
                "customer_info__customer_name",
                "customer_info__customer_email",
                "customer_info__customer_phone",
                "shipment_id__awb",
                "shipment_id__courier",
                "shipment_id__status",
            )
        )
        # Index by both id styles so the lookup below is a single dict hit
        # regardless of which one a given enquiry happens to reference.
        orders_by_order_id = {}
        for o in matched_orders:
            if o["pa_order_id"]:
                orders_by_order_id[o["pa_order_id"]] = o
            orders_by_order_id[o["merchant_order_id"]] = o

        # Pull just enough from EnquiryNote to show a preview + count in the
        # list view; the full note history is fetched separately per-enquiry
        # when the admin opens the notes modal (AdminEnquiryNoteListView).
        enquiry_ids = [e["id"] for e in enquiries]
        notes_count_by_enquiry = {}
        latest_note_by_enquiry = {}
        for note in (
            EnquiryNote.objects.filter(enquiry_id__in=enquiry_ids)
            .select_related("created_by")
            .order_by("enquiry_id", "-created_at")
        ):
            notes_count_by_enquiry[note.enquiry_id] = notes_count_by_enquiry.get(note.enquiry_id, 0) + 1
            if note.enquiry_id not in latest_note_by_enquiry:
                latest_note_by_enquiry[note.enquiry_id] = {
                    "id": note.id,
                    "note": note.note,
                    "created_by": note.created_by.username if note.created_by else None,
                    "created_at": note.created_at,
                }

        for e in enquiries:
            # Swap the raw stored file path for a browser-openable absolute URL.
            evidence_path = e.pop("evidence_file", None)
            if evidence_path:
                e["evidence_url"] = request.build_absolute_uri(f"{settings.MEDIA_URL}{evidence_path}")
            else:
                e["evidence_url"] = None

            order = orders_by_order_id.get(e["order_id"])
            e["order_amount"] = order.get("order_amount") if order else None
            e["order_currency"] = order.get("order_currency") if order else None
            e["order_status"] = order.get("order_status") if order else None
            e["merchant_id"] = order.get("merchant_id") if order else None
            e["merchant_name"] = order.get("merchant__merchant_name") if order else None
            e["merchant_email"] = order.get("merchant__merchant_email") if order else None
            e["customer_name"] = order.get("customer_info__customer_name") if order else None
            e["customer_email"] = order.get("customer_info__customer_email") if order else None
            e["customer_phone"] = order.get("customer_info__customer_phone") if order else None
            e["shipment_awb"] = order.get("shipment_id__awb") if order else None
            e["shipment_courier"] = order.get("shipment_id__courier") if order else None
            e["shipment_status"] = order.get("shipment_id__status") if order else None

            e["notes_count"] = notes_count_by_enquiry.get(e["id"], 0)
            e["latest_note"] = latest_note_by_enquiry.get(e["id"])

        if q:
            enquiries = [
                e
                for e in enquiries
                if q
                in " ".join(
                    str(v) for v in [
                        e.get("enquiry_id"),
                        e.get("order_id"),
                        e.get("customer_name"),
                        e.get("customer_email"),
                        e.get("customer_phone"),
                        e.get("merchant_name"),
                        e.get("merchant_email"),
                        e.get("shipment_awb"),
                    ] if v
                ).lower()
            ]

        total = len(enquiries)
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        end = start + limit

        return Response(
            {
                "results": enquiries[start:end],
                "page": page,
                "total_pages": total_pages,
                "total": total,
            }
        )

class AdminSuspiciousPdfListView(AdminAPIView):
    """Paginated list of merchant-uploaded PDFs that failed forgery validation."""
    required_permission = "suspicious_pdfs.view"

    def get(self, request):
        try:
            limit = min(int(request.GET.get("limit", 25)), 200)
        except ValueError:
            limit = 25
        try:
            page = max(int(request.GET.get("page", 1)), 1)
        except ValueError:
            page = 1
        q = (request.GET.get("q") or "").strip().lower()
        merchant_id = request.GET.get("merchant_id")
        date_from = parse_date(request.GET.get("date_from") or "")
        date_to = parse_date(request.GET.get("date_to") or "")

        qs = (
            PdfValidationRecord.objects.select_related("merchant")
            .filter(status="not_approved")
            .order_by("-uploaded_at")
        )
        if merchant_id:
            qs = qs.filter(merchant_id=merchant_id)
        if date_from:
            qs = qs.filter(uploaded_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(uploaded_at__date__lte=date_to)

        rows = list(
            qs.values(
                "id",
                "file_name",
                "status",
                "verdict",
                "risk_verdict",
                "score",
                "risk_score",
                "delivery_partner",
                "courier_partner",
                "awb",
                "order_id",
                "uploaded_at",
                "details",
                "merchant_id",
                "merchant__merchant_name",
                "merchant__merchant_email",
                "merchant__merchant_phone",
            )
        )
        for row in rows:
            stored_pdf = (row.pop("details") or {}).get("stored_pdf")
            row["pdf_url"] = request.build_absolute_uri(f"{settings.MEDIA_URL}{stored_pdf}") if stored_pdf else None

        if q:
            rows = [
                row
                for row in rows
                if q
                in " ".join(
                    str(value)
                    for value in [
                        row.get("file_name"),
                        row.get("verdict"),
                        row.get("risk_verdict"),
                        row.get("delivery_partner"),
                        row.get("courier_partner"),
                        row.get("awb"),
                        row.get("order_id"),
                        row.get("merchant__merchant_name"),
                        row.get("merchant__merchant_email"),
                        row.get("merchant__merchant_phone"),
                    ]
                    if value
                ).lower()
            ]

        total = len(rows)
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        end = start + limit

        return Response(
            {
                "results": rows[start:end],
                "page": page,
                "total_pages": total_pages,
                "total": total,
            }
        )
