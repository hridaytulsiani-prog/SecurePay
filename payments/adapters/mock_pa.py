from uuid import uuid4

from payments.adapters.pa_contract import (
    CanonicalProviderEvent,
    ProviderCapabilities,
    ProviderCommandResult,
    ProviderOrderReference,
    ReconciliationResult,
)


MOCK_CAPABILITIES = {
    "mock_hold_pa": ProviderCapabilities(
        provider="mock_hold_pa",
        display_name="Mock Hold PA",
        supports_per_order_hold=True,
        supports_release=True,
        supports_refund_before_settlement=True,
        supports_refund_after_settlement=True,
        supports_webhooks=True,
        supports_idempotency=True,
        supports_status_lookup=True,
        supports_reconciliation=True,
        max_hold_days=14,
    ),
    "mock_refund_only_pa": ProviderCapabilities(
        provider="mock_refund_only_pa",
        display_name="Mock Refund-Only PA",
        supports_per_order_hold=False,
        supports_release=False,
        supports_refund_before_settlement=True,
        supports_refund_after_settlement=True,
        supports_webhooks=True,
        supports_idempotency=True,
        supports_status_lookup=True,
        supports_reconciliation=True,
        max_hold_days=0,
        manual_review_required=True,
    ),
    "mock_limited_pa": ProviderCapabilities(
        provider="mock_limited_pa",
        display_name="Mock Limited PA",
        supports_per_order_hold=True,
        supports_release=True,
        supports_refund_before_settlement=False,
        supports_refund_after_settlement=False,
        supports_webhooks=True,
        supports_idempotency=False,
        supports_status_lookup=False,
        supports_reconciliation=False,
        max_hold_days=3,
    ),
}


class MockPaymentAggregatorAdapter:
    def __init__(self, provider):
        self.provider = provider

    def get_capabilities(self):
        return MOCK_CAPABILITIES[self.provider]

    def create_or_register_protected_order(self, payload):
        merchant_order_id = payload.get("merchant_order_id") or uuid4().hex[:10]
        return ProviderOrderReference(
            pa_order_id=payload.get("pa_order_id") or f"{self.provider}_order_{merchant_order_id}",
            pa_payment_id=payload.get("pa_payment_id") or f"{self.provider}_pay_{uuid4().hex[:10]}",
            pa_account_id=payload.get("pa_account_id") or "pa_account_demo",
        )

    def verify_webhook(self, raw_payload, signature=""):
        return bool(raw_payload.get("signature_valid", True))

    def normalize_webhook(self, raw_payload, signature=""):
        signature_valid = self.verify_webhook(raw_payload, signature)
        return [
            CanonicalProviderEvent(
                pa_event_id=raw_payload.get("pa_event_id") or f"{self.provider}_evt_{uuid4().hex[:12]}",
                event_type=raw_payload.get("event_type") or "PAYMENT_SUCCEEDED",
                amount_minor=raw_payload.get("amount_minor"),
                currency=raw_payload.get("currency") or "INR",
                raw_payload=raw_payload,
                signature_valid=signature_valid,
                error="" if signature_valid else "Invalid mock signature",
            )
        ]

    def execute_release(self, command):
        return self._execute_command(command, "release")

    def execute_refund(self, command):
        return self._execute_command(command, "refund")

    def _execute_command(self, command, operation):
        request = {
            "provider": self.provider,
            "operation": operation,
            "provider_reference": command["provider_reference"],
            "amount_minor": command["amount_minor"],
            "currency": command["currency"],
            "idempotency_key": command["idempotency_key"],
            "evidence_report_url": command.get("evidence_report_url", ""),
        }
        return ProviderCommandResult(
            status="PENDING",
            provider_request=request,
            provider_response={"status": "PENDING", "mock": True},
            transport=f"{self.provider}-mock-adapter",
        )

    def get_payment_status(self, reference):
        return {
            "provider": self.provider,
            "pa_order_id": reference.pa_order_id,
            "pa_payment_id": reference.pa_payment_id,
            "status": "HELD",
        }

    def reconcile(self, protected_order, command=None, force_mismatch=False):
        mismatches = []
        if not protected_order.pa_payment_id and not protected_order.pa_order_id:
            mismatches.append("Missing provider reference")
        if command and command.status == "UNKNOWN":
            mismatches.append("Financial command outcome is unknown")
        if force_mismatch:
            mismatches.append("Provider report amount differs from VaultPay order amount")

        return ReconciliationResult(
            status="EXCEPTION" if mismatches else "MATCHED",
            mismatches=mismatches,
            provider_report={
                "provider": self.provider,
                "pa_order_id": protected_order.pa_order_id,
                "pa_payment_id": protected_order.pa_payment_id,
                "amount_minor": protected_order.amount_minor + (100 if force_mismatch else 0),
            },
        )

    def provider_result_event(self, command, result_status):
        if result_status == "ACCEPTED":
            return "REFUND_ACCEPTED" if command.decision == "REFUND" else "RELEASE_ACCEPTED"
        if result_status == "COMPLETED":
            return "REFUND_COMPLETED" if command.decision == "REFUND" else "RELEASE_COMPLETED"
        if result_status == "REJECTED":
            return "REFUND_FAILED" if command.decision == "REFUND" else "RELEASE_FAILED"
        return None
