from django.db.models import Q
from rest_framework import status
from rest_framework.views import APIView, Response

from adminpanel.permissions import AdminAPIView
from payments.models import MerchantInfo, PAFinancialCommand, PAProviderCapability, PAProtectedOrder
from payments.services.pa_control import (
    apply_provider_command_result,
    auto_refund_overdue_missing_pdf,
    dispatch_financial_command,
    ensure_default_capabilities,
    ingest_mock_event,
    register_protected_order,
    run_reconciliation,
    send_aggregator_request,
    serialize_evidence_report,
    serialize_capability,
    serialize_order,
    sync_recent_orders_to_pa,
    update_delivery_state,
)


class PACapabilityListView(AdminAPIView):
    required_permission = "pa_control.manage"

    def get(self, request):
        ensure_default_capabilities()
        rows = PAProviderCapability.objects.all()
        return Response({"results": [serialize_capability(item) for item in rows]})


class PAProtectedOrderListCreateView(AdminAPIView):
    required_permission = "pa_control.manage"

    def get(self, request):
        ensure_default_capabilities()
        if request.GET.get("sync") == "1":
            sync_recent_orders_to_pa(limit=50)
        q = (request.GET.get("q") or "").strip()
        rows = PAProtectedOrder.objects.select_related("merchant", "order").prefetch_related(
            "canonical_events", "pa_audit_entries"
        ).filter(order__isnull=False)
        if q:
            rows = rows.filter(
                Q(vaultpay_order_id__icontains=q)
                | Q(merchant_order_id__icontains=q)
                | Q(pa_order_id__icontains=q)
                | Q(pa_payment_id__icontains=q)
                | Q(merchant__merchant_name__icontains=q)
            )
        rows = list(rows.order_by("-created_at")[:100])
        for item in rows:
            auto_refund_overdue_missing_pdf(item)
            item.refresh_from_db()
            if (
                item.decision_state in {PAProtectedOrder.DECISION_RELEASE, PAProtectedOrder.DECISION_REFUND}
                and not (item.metadata or {}).get("aggregator_request")
            ):
                send_aggregator_request(item, item.decision_state, actor=getattr(request, "admin_user", None))
                item.refresh_from_db()
        return Response({"results": [serialize_order(item) for item in rows]})

    def post(self, request):
        ensure_default_capabilities()
        merchant_id = request.data.get("merchant_id")
        merchant = MerchantInfo.objects.filter(id=merchant_id).first() if merchant_id else MerchantInfo.objects.first()
        if not merchant:
            return Response({"error": "Create a merchant before registering a PA protected order."}, status=400)
        payload = dict(request.data)
        payload["merchant"] = merchant
        try:
            protected_order = register_protected_order(payload)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        return Response({"order": serialize_order(protected_order)}, status=status.HTTP_201_CREATED)


class PAProtectedOrderActionView(AdminAPIView):
    required_permission = "pa_control.manage"

    def post(self, request, vaultpay_order_id, action):
        protected_order = PAProtectedOrder.objects.filter(vaultpay_order_id=vaultpay_order_id).first()
        if not protected_order:
            return Response({"error": "protected order not found"}, status=404)

        try:
            if action == "event":
                ingest_mock_event(
                    protected_order,
                    request.data.get("event_type") or "PAYMENT_SUCCEEDED",
                    signature_valid=bool(request.data.get("signature_valid", True)),
                    pa_event_id=request.data.get("pa_event_id") or None,
                )
            elif action == "delivery":
                update_delivery_state(
                    protected_order,
                    fulfilment_state=request.data.get("fulfilment_state") or None,
                    confirmation_state=request.data.get("confirmation_state") or None,
                )
            elif action == "dispatch":
                dispatch_financial_command(protected_order)
            elif action == "provider-result":
                apply_provider_command_result(protected_order, request.data.get("status") or "ACCEPTED")
            elif action == "provider-accept":
                dispatch_financial_command(protected_order)
                apply_provider_command_result(protected_order, "ACCEPTED")
            elif action == "send-request":
                send_aggregator_request(
                    protected_order,
                    request.data.get("decision"),
                    actor=getattr(request, "admin_user", None),
                )
            elif action == "reconcile":
                run_reconciliation(protected_order, force_mismatch=bool(request.data.get("force_mismatch", False)))
            else:
                return Response({"error": "unknown PA action"}, status=400)
        except ValueError as exc:
            return Response({"error": str(exc), "order": serialize_order(protected_order)}, status=400)

        protected_order.refresh_from_db()
        return Response({"order": serialize_order(protected_order)})


class PAEvidenceReportView(APIView):
    """Read-only tokenized report for PA evidence review."""

    authentication_classes = []
    permission_classes = []

    def get(self, request, token):
        command = (
            PAFinancialCommand.objects.select_related(
                "protected_order",
                "protected_order__merchant",
                "protected_order__order",
                "protected_order__order__customer_info",
            )
            .filter(evidence_token=token)
            .first()
        )
        if not command:
            return Response({"error": "Evidence report not found."}, status=404)
        return Response({"report": serialize_evidence_report(command)})
