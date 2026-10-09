# Merchant-scoped enquiry list (the merchant sees only their own customers'
# enquiries) — the counterpart to adminpanel.AdminEnquiryListView, which
# shows every merchant's enquiries. Confirms EnquiryData.order_id is matched
# against OrderInfo.pa_order_id, not merchant_order_id.
from django.http import JsonResponse
from django.views import View

from payments.auth import get_merchant_id_from_token
from payments.models import EnquiryData, OrderInfo


def _get_auth_token_from_request(request):
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1].strip()

    return request.GET.get("auth_token")


class EnquiryListView(View):
    def get(self, request, *args, **kwargs):
        """
        GET /tracking/enquiries/
        Accepts optional query params: limit (int), page (int), q (search string).
        Returns: { results: [...], page: 1, total_pages: 1, total: n }
        """
        auth_token = _get_auth_token_from_request(request)
        token_merchant_id = get_merchant_id_from_token(auth_token)
        if token_merchant_id is None:
            return JsonResponse({"error": "Invalid or expired merchant token"}, status=401)

        try:
            limit = int(request.GET.get("limit", 25))
        except ValueError:
            limit = 25
        try:
            page = int(request.GET.get("page", 1))
        except ValueError:
            page = 1
        q = (request.GET.get("q") or "").strip().lower()

        # The scoping step: find every pa_order_id that belongs to THIS
        # merchant, then only pull enquiries whose order_id matches one of
        # those. This is what makes the endpoint merchant-scoped — an
        # enquiry whose order_id belongs to a different merchant's order
        # never appears here, no matter what q/page/limit are passed.
        # (pa_order_id can be None for an order that hasn't reached the
        # payment aggregator yet — that's harmless here since no real
        # EnquiryData.order_id would ever equal None.)
        merchant_order_ids = set(
            OrderInfo.objects.filter(merchant_id=token_merchant_id).values_list("pa_order_id", flat=True)
        )

        orders_by_id = {
            order.pa_order_id: order
            for order in OrderInfo.objects.filter(pa_order_id__in=merchant_order_ids).select_related("customer_info", "shipment_id")
        }

        # NOTE: an order_id can legitimately map to MULTIPLE EnquiryData rows
        # (see the "nothing stops duplicate submissions" note in
        # tracking.api.v1.enquiry.SubmitEnquiry) — every matching row is
        # returned here, not just the latest one per order.
        enquiries = EnquiryData.objects.filter(order_id__in=merchant_order_ids).order_by("-created_at")

        rows = []
        for enquiry in enquiries:
            order = orders_by_id.get(enquiry.order_id)
            customer = getattr(order, "customer_info", None)
            shipment = getattr(order, "shipment_id", None)

            rows.append(
                {
                    "enquiry_id": enquiry.enquiry_id,
                    "order_id": enquiry.order_id,
                    "enquiry_text": enquiry.enquiry_text,
                    "receipt_status": enquiry.receipt_status,
                    "someone_else_received": enquiry.someone_else_received,
                    "agent_contacted": enquiry.agent_contacted,
                    "otp_shared": enquiry.otp_shared,
                    "unboxing_evidence": enquiry.unboxing_evidence,
                    "evidence_file_url": request.build_absolute_uri(enquiry.evidence_file.url)
                    if enquiry.evidence_file
                    else None,
                    "status": enquiry.status,
                    "created_at": enquiry.created_at.isoformat(),
                    "customer_name": getattr(customer, "customer_name", None),
                    "customer_phone": getattr(customer, "customer_phone", None),
                    "customer_email": getattr(customer, "customer_email", None),
                    "shipment_awb": getattr(shipment, "awb", None),
                    "shipment_courier": getattr(shipment, "courier", None),
                }
            )

        if q:
            rows = [
                row
                for row in rows
                if q in (row["order_id"] or "").lower()
                or q in (row["customer_name"] or "").lower()
                or q in (row["customer_phone"] or "").lower()
                or q in (row["enquiry_id"] or "").lower()
                or q in (row["shipment_awb"] or "").lower()
            ]

        total = len(rows)
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        end = start + limit

        return JsonResponse(
            {
                "results": rows[start:end],
                "page": page,
                "total_pages": total_pages,
                "total": total,
            }
        )
