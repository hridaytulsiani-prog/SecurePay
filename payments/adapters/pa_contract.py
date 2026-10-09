from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    display_name: str
    supports_per_order_hold: bool
    supports_release: bool
    supports_refund_before_settlement: bool
    supports_refund_after_settlement: bool
    supports_webhooks: bool
    supports_idempotency: bool
    supports_status_lookup: bool
    supports_reconciliation: bool
    max_hold_days: int = 0
    manual_review_required: bool = False

    def to_model_defaults(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderOrderReference:
    pa_order_id: str
    pa_payment_id: str
    pa_account_id: str


@dataclass(frozen=True)
class CanonicalProviderEvent:
    pa_event_id: str
    event_type: str
    amount_minor: int | None
    currency: str
    raw_payload: dict[str, Any]
    signature_valid: bool
    error: str = ""


@dataclass(frozen=True)
class ProviderCommandResult:
    status: str
    provider_request: dict[str, Any]
    provider_response: dict[str, Any]
    transport: str


@dataclass(frozen=True)
class ReconciliationResult:
    status: str
    mismatches: list[str]
    provider_report: dict[str, Any]


class PaymentAggregatorAdapter(Protocol):
    provider: str

    def get_capabilities(self) -> ProviderCapabilities:
        ...

    def create_or_register_protected_order(self, payload: dict[str, Any]) -> ProviderOrderReference:
        ...

    def verify_webhook(self, raw_payload: dict[str, Any], signature: str = "") -> bool:
        ...

    def normalize_webhook(self, raw_payload: dict[str, Any], signature: str = "") -> list[CanonicalProviderEvent]:
        ...

    def execute_release(self, command: dict[str, Any]) -> ProviderCommandResult:
        ...

    def execute_refund(self, command: dict[str, Any]) -> ProviderCommandResult:
        ...

    def get_payment_status(self, reference: ProviderOrderReference) -> dict[str, Any]:
        ...

    def reconcile(self, protected_order: Any, command: Any | None = None, force_mismatch: bool = False) -> ReconciliationResult:
        ...

    def provider_result_event(self, command: Any, result_status: str) -> str | None:
        ...
