"""Admin actions taken on an enquiry: leaving notes, and recording where the
disputed money ultimately went (refunded to the customer vs. paid out to the
merchant) along with why. Kept separate from orders_views.py since these are
writes/mutations rather than read-only listings.

*** IMPORTANT: AdminEnquiryResolutionView is a RECORD, not an ACTION ***
Setting resolution_status to "money_refunded" or "money_to_merchant" only
writes that decision into the EnquiryData row (resolution_status,
resolution_reason, resolved_by, resolved_at) — it does NOT call Razorpay's
refund API, does NOT trigger any actual payout to the merchant, and does NOT
move money anywhere. It's purely an audit trail of "an admin decided X, for
reason Y, at time Z" — someone still has to go actually issue the refund (via
the Razorpay dashboard/API) or release the payout separately. If a new
developer is asked to "make the refund button actually refund the customer",
that integration doesn't exist yet and would need to be added — this view
just records the decision.
"""

from django.shortcuts import get_object_or_404
from django.utils import timezone
from datetime import timedelta

from rest_framework.views import Response, status

from adminpanel.permissions import AdminAPIView
from adminpanel.access_control import normalize_special_access
from adminpanel.permissions import role_has_permission
from payments.models import AuditLog
from payments.models.enquirydata import EnquiryData, EnquiryNote
from payments.services.audit import create_audit_log
from payments.services.decision_history import add_decision_history

VALID_RESOLUTION_STATUSES = {choice[0] for choice in EnquiryData.RESOLUTION_STATUS_CHOICES}


