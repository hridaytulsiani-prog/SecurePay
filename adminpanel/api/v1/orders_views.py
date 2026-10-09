"""Cross-merchant read views for the admin panel: every order and every
enquiry in the system, regardless of which merchant they belong to.

The merchant-facing views (payments.api.v1.generate_order.ShipmentListView)
scope everything to the logged-in merchant's own orders; these views
deliberately don't, since the whole point of the admin panel is to see
across merchants.
"""

from django.conf import settings
from django.db.models import Q
from django.utils.dateparse import parse_date

from rest_framework.views import Response

from adminpanel.permissions import AdminAPIView
from payments.models.merchantinfo import MerchantInfo
from payments.models.orderinfo import OrderInfo
from payments.models.enquirydata import EnquiryData, EnquiryNote
from tracking.models.pdf_validation import PdfValidationRecord
from tracking.models.delhivery_verification_decision import DelhiveryVerificationDecision


def attach_shipment_label_status(rows):
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
            row["shipment_label_status"] = "missing"
            row["shipment_label_verdict"] = None
            row["shipment_label_file_name"] = None
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
            row["shipment_label_status"] = "missing"
            row["shipment_label_verdict"] = None
            row["shipment_label_file_name"] = None
            continue

        row["shipment_label_status"] = record.status
        row["shipment_label_verdict"] = record.verdict
        row["shipment_label_file_name"] = record.file_name


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

        qs = OrderInfo.objects.select_related(
            "merchant", "customer_info", "shipment_id", "enquiry"
        ).order_by("-order_date")

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
        rows = qs.values(
            "id",
            "merchant_order_id",
            "pa_order_id",
            "pa_payment_id",
            "order_amount",
            "order_currency",
            "order_status",
            "order_date",
            "payment_provider",
            "merchant_id",
            "merchant__merchant_name",
            "merchant__merchant_email",
            "customer_info__customer_name",
            "customer_info__customer_email",
            "customer_info__customer_phone",
            "customer_info__customer_address",
            "shipment_id__awb",
            "shipment_id__courier",
            "shipment_id__status",
            "enquiry_id",
            "enquiry__enquiry_id",
            "enquiry__status",
        )

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
                        r.get("shipment_id__awb"),
                        r.get("customer_info__customer_name"),
                        r.get("customer_info__customer_email"),
                        r.get("customer_info__customer_phone"),
                        r.get("merchant__merchant_name"),
                    ] if v
                ).lower()
            ]
        else:
            rows = list(rows)

        attach_shipment_label_status(rows)
        attach_verification_decisions(rows)
        if shipment_label_status:
            rows = [
                row
                for row in rows
                if (row.get("shipment_label_status") or "").lower() == shipment_label_status
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
