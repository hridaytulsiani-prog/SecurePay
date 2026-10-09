from django.db import models
from django.utils import timezone


class PaymentEvent(models.Model):
    id = models.AutoField(primary_key=True)
    STATUS_RECEIVED = "RECEIVED"
    STATUS_PROCESSED = "PROCESSED"
    STATUS_DUPLICATE = "DUPLICATE"
    STATUS_REJECTED = "REJECTED"
    STATUS_FAILED = "FAILED"

    provider = models.CharField(max_length=64)
    event_id = models.CharField(max_length=255, blank=True, default="")
    event_hash = models.CharField(max_length=64)
    event_type = models.CharField(max_length=128, blank=True, default="")
    order = models.ForeignKey(
        "payments.OrderInfo", null=True, blank=True, on_delete=models.SET_NULL, related_name="payment_events"
    )
    payload = models.JSONField(default=dict)
    signature = models.CharField(max_length=512, blank=True, default="")
    status = models.CharField(max_length=16, default=STATUS_RECEIVED)
    error = models.TextField(blank=True, default="")
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("provider", "event_hash"), name="payment_event_provider_hash_uniq"),
        ]
        indexes = [
            models.Index(fields=("provider", "event_id"), name="payments_pe_provider_event_idx"),
            models.Index(fields=("status", "received_at"), name="pay_pe_status_recv_idx"),
        ]


class PaymentStateTransition(models.Model):
    id = models.AutoField(primary_key=True)
    order = models.ForeignKey("payments.OrderInfo", on_delete=models.CASCADE, related_name="payment_state_transitions")
    from_state = models.CharField(max_length=32, blank=True, default="")
    to_state = models.CharField(max_length=32)
    source = models.CharField(max_length=64, default="system")
    event = models.ForeignKey(PaymentEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name="state_transitions")
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]


class PaymentLedgerEntry(models.Model):
    id = models.AutoField(primary_key=True)
    ENTRY_CAPTURED = "CAPTURED"
    ENTRY_HELD = "HELD"
    ENTRY_SETTLEMENT = "SETTLEMENT"
    ENTRY_FEE = "FEE"
    ENTRY_REFUND = "REFUND"
    ENTRY_REVERSAL = "REVERSAL"

    order = models.ForeignKey("payments.OrderInfo", on_delete=models.CASCADE, related_name="ledger_entries")
    entry_type = models.CharField(max_length=32)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=10, default="INR")
    reference = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=("order", "entry_type", "created_at"), name="payments_ple_order_type_idx")]


class PaymentNotification(models.Model):
    id = models.AutoField(primary_key=True)
    CHANNEL_DASHBOARD = "DASHBOARD"
    CHANNEL_EMAIL = "EMAIL"
    CHANNEL_WEBHOOK = "WEBHOOK"
    STATUS_PENDING = "PENDING"
    STATUS_SENT = "SENT"
    STATUS_FAILED = "FAILED"

    merchant = models.ForeignKey("payments.MerchantInfo", on_delete=models.CASCADE, related_name="payment_notifications")
    order = models.ForeignKey("payments.OrderInfo", null=True, blank=True, on_delete=models.CASCADE, related_name="payment_notifications")
    event_type = models.CharField(max_length=64)
    channel = models.CharField(max_length=16, default=CHANNEL_DASHBOARD)
    dedupe_key = models.CharField(max_length=255, unique=True)
    title = models.CharField(max_length=255)
    message = models.TextField()
    status = models.CharField(max_length=16, default=STATUS_PENDING)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=("merchant", "status", "created_at"), name="pay_pn_mer_status_idx")]


class SettlementRecord(models.Model):
    id = models.AutoField(primary_key=True)
    STATUS_PENDING = "PENDING"
    STATUS_PROCESSING = "PROCESSING"
    STATUS_SETTLED = "SETTLED"
    STATUS_FAILED = "FAILED"
    STATUS_MISMATCH = "MISMATCH"

    order = models.OneToOneField("payments.OrderInfo", on_delete=models.CASCADE, related_name="settlement_record")
    provider = models.CharField(max_length=64)
    settlement_id = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=16, default=STATUS_PENDING)
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    fee_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    eligible_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    raw_response = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("status", "eligible_at"), name="pay_sr_status_elig_idx")]
