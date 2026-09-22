from django.db.models import Q

from rest_framework.views import Response

from adminpanel.permissions import AdminAPIView
from payments.models import DecisionHistory


def _serialize_history(row):
    return {
        "id": row.id,
        "case_type": row.case_type,
        "case_id": row.case_id,
        "order_id": row.order_id,
        "status": row.status,
        "title": row.title,
        "remarks": row.remarks,
        "actor": row.actor_display or (row.actor_user.username if row.actor_user else ""),
        "actor_role": row.actor_role,
        "version": row.version,
        "metadata": row.metadata,
        "created_at": row.created_at,
    }


class AdminDecisionHistoryListView(AdminAPIView):
    """Read-only date-wise status/decision journey for a case or order."""
    required_permission = "decision_history.view"

    def get(self, request):
        case_type = (request.GET.get("case_type") or "").strip()
        case_id = (request.GET.get("case_id") or "").strip()
        order_id = (request.GET.get("order_id") or "").strip()

        qs = DecisionHistory.objects.select_related("actor_user").order_by("created_at", "id")
        if case_type:
            qs = qs.filter(case_type=case_type)
        if case_id:
            qs = qs.filter(case_id=case_id)
        if order_id:
            qs = qs.filter(Q(order_id=order_id) | Q(metadata__order_id=order_id))

        return Response({"results": [_serialize_history(row) for row in qs[:300]]})
