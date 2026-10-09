from django.db import models
from django.utils import timezone


class RefundRequest(models.Model):
    STATUS_REQUESTED = "REQUESTED"
    STATUS_PENDING = "PENDING"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_FAILED = "FAILED"
    STATUS_REJECTED = "REJECTED"

    STATUS_CHOICES = [
        (STATUS_REQUESTED, "Requested"),
        (STATUS_PENDING, "Pending"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
        (STATUS_REJECTED, "Rejected"),
    ]

    id = models.AutoField(primary_key=True)
    order = models.OneToOneField(
        "payments.OrderInfo",
        on_delete=models.CASCADE,
        related_name="refund_request",
    )
    provider = models.CharField(max_length=64)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=10, default="INR")
    reason = models.CharField(max_length=255)
    refund_reference = models.CharField(max_length=100, unique=True)
    provider_payment_id = models.CharField(max_length=255, blank=True, default="")
    provider_refund_id = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_REQUESTED)
    verification_snapshot = models.JSONField(default=dict)
    provider_request = models.JSONField(default=dict)
    provider_response = models.JSONField(default=dict)
    error = models.TextField(blank=True, default="")
    requested_at = models.DateTimeField(default=timezone.now)
    provider_submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=("provider", "provider_refund_id"), name="pay_rr_provider_refund_idx"),
            models.Index(fields=("status", "requested_at"), name="pay_rr_status_req_idx"),
        ]

    def __str__(self):
        return f"{self.refund_reference} - {self.status}"
