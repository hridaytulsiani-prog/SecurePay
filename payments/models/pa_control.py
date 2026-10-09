from django.db import models
from django.utils import timezone


class PAProviderCapability(models.Model):
    id = models.BigAutoField(primary_key=True)
    provider = models.CharField(max_length=64, unique=True)
    display_name = models.CharField(max_length=128)
    supports_per_order_hold = models.BooleanField(default=False)
    supports_release = models.BooleanField(default=False)
    supports_refund_before_settlement = models.BooleanField(default=False)
    supports_refund_after_settlement = models.BooleanField(default=False)
    supports_webhooks = models.BooleanField(default=True)
    supports_idempotency = models.BooleanField(default=True)
    supports_status_lookup = models.BooleanField(default=True)
    supports_reconciliation = models.BooleanField(default=True)
    max_hold_days = models.PositiveIntegerField(default=0)
    manual_review_required = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider"]

    def __str__(self):
        return self.display_name


class PAProtectedOrder(models.Model):
    id = models.BigAutoField(primary_key=True)
    PAYMENT_CREATED = "CREATED"
    PAYMENT_SUCCEEDED = "SUCCEEDED"
    PAYMENT_FAILED = "FAILED"
    PAYMENT_HELD = "HELD"
    PAYMENT_EXPIRED = "EXPIRED"
    PAYMENT_DISPUTED = "DISPUTED"

    FULFILMENT_NOT_STARTED = "NOT_STARTED"
    FULFILMENT_IN_TRANSIT = "IN_TRANSIT"
    FULFILMENT_DELIVERED = "DELIVERED"
    FULFILMENT_FAILED = "DELIVERY_FAILED"
    FULFILMENT_RETURNED = "RETURNED"

    CONFIRMATION_PENDING = "PENDING"
    CONFIRMATION_CONFIRMED = "CONFIRMED"
    CONFIRMATION_EXPIRED = "EXPIRED"
    CONFIRMATION_REJECTED = "REJECTED"

    DECISION_PENDING = "PENDING"
    DECISION_RELEASE = "RELEASE"
    DECISION_REFUND = "REFUND"
    DECISION_MANUAL_REVIEW = "MANUAL_REVIEW"

    COMMAND_NOT_SENT = "NOT_SENT"
    COMMAND_ACCEPTED = "ACCEPTED"
    COMMAND_PENDING = "PENDING"
    COMMAND_COMPLETED = "COMPLETED"
    COMMAND_REJECTED = "REJECTED"
    COMMAND_UNKNOWN = "UNKNOWN"

    PROTECTION_PENDING = "PENDING_HOLD"
    PROTECTION_PROTECTED = "PROTECTED"
    PROTECTION_UNSUPPORTED = "UNSUPPORTED"
    PROTECTION_EXPIRED = "EXPIRED"

    vaultpay_order_id = models.CharField(max_length=100, unique=True)
    order = models.OneToOneField(
        "payments.OrderInfo",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pa_protection",
    )
    merchant = models.ForeignKey("payments.MerchantInfo", on_delete=models.CASCADE, related_name="pa_protected_orders")
    merchant_order_id = models.CharField(max_length=100)
    pa_provider = models.CharField(max_length=64)
    pa_account_id = models.CharField(max_length=100, blank=True, default="")
    pa_order_id = models.CharField(max_length=128, blank=True, default="")
    pa_payment_id = models.CharField(max_length=128, blank=True, default="")
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=10, default="INR")
    protection_policy = models.CharField(max_length=64, default="DELIVERY_CONFIRMATION")
    payment_state = models.CharField(max_length=32, default=PAYMENT_CREATED)
    fulfilment_state = models.CharField(max_length=32, default=FULFILMENT_NOT_STARTED)
    confirmation_state = models.CharField(max_length=32, default=CONFIRMATION_PENDING)
    decision_state = models.CharField(max_length=32, default=DECISION_PENDING)
    pa_command_state = models.CharField(max_length=32, default=COMMAND_NOT_SENT)
    protection_state = models.CharField(max_length=32, default=PROTECTION_PENDING)
    expires_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=("merchant", "merchant_order_id"), name="pa_po_merchant_order_idx"),
            models.Index(fields=("pa_provider", "pa_payment_id"), name="pa_po_provider_pay_idx"),
            models.Index(fields=("decision_state", "pa_command_state"), name="pa_po_decision_cmd_idx"),
        ]

    def __str__(self):
        return self.vaultpay_order_id


class PACanonicalEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    STATUS_PROCESSED = "PROCESSED"
    STATUS_DUPLICATE = "DUPLICATE"
    STATUS_REJECTED = "REJECTED"

    protected_order = models.ForeignKey(PAProtectedOrder, on_delete=models.CASCADE, related_name="canonical_events")
    event_id = models.CharField(max_length=100, unique=True)
    pa_provider = models.CharField(max_length=64)
    pa_event_id = models.CharField(max_length=128)
    event_type = models.CharField(max_length=64)
    amount_minor = models.PositiveIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=10, default="INR")
    raw_payload = models.JSONField(default=dict, blank=True)
    signature_valid = models.BooleanField(default=True)
    status = models.CharField(max_length=16, default=STATUS_PROCESSED)
    error = models.TextField(blank=True, default="")
    occurred_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("pa_provider", "pa_event_id", "event_type"),
                name="pa_event_provider_event_type_uniq",
            )
        ]
        ordering = ["-created_at"]


class PAFinancialCommand(models.Model):
    id = models.BigAutoField(primary_key=True)
    DECISION_RELEASE = "RELEASE"
    DECISION_REFUND = "REFUND"

    STATUS_NOT_SENT = "NOT_SENT"
    STATUS_PENDING = "PENDING"
    STATUS_ACCEPTED = "ACCEPTED"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_REJECTED = "REJECTED"
    STATUS_UNKNOWN = "UNKNOWN"

    protected_order = models.OneToOneField(PAProtectedOrder, on_delete=models.CASCADE, related_name="financial_command")
    command_id = models.CharField(max_length=100, unique=True)
    decision = models.CharField(max_length=16)
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=10, default="INR")
    reason_code = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=180, unique=True)
    provider_reference = models.CharField(max_length=128)
    evidence_token = models.CharField(max_length=80, unique=True, null=True, blank=True)
    evidence_report_url = models.CharField(max_length=500, blank=True, default="")
    status = models.CharField(max_length=16, default=STATUS_PENDING)
    provider_request = models.JSONField(default=dict, blank=True)
    provider_response = models.JSONField(default=dict, blank=True)
    attempts = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True, default="")
    issued_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("decision", "status"), name="pa_cmd_decision_status_idx")]

    def __str__(self):
        return self.command_id


class PAReconciliationRecord(models.Model):
    id = models.BigAutoField(primary_key=True)
    STATUS_NOT_RUN = "NOT_RUN"
    STATUS_MATCHED = "MATCHED"
    STATUS_EXCEPTION = "EXCEPTION"

    protected_order = models.OneToOneField(PAProtectedOrder, on_delete=models.CASCADE, related_name="pa_reconciliation")
    status = models.CharField(max_length=16, default=STATUS_NOT_RUN)
    mismatches = models.JSONField(default=list, blank=True)
    provider_report = models.JSONField(default=dict, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)


class PAAuditEntry(models.Model):
    id = models.BigAutoField(primary_key=True)
    protected_order = models.ForeignKey(PAProtectedOrder, on_delete=models.CASCADE, related_name="pa_audit_entries")
    event_type = models.CharField(max_length=64)
    message = models.TextField()
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=("protected_order", "created_at"), name="pa_audit_order_created_idx")]
