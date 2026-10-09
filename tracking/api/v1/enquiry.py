# Public endpoint (no login) that a customer hits from the signed enquiry
# link (see tracking.signing.build_enquiry_link) to report a delivery
# problem. Feeds adminpanel's Enquiries dashboard on the other end.
#
# REQUEST FORMAT: must be multipart/form-data, not JSON — evidence_file is
# read from request.FILES, which only gets populated for multipart uploads.
# The frontend's api/enquiry.js (securepay-client) builds this with a
# FormData object for exactly this reason.
#
# NOTHING STOPS DUPLICATE SUBMISSIONS FOR THE SAME ORDER: there's no check
# here for "does an EnquiryData row already exist for this order_id" — the
# signed link can be submitted against multiple times (a customer resubmitting
# after being told to add more detail, retrying after what looked like a
# failed submit, etc.), and each submission creates its own separate
# EnquiryData row, all sharing the same order_id. This is why a single order
# can show up with several ENQxx rows in the admin panel's Enquiries list —
# that's expected, not a bug, but worth knowing so you don't assume order_id
# is unique on EnquiryData (it explicitly isn't — only enquiry_id is).
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from payments.models import EnquiryData
from payments.services.decision_history import add_decision_history
from tracking.signing import verify_order_signature

VALID_RECEIPT_STATUSES = {choice for choice, _ in EnquiryData.RECEIPT_STATUS_CHOICES}


def _parse_bool(value):
    """Form data arrives as strings ("true"/"false"/"1"/"0"/etc.), not real
    booleans, so this normalizes to Python True/False/None. Returns None
    (not False) for a genuinely missing/blank field — these model fields
    (someone_else_received, agent_contacted, ...) are nullable specifically
    because not every enquiry step applies to every receipt_status branch of
    the customer-facing wizard (see securepay-client's Enquiry.jsx), so
    "not asked" (None) needs to stay distinguishable from "asked and
    answered no" (False)."""
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 'yes')


class SubmitEnquiry(APIView):
    def post(self, request):
        order_id = request.data.get('order_id')
        signature = request.data.get('sig')
        enquiry_text = request.data.get('enquiry_msg')
        receipt_status = request.data.get('receipt_status')

        if not order_id or not enquiry_text or not receipt_status:
            return Response({"error": "Missing required fields."}, status=status.HTTP_400_BAD_REQUEST)

        if not verify_order_signature(order_id, signature):
            return Response({"error": "Invalid or tampered order link."}, status=status.HTTP_403_FORBIDDEN)

        if receipt_status not in VALID_RECEIPT_STATUSES:
            return Response({"error": "Invalid receipt_status."}, status=status.HTTP_400_BAD_REQUEST)

        # NOTE: count()+1 for the id is racy under concurrent submissions and
        # can collide/reuse a number if an earlier enquiry was ever deleted
        # (enquiry_id is unique=True, so a collision here would 500 instead
        # of silently overwriting).
        enquiry = EnquiryData.objects.create(
            enquiry_id=f"ENQ{EnquiryData.objects.count() + 1}",
            order_id=order_id,
            enquiry_text=enquiry_text,
            receipt_status=receipt_status,
            someone_else_received=_parse_bool(request.data.get('someone_else_received')),
            agent_contacted=_parse_bool(request.data.get('agent_contacted')),
            otp_shared=_parse_bool(request.data.get('otp_shared')),
            unboxing_evidence=_parse_bool(request.data.get('unboxing_evidence')),
            evidence_file=request.FILES.get('evidence_file'),
        )
        add_decision_history(
            case_type="enquiry",
            case_id=enquiry.enquiry_id,
            order_id=enquiry.order_id,
            status="created",
            title="Enquiry Created",
            remarks="Customer enquiry case created.",
            actor_display="customer",
            actor_role="Customer",
            metadata={"receipt_status": receipt_status},
        )
        add_decision_history(
            case_type="enquiry",
            case_id=enquiry.enquiry_id,
            order_id=enquiry.order_id,
            status="submitted",
            title="Submitted For Review",
            remarks=enquiry_text,
            actor_display="customer",
            actor_role="Customer",
            metadata={
                "receipt_status": receipt_status,
                "agent_contacted": enquiry.agent_contacted,
                "otp_shared": enquiry.otp_shared,
                "unboxing_evidence": enquiry.unboxing_evidence,
            },
        )

        return Response({"message": "Enquiry submitted successfully.", "enquiry_id": enquiry.enquiry_id}, status=status.HTTP_201_CREATED)