def _resolution_hourly_limit_response(request):
    if request.admin_role == "owner":
        return None
    try:
        rules = normalize_special_access(request.admin_user.admin_access_policy.access_rules)
    except Exception:
        rules = {}

    rule = rules.get("update_refund_release_decision") or {}
    has_role_access = role_has_permission(request.admin_role, "enquiries.manage")
    if not has_role_access and not rule.get("enabled"):
        return Response({"error": "You do not have permission to update this decision."}, status=status.HTTP_403_FORBIDDEN)

    hourly_limit = int(rule.get("hourly_limit") or 10)
    one_hour_ago = timezone.now() - timedelta(hours=1)
    used_count = AuditLog.objects.filter(
        actor_user=request.admin_user,
        action="resolution_updated",
        created_at__gte=one_hour_ago,
    ).count()
    if used_count >= hourly_limit:
        return Response(
            {"error": "Hourly approval limit reached. Try again after sometime."},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    return None


def _serialize_note(note):
    """Shared JSON shape for a note, used by both the list and detail views."""
    return {
        "id": note.id,
        "note": note.note,
        "created_by": note.created_by.username if note.created_by else None,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }


class AdminEnquiryNoteListView(AdminAPIView):
    """GET: list all notes for an enquiry. POST: add a new note."""
    required_permission = "enquiries.view"

    def get(self, request, enquiry_id):
        enquiry = get_object_or_404(EnquiryData, id=enquiry_id)
        notes = enquiry.notes.select_related("created_by").order_by("-created_at")
        return Response({"results": [_serialize_note(n) for n in notes]})

    def post(self, request, enquiry_id):
        if request.admin_role == "auditor":
            return Response({"error": "Auditor role is read-only."}, status=status.HTTP_403_FORBIDDEN)
        enquiry = get_object_or_404(EnquiryData, id=enquiry_id)
        text = (request.data.get("note") or "").strip()
        if not text:
            return Response({"error": "note text is required."}, status=status.HTTP_400_BAD_REQUEST)

        note = EnquiryNote.objects.create(
            enquiry=enquiry,
            note=text,
            created_by=request.admin_user,
        )
        create_audit_log(
            case_type="enquiry",
            case_id=enquiry.enquiry_id,
            action="note_created",
            actor_user=request.admin_user,
            actor_role="Admin",
            remarks=text,
            new_values={"note": note.note, "note_id": note.id},
            metadata={"enquiry_db_id": enquiry.id, "order_id": enquiry.order_id},
            source="adminpanel.enquiry_notes",
            request=request,
        )
        add_decision_history(
            case_type="enquiry",
            case_id=enquiry.enquiry_id,
            order_id=enquiry.order_id,
            status=enquiry.resolution_status,
            title="Admin Note Added",
            remarks=text,
            actor_user=request.admin_user,
            actor_role="Admin",
            metadata={"note_id": note.id},
        )
        return Response(_serialize_note(note), status=status.HTTP_201_CREATED)


class AdminEnquiryNoteDetailView(AdminAPIView):
    """PATCH: edit an existing note's text. DELETE: remove a note."""
    required_permission = "enquiries.manage"

    def patch(self, request, enquiry_id, note_id):
        note = get_object_or_404(EnquiryNote, id=note_id, enquiry_id=enquiry_id)
        text = (request.data.get("note") or "").strip()
        if not text:
            return Response({"error": "note text is required."}, status=status.HTTP_400_BAD_REQUEST)

        old_note = note.note
        note.note = text
        note.save(update_fields=["note", "updated_at"])
        create_audit_log(
            case_type="enquiry",
            case_id=note.enquiry.enquiry_id,
            action="note_updated",
            actor_user=request.admin_user,
            actor_role="Admin",
            remarks="Admin note updated",
            old_values={"note": old_note, "note_id": note.id},
            new_values={"note": note.note, "note_id": note.id},
            metadata={"enquiry_db_id": enquiry_id, "order_id": note.enquiry.order_id},
            source="adminpanel.enquiry_notes",
            request=request,
        )
        return Response(_serialize_note(note))

    def delete(self, request, enquiry_id, note_id):
        note = get_object_or_404(EnquiryNote, id=note_id, enquiry_id=enquiry_id)
        create_audit_log(
            case_type="enquiry",
            case_id=note.enquiry.enquiry_id,
            action="note_deleted",
            actor_user=request.admin_user,
            actor_role="Admin",
            remarks="Admin note deleted",
            old_values={"note": note.note, "note_id": note.id},
            metadata={"enquiry_db_id": enquiry_id, "order_id": note.enquiry.order_id},
            source="adminpanel.enquiry_notes",
            request=request,
        )
        note.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminEnquiryResolutionView(AdminAPIView):
    """PATCH: record the money-movement decision for an enquiry, with a
    reason. See the DOES-NOT-MOVE-MONEY warning in this file's module
    docstring above before assuming this triggers a real refund/payout."""
    required_permission = "enquiries.manage"

    def patch(self, request, enquiry_id):
        limit_response = _resolution_hourly_limit_response(request)
        if limit_response is not None:
            return limit_response

        enquiry = get_object_or_404(EnquiryData, id=enquiry_id)

        resolution_status = request.data.get("resolution_status")
        reason = (request.data.get("reason") or "").strip()

        if resolution_status not in VALID_RESOLUTION_STATUSES:
            return Response(
                {"error": f"resolution_status must be one of {sorted(VALID_RESOLUTION_STATUSES)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Moving money either direction must be justified; "unresolved" (e.g.
        # reverting a mistaken action) doesn't need one.
        if resolution_status != "unresolved" and not reason:
            return Response({"error": "reason is required for this status."}, status=status.HTTP_400_BAD_REQUEST)

        old_values = {
            "resolution_status": enquiry.resolution_status,
            "resolution_reason": enquiry.resolution_reason,
            "resolved_by": enquiry.resolved_by.username if enquiry.resolved_by else None,
            "resolved_at": enquiry.resolved_at,
            "status": enquiry.status,
        }

        enquiry.resolution_status = resolution_status
        enquiry.resolution_reason = reason or None
        enquiry.resolved_by = request.admin_user
        enquiry.resolved_at = timezone.now()

        # Recording a real resolution also closes out the enquiry itself.
        # NOTE the asymmetry: there's no corresponding `else` branch that
        # reverts enquiry.status back to "pending"/"reviewed" if an admin
        # later changes resolution_status back to "unresolved" (e.g. to undo
        # a mistaken click) — enquiry.status stays "resolved" from whatever
        # it was last set to. If "undo a resolution" needs to fully reopen
        # the enquiry in the Enquiries list's status filter too, that'd need
        # handling here explicitly.
        if resolution_status != "unresolved":
            enquiry.status = "resolved"

        enquiry.save(
            update_fields=[
                "resolution_status",
                "resolution_reason",
                "resolved_by",
                "resolved_at",
                "status",
                "updated_at",
            ]
        )
        create_audit_log(
            case_type="enquiry",
            case_id=enquiry.enquiry_id,
            action="resolution_updated",
            actor_user=request.admin_user,
            actor_role="Admin",
            remarks=reason or "Resolution reverted to unresolved",
            old_values=old_values,
            new_values={
                "resolution_status": enquiry.resolution_status,
                "resolution_reason": enquiry.resolution_reason,
                "resolved_by": request.admin_user.username,
                "resolved_at": enquiry.resolved_at,
                "status": enquiry.status,
            },
            metadata={"enquiry_db_id": enquiry.id, "order_id": enquiry.order_id},
            source="adminpanel.enquiry_resolution",
            request=request,
        )
        decision_titles = {
            "unresolved": "Decision Reopened",
            "money_refunded": "Refund Decision Recorded",
            "money_to_merchant": "Release Decision Recorded",
        }
        add_decision_history(
            case_type="enquiry",
            case_id=enquiry.enquiry_id,
            order_id=enquiry.order_id,
            status=enquiry.resolution_status,
            title=decision_titles.get(enquiry.resolution_status, "Decision Updated"),
            remarks=reason or "Resolution reverted to unresolved",
            actor_user=request.admin_user,
            actor_role="Admin",
            metadata={
                "resolution_status": enquiry.resolution_status,
                "enquiry_status": enquiry.status,
            },
        )

        return Response(
            {
                "id": enquiry.id,
                "resolution_status": enquiry.resolution_status,
                "resolution_reason": enquiry.resolution_reason,
                "resolved_by": request.admin_user.username,
                "resolved_at": enquiry.resolved_at,
                "status": enquiry.status,
            }
        )
