from django.db.models import Q
from django.utils.dateparse import parse_date

from rest_framework.views import Response

from adminpanel.permissions import AdminAPIView
from payments.models import AuditLog, EnquiryData


def _order_id_for_log(log):
    metadata_order_id = (log.metadata or {}).get("merchant_order_id") or (log.metadata or {}).get("order_id")
    if metadata_order_id:
        return metadata_order_id
    if log.case_type == "enquiry":
        enquiry = EnquiryData.objects.filter(enquiry_id=log.case_id).only("order_id").first()
        return enquiry.order_id if enquiry else ""
    return ""


def _serialize_audit_log(log):
    return {
        "id": log.id,
        "case_type": log.case_type,
        "case_id": log.case_id,
        "order_id": _order_id_for_log(log),
        "action": log.action,
        "actor": log.actor_display or (log.actor_user.username if log.actor_user else ""),
        "actor_role": log.actor_role,
        "remarks": log.remarks,
        "old_values": log.old_values,
        "new_values": log.new_values,
        "changed_fields": log.changed_fields,
        "metadata": log.metadata,
        "source": log.source,
        "request_method": log.request_method,
        "request_path": log.request_path,
        "ip_address": log.ip_address,
        "created_at": log.created_at,
    }


class AdminAuditLogListView(AdminAPIView):
    """Read-only audit trail for sensitive actions across enquiries and refunds."""
    required_permission = "audit.view"

    def get(self, request):
        try:
            limit = min(int(request.GET.get("limit", 25)), 200)
        except ValueError:
            limit = 25
        try:
            page = max(int(request.GET.get("page", 1)), 1)
        except ValueError:
            page = 1

        q = (request.GET.get("q") or "").strip()
        case_type = (request.GET.get("case_type") or "").strip()
        action = (request.GET.get("action") or "").strip()
        date_from = parse_date(request.GET.get("date_from") or "")
        date_to = parse_date(request.GET.get("date_to") or "")

        qs = AuditLog.objects.select_related("actor_user").order_by("-created_at", "-id")
        if case_type:
            qs = qs.filter(case_type=case_type)
        if action:
            qs = qs.filter(action=action)
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        if q:
            qs = qs.filter(
                Q(case_id__icontains=q)
                | Q(action__icontains=q)
                | Q(actor_display__icontains=q)
                | Q(actor_role__icontains=q)
                | Q(remarks__icontains=q)
                | Q(source__icontains=q)
            )

        total = qs.count()
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        end = start + limit

        return Response(
            {
                "results": [_serialize_audit_log(log) for log in qs[start:end]],
                "page": page,
                "total_pages": total_pages,
                "total": total,
            }
        )
